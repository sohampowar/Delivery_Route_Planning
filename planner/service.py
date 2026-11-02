"""Application boundary: immutable runs, query projections and local HTTP API."""

import csv
import hashlib
import io
import json
import os
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .engine import SEED, SCALES, allocation_explanations, daily_resources, generate, planning_period
from .schema import SCHEMA
from .storage import DATASET_VERSION, archive, canonical, checksums, export, load
from .validation import evaluate_anomalies, validate

ROOT = Path(__file__).resolve().parent.parent


class Workspace:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.tables = None
        self.current = None
        self._anomaly_cache = None
        pointer = self.directory / "current.json"
        if pointer.exists():
            self.current = json.loads(pointer.read_bytes())
            self.tables = load(self.directory / "versions" / self.current["run_id"] / "planning.sqlite")

    def plan(self, scale="full", seed=SEED, start_date=None, end_date=None):
        if scale not in SCALES or not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 4294967295:
            raise ValueError("Choose starter/full and an integer seed from 0 to 4294967295")
        start, end = planning_period(scale, start_date, end_date)
        with self.lock:
            tables = generate(scale, seed, start.isoformat(), end.isoformat())
            digest = hashlib.sha256(canonical(dict(tables=checksums(tables), dataset_version=DATASET_VERSION))).hexdigest()[:12]
            run_id = f"{scale}-{seed}-{digest}"
            destination = self.directory / "versions" / run_id
            if not destination.exists():
                export(tables, destination, seed, scale)
            self.tables = tables
            self.current = dict(run_id=run_id, scale=scale, seed=seed, start_date=start.isoformat(), end_date=end.isoformat(), days=(end - start).days + 1)
            temporary = self.directory / "current.tmp"
            temporary.write_bytes(canonical(self.current))
            os.replace(temporary, self.directory / "current.json")
            with (self.directory / "run_history.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(self.current, event="PLAN_SELECTED", recorded_at=datetime.now(timezone.utc).isoformat())) + "\n")
            return self.current

    def summary(self, selected_date=None):
        with self.lock:
            tables = self.tables
            dates = sorted(row["scenario_date"] for row in tables["TrafficScenario"])
            selected_date = selected_date or dates[0]
            if selected_date not in dates:
                raise ValueError("Date not in current planning horizon")
            orders = {row["order_id"]: row for row in tables["Order"]}
            sites = {row["site_id"]: row for row in tables["CustomerSite"]}
            deliveries = {row["delivery_id"]: row for row in tables["Delivery"] if row["delivery_date"] == selected_date}
            shifts = {row["shift_id"]: row for row in tables["Shift"] if row["shift_date"] == selected_date}
            available, capacities, restricted = daily_resources(tables, selected_date)
            vehicles = {row["vehicle_id"]: row for row in tables["Vehicle"]}
            outcomes = {row["delivery_id"]: row for row in tables["DeliveryOutcome"] if row["delivery_id"] in deliveries}
            windows = {row["delivery_id"]: row for row in tables["TimeWindow"]}
            scenario = next(row for row in tables["TrafficScenario"] if row["scenario_date"] == selected_date)
            allocation = allocation_explanations(tables, scenario)
            matrix = {(row["origin_id"], row["destination_id"]): row for row in tables["TravelTimeMatrix"] if row["scenario_id"] == scenario["scenario_id"]}
            demand_order = {delivery_id: position for position, delivery_id in enumerate(deliveries)}
            events = defaultdict(list)
            for event in tables["DeliveryEvent"]:
                if event["delivery_id"] in deliveries:
                    events[event["delivery_id"]].append(event)
            stops = defaultdict(list)
            exceptions = []
            for stop in tables["RouteStop"]:
                if stop["delivery_id"] not in deliveries:
                    continue
                delivery = deliveries[stop["delivery_id"]]
                site = sites[orders[delivery["order_id"]]["site_id"]]
                outcome = outcomes[delivery["delivery_id"]]
                enriched = dict(stop, delivery=delivery, site=site, window=windows[delivery["delivery_id"]], outcome=outcome, events=events[delivery["delivery_id"]])
                stops[stop["route_id"]].append(enriched)
                if outcome["status"] != "DELIVERED" or outcome["late_minutes"] > 0:
                    exceptions.append(enriched)
            for delivery_id, outcome in outcomes.items():
                if outcome["status"] == "NOT_ATTEMPTED":
                    delivery = deliveries[delivery_id]
                    exceptions.append(dict(delivery=delivery, site=sites[orders[delivery["order_id"]]["site_id"]], outcome=outcome, window=windows[delivery_id], events=events[delivery_id], stop_id=None, route_id=None, allocation=allocation[delivery_id]))
            routes = []
            route_events = defaultdict(list)
            for event in tables["RouteEvent"]:
                route_events[event["route_id"]].append(event)
            for route in tables["Route"]:
                if route["shift_id"] not in shifts:
                    continue
                shift = shifts[route["shift_id"]]
                route_stops = sorted(stops[route["route_id"]], key=lambda row: row["sequence"])
                weight = sum(row["delivery"]["weight_kg"] for row in route_stops)
                volume = sum(row["delivery"]["volume_l"] for row in route_stops)
                failed = sum(row["outcome"]["status"] == "FAILED" for row in route_stops)
                completion = route_events[route["route_id"]][-1]["recorded_at"]
                routes.append(dict(route, shift=shift, vehicle=vehicles[shift["vehicle_id"]], capacity=capacities[shift["vehicle_id"]], stops=route_stops, weight_kg=weight, volume_l=volume, utilization=round(max(weight / capacities[shift["vehicle_id"]]["weight_kg"], volume / capacities[shift["vehicle_id"]]["volume_l"]) * 100, 1), failed=failed, events=route_events[route["route_id"]], actual_return=completion))
                planned_sites = [route["depot_id"]] + [row["site"]["site_id"] for row in route_stops] + [route["depot_id"]]
                baseline_sites = [route["depot_id"]] + [row["site"]["site_id"] for row in sorted(route_stops, key=lambda row: demand_order[row["delivery_id"]])] + [route["depot_id"]]
                routes[-1]["travel_minutes"] = sum(matrix[origin, destination]["travel_minutes"] for origin, destination in zip(planned_sites, planned_sites[1:]))
                routes[-1]["baseline_distance_m"] = sum(matrix[origin, destination]["distance_m"] for origin, destination in zip(baseline_sites, baseline_sites[1:]))
            total = len(deliveries)
            assigned = sum(len(row["stops"]) for row in routes)
            delivered = sum(row["status"] == "DELIVERED" for row in outcomes.values())
            on_time = sum(row["actual_arrival"] is not None and row["late_minutes"] == 0 for row in outcomes.values())
            metrics = dict(deliveries=total, assigned=assigned, unserved=total - assigned, routes=len(routes), delivered=delivered, delayed=sum(row["late_minutes"] > 0 for row in outcomes.values()), failed=sum(row["status"] == "FAILED" for row in outcomes.values()), distance_km=round(sum(row["distance_m"] for row in routes) / 1000, 1), route_hours=round(sum(row["end_minute"] - row["start_minute"] for row in routes) / 60, 1), on_time_pct=round(100 * on_time / assigned, 1) if assigned else 0, service_success_pct=round(100 * delivered / total, 1) if total else 0, utilization_pct=round(sum(row["utilization"] for row in routes) / len(routes), 1) if routes else 0)
            completed = [(route, stop) for route in routes for stop in route["stops"] if stop["outcome"]["status"] == "DELIVERED"]
            weight_capacity = sum(row["capacity"]["weight_kg"] for row in routes)
            volume_capacity = sum(row["capacity"]["volume_l"] for row in routes)
            weight_utilization = 100 * sum(row["weight_kg"] for row in routes) / weight_capacity if weight_capacity else 0
            volume_utilization = 100 * sum(row["volume_l"] for row in routes) / volume_capacity if volume_capacity else 0
            total_distance = sum(row["distance_m"] for row in routes)
            baseline_distance = sum(row["baseline_distance_m"] for row in routes)
            total_service = sum(stop["delivery"]["service_minutes"] for route in routes for stop in route["stops"])
            total_travel = sum(row["travel_minutes"] for row in routes)
            metrics.update(failed_delivery_pct=round(100 * metrics["failed"] / total, 1) if total else 0,
                           unserved_delivery_pct=round(100 * metrics["unserved"] / total, 1) if total else 0,
                           on_time_delivery_pct=round(100 * sum(stop["outcome"]["late_minutes"] == 0 for route, stop in completed) / delivered, 1) if delivered else 0,
                           weight_utilization_pct=round(weight_utilization, 1), volume_utilization_pct=round(volume_utilization, 1),
                           vehicle_utilization_pct=round(max(weight_utilization, volume_utilization), 1),
                           total_distance_m=total_distance, total_travel_minutes=total_travel, total_service_minutes=total_service,
                           total_waiting_minutes=sum(row["end_minute"] - row["start_minute"] for row in routes) - total_travel - total_service,
                           average_route_distance_km=round(total_distance / len(routes) / 1000, 2) if routes else 0,
                           average_delivery_minutes=round(sum(stop["outcome"]["actual_departure"] - route["start_minute"] for route, stop in completed) / delivered, 1) if delivered else 0)
            efficiency = dict(baseline_distance_m=baseline_distance, optimized_distance_m=total_distance,
                              distance_saved_m=baseline_distance - total_distance,
                              improvement_pct=round(100 * (baseline_distance - total_distance) / baseline_distance, 1) if baseline_distance else 0,
                              baseline="Input-order visits for the same assigned deliveries on the same vehicles, including depot return. Distance reference only; time-window and shift feasibility not asserted. Negative savings are retained. No fuel or cost savings estimate.")
            analytics = []
            delivery_dates = {row["delivery_id"]: row["delivery_date"] for row in tables["Delivery"]}
            daily = defaultdict(Counter)
            for outcome in tables["DeliveryOutcome"]:
                daily[delivery_dates[outcome["delivery_id"]]][outcome["status"]] += 1
                if outcome["late_minutes"] > 0:
                    daily[delivery_dates[outcome["delivery_id"]]]["LATE"] += 1
            shift_dates = {row["shift_id"]: row["shift_date"] for row in tables["Shift"]}
            for route in tables["Route"]:
                counts = daily[shift_dates[route["shift_id"]]]
                counts["routes"] += 1
                counts["total_distance_m"] += route["distance_m"]
                counts["route_minutes"] += route["end_minute"] - route["start_minute"]

            def period_metrics(counts):
                total = counts["DELIVERED"] + counts["FAILED"] + counts["NOT_ATTEMPTED"]
                assigned = counts["DELIVERED"] + counts["FAILED"]
                return dict(deliveries=total, assigned=assigned, delivered=counts["DELIVERED"], failed=counts["FAILED"], unserved=counts["NOT_ATTEMPTED"], delayed=counts["LATE"], routes=counts["routes"], total_distance_m=counts["total_distance_m"], distance_km=round(counts["total_distance_m"] / 1000, 1), route_hours=round(counts["route_minutes"] / 60, 1), service_success_pct=round(100 * counts["DELIVERED"] / total, 1) if total else 0, on_time_pct=round(100 * (assigned - counts["LATE"]) / assigned, 1) if assigned else 0)

            for day in dates:
                analytics.append(dict(date=day, **dict(daily[day]), metrics=period_metrics(daily[day])))
            overall = sum(daily.values(), Counter())
            return dict(run=self.current, dates=dates, date=selected_date, metrics=metrics, overall_metrics=period_metrics(overall), efficiency=efficiency, routes=routes, sites=tables["CustomerSite"], depot=sites["SITE-000000"], exceptions=exceptions, fleet=[dict(row, capacity=capacities.get(row["vehicle_id"]), available=any(shift["vehicle_id"] == row["vehicle_id"] for shift in available), shift=next((shift for shift in shifts.values() if shift["vehicle_id"] == row["vehicle_id"]), None), route_id=next((route["route_id"] for route in routes if route["vehicle"]["vehicle_id"] == row["vehicle_id"]), None)) for row in tables["Vehicle"]], analytics=analytics, totals=dict(customer_sites=len(tables["CustomerSite"]) - 1, deliveries=len(tables["Delivery"]), vehicles=len(tables["Vehicle"]), days=len(dates)), traffic=scenario)

    def anomalies(self):
        with self.lock:
            if self._anomaly_cache is None or self._anomaly_cache[0] is not self.tables:
                self._anomaly_cache = (self.tables, evaluate_anomalies(self.tables))
            return self._anomaly_cache[1]

    def quality(self):
        with self.lock:
            path = self.directory / "versions" / self.current["run_id"]
            result = json.loads((path / "validation_summary.json").read_bytes())
            result["anomaly_evaluation"] = self.anomalies()
            result["manifest"] = json.loads((path / "source_manifest.json").read_bytes())
            result["history"] = [json.loads(line) for line in (self.directory / "run_history.jsonl").read_text(encoding="utf-8").splitlines()]
            return result


def make_handler(workspace):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def respond(self, status, value, content_type="application/json", filename=None):
            body = canonical(value) if content_type == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                if parsed.path == "/api/state":
                    self.respond(200, workspace.summary(query.get("date")))
                elif parsed.path == "/api/quality":
                    self.respond(200, workspace.quality())
                elif parsed.path == "/api/table":
                    name = query.get("name", "Delivery")
                    if name not in SCHEMA:
                        raise ValueError("Unknown entity")
                    page = int(query.get("page", 1))
                    if page < 1:
                        raise ValueError("Page must be positive")
                    with workspace.lock:
                        search = query.get("search", "").lower()
                        rows = workspace.tables[name]
                        if search:
                            rows = [row for row in rows if search in " ".join(str(value) for value in row.values()).lower()]
                        self.respond(200, dict(name=name, columns=[item.name for item in SCHEMA[name]], rows=rows[(page - 1) * 25:page * 25], total=len(rows), page=page, tables=list(SCHEMA)))
                elif parsed.path == "/api/export":
                    with workspace.lock:
                        run_id = workspace.current["run_id"]
                        self.respond(200, archive(workspace.directory / "versions" / run_id), "application/zip", f"waypoint-{run_id}.zip")
                elif parsed.path == "/api/routes.csv":
                    summary = workspace.summary(query.get("date"))
                    buffer = io.StringIO(newline="")
                    writer = csv.writer(buffer)
                    writer.writerow(["route_id", "vehicle_id", "driver_id", "sequence", "delivery_id", "site", "planned_arrival", "actual_arrival", "status", "reason_code"])
                    for route in summary["routes"]:
                        for stop in route["stops"]:
                            writer.writerow([route["route_id"], route["vehicle"]["vehicle_id"], route["shift"]["driver_id"], stop["sequence"], stop["delivery"]["delivery_id"], stop["site"]["name"], stop["arrival_minute"], stop["outcome"]["actual_arrival"], stop["outcome"]["status"], stop["outcome"]["reason_code"]])
                    self.respond(200, buffer.getvalue().encode("utf-8"), "text/csv; charset=utf-8", f"dispatch-{summary['date']}.csv")
                elif parsed.path in ("/", "/index.html", "/app.js", "/styles.css", "/vendor/lucide.min.js"):
                    name = "index.html" if parsed.path == "/" else parsed.path[1:]
                    path = ROOT / "web" / name
                    if not path.exists():
                        self.respond(404, dict(error="Asset not found"))
                        return
                    content_type = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}[path.suffix]
                    self.respond(200, path.read_bytes(), content_type)
                else:
                    self.respond(404, dict(error="Not found"))
            except (ValueError, KeyError) as error:
                self.respond(400, dict(error=str(error)))
            except Exception:
                self.respond(500, dict(error="Request failed; check application data and restart the local server"))

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected_origin = f"http://{self.headers.get('Host')}"
            if origin and origin != expected_origin:
                self.respond(403, dict(error="Cross-origin writes are not allowed"))
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.respond(415, dict(error="Expected application/json"))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                if self.path == "/api/plan":
                    self.respond(200, workspace.plan(payload.get("scale", "full"), payload.get("seed", SEED), payload.get("start_date"), payload.get("end_date")))
                elif self.path == "/api/validate":
                    with workspace.lock:
                        self.respond(200, validate(workspace.tables))
                elif self.path == "/api/anomalies":
                    with workspace.lock:
                        self.respond(200, workspace.anomalies())
                else:
                    self.respond(404, dict(error="Not found"))
            except (ValueError, TypeError, KeyError) as error:
                self.respond(400, dict(error=str(error)))
            except Exception:
                self.respond(500, dict(error="Operation failed; the previous plan is preserved"))

    return Handler


def serve(port=8000, directory=ROOT / "data", scale="full", start_date=None, end_date=None):
    workspace = Workspace(directory)
    if workspace.tables is None or start_date is not None or end_date is not None:
        print(f"Building {scale} dataset, seed {SEED}...", flush=True)
        workspace.plan(scale, SEED, start_date, end_date)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(workspace))
    print(f"Waypoint is ready at http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()