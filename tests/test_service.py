import json
import io
import tempfile
import threading
import unittest
import zipfile
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from planner.engine import generate, plan, simulate
from planner.service import Workspace, make_handler


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.workspace = Workspace(cls.temporary.name)
        cls.workspace.plan("starter")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.workspace))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def request(self, path, payload=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        return urlopen(request, timeout=30)

    def test_summary_reconciliation(self):
        with self.request("/api/state") as response:
            result = json.load(response)
        metrics = result["metrics"]
        self.assertEqual(metrics["assigned"] + metrics["unserved"], metrics["deliveries"])
        self.assertEqual(metrics["delivered"] + metrics["failed"], metrics["assigned"])
        self.assertEqual(len(result["fleet"]), 12)
        self.assertNotIn("ground_truth", result)

    def test_calendar_control_is_unrestricted(self):
        with self.request("/") as response:
            markup = response.read().decode("utf-8")
        self.assertIn('<input id="date-select"', markup)
        self.assertIn('type="date" min="0001-01-01" max="9999-12-31"', markup)
        self.assertNotIn('<select id="date-select"', markup)

    def test_period_kpis(self):
        summary = self.workspace.summary()
        overall = summary["overall_metrics"]
        for key in ("deliveries", "assigned", "delivered", "failed", "unserved", "delayed", "routes", "total_distance_m"):
            self.assertEqual(overall[key], sum(day["metrics"][key] for day in summary["analytics"]))
        self.assertEqual(overall["deliveries"], 160)
        self.assertEqual(overall["service_success_pct"], round(100 * overall["delivered"] / 160, 1))
        self.assertEqual(summary["metrics"]["delayed"], summary["analytics"][0]["metrics"]["delayed"])

    def test_empty_day_and_period_reporting(self):
        tables = generate(start_date="2028-02-28", end_date="2028-03-01")
        removed = {row["delivery_id"] for row in tables["Delivery"] if row["delivery_date"] == "2028-02-29"}
        tables["Delivery"] = [row for row in tables["Delivery"] if row["delivery_id"] not in removed]
        tables["TimeWindow"] = [row for row in tables["TimeWindow"] if row["delivery_id"] not in removed]
        for name in ("Route", "RouteStop", "DeliveryOutcome", "DeliveryEvent", "RouteEvent"):
            tables[name] = []
        plan(tables)
        simulate(tables)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            with patch("planner.service.generate", return_value=tables):
                workspace.plan("starter", start_date="2028-02-28", end_date="2028-03-01")
            workspace = Workspace(directory)
            summary = workspace.summary("2028-02-29")
            self.assertEqual(summary["dates"], ["2028-02-28", "2028-02-29", "2028-03-01"])
            self.assertTrue(all(value == 0 for value in summary["metrics"].values()))
            self.assertEqual(summary["routes"], [])
            self.assertEqual(summary["overall_metrics"]["deliveries"], len(tables["Delivery"]))
            self.assertEqual(summary["analytics"][1]["metrics"]["deliveries"], 0)
            for name in ("Delivery", "TimeWindow", "Route", "RouteStop", "DeliveryOutcome", "DeliveryEvent", "RouteEvent"):
                tables[name] = []
            workspace.tables = tables
            self.assertTrue(all(value == 0 for value in workspace.summary()["overall_metrics"].values()))

    def test_custom_period_history_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            first = workspace.plan("starter", start_date="2026-11-02", end_date="2026-11-02")
            second = workspace.plan("starter", start_date="2026-11-03", end_date="2026-11-03")
            self.assertNotEqual(first["run_id"], second["run_id"])
            restored = Workspace(directory)
            self.assertEqual(restored.summary()["dates"], ["2026-11-03"])
            quality = restored.quality()
            self.assertEqual(quality["manifest"]["start_date"], "2026-11-03")
            self.assertEqual(quality["manifest"]["days"], 1)
            self.assertEqual(quality["history"][0]["start_date"], "2026-11-02")
            self.assertTrue((workspace.directory / "versions" / first["run_id"] / "planning.sqlite").exists())

    def test_date_range_api_workflow(self):
        periods = [("2030-04-04", "2030-04-10", 7), ("2030-06-01", "2030-06-30", 30), ("2031-07-01", "2031-07-31", 31), ("2029-04-28", "2029-05-03", 6), ("2029-12-29", "2030-01-03", 6), ("2028-02-27", "2028-03-02", 5)]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(workspace))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            run_ids = set()
            try:
                for start, end, days in periods:
                    with self.subTest(start=start, end=end):
                        payload = dict(scale="starter", seed=42, start_date=start, end_date=end)
                        request = Request(base + "/api/plan", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                        with urlopen(request, timeout=60) as response:
                            run = json.load(response)
                        self.assertEqual(run["days"], days)
                        self.assertNotIn(run["run_id"], run_ids)
                        run_ids.add(run["run_id"])
                        daily_total = 0
                        for offset in range(days):
                            day = (date.fromisoformat(start) + timedelta(days=offset)).isoformat()
                            with urlopen(base + "/api/state?date=" + day, timeout=30) as response:
                                summary = json.load(response)
                            self.assertEqual(summary["date"], day)
                            self.assertEqual(len(summary["dates"]), days)
                            self.assertEqual(summary["dates"][0], start)
                            self.assertEqual(summary["dates"][-1], end)
                            self.assertEqual(summary["metrics"]["deliveries"], summary["metrics"]["delivered"] + summary["metrics"]["failed"] + summary["metrics"]["unserved"])
                            self.assertTrue(summary["routes"])
                            daily_total += summary["metrics"]["deliveries"]
                        self.assertEqual(summary["overall_metrics"]["deliveries"], daily_total)
                        with urlopen(base + "/api/export", timeout=30) as response:
                            with zipfile.ZipFile(io.BytesIO(response.read())) as bundle:
                                manifest = json.loads(bundle.read("source_manifest.json"))
                                dataset = json.loads(bundle.read("dataset.json"))
                        self.assertEqual((manifest["start_date"], manifest["end_date"], manifest["days"]), (start, end, days))
                        self.assertEqual(len(dataset["DeliveryOutcome"]), daily_total)
                        self.assertTrue(dataset["DeliveryEvent"])
                        self.assertTrue(dataset["RouteEvent"])
                restored = Workspace(directory)
                self.assertEqual(restored.current, workspace.current)
                self.assertEqual(len(restored.quality()["history"]), len(periods))
                current = dict(workspace.current)
                request = Request(base + "/api/plan", data=json.dumps(dict(start_date="2030-02-29", end_date="2030-03-01")).encode(), headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=30)
                self.assertEqual(caught.exception.code, 400)
                self.assertEqual(workspace.current, current)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_invalid_period_requests(self):
        for payload in [{"start_date": "2026-11-02"}, {"start_date": "2026-11-03", "end_date": "2026-11-02"}, {"start_date": "bad", "end_date": "bad"}]:
            with self.subTest(payload=payload), self.assertRaises(HTTPError) as caught:
                self.request("/api/plan", payload)
            self.assertEqual(caught.exception.code, 400)

    def test_operational_kpis_and_baseline(self):
        summary = self.workspace.summary()
        metrics = summary["metrics"]
        routes = summary["routes"]
        completed = [(route, stop) for route in routes for stop in route["stops"] if stop["outcome"]["status"] == "DELIVERED"]
        self.assertEqual(metrics["failed_delivery_pct"], round(100 * metrics["failed"] / metrics["deliveries"], 1))
        self.assertEqual(metrics["unserved_delivery_pct"], round(100 * metrics["unserved"] / metrics["deliveries"], 1))
        self.assertEqual(metrics["on_time_delivery_pct"], round(100 * sum(stop["outcome"]["late_minutes"] == 0 for route, stop in completed) / len(completed), 1))
        self.assertEqual(metrics["average_delivery_minutes"], round(sum(stop["outcome"]["actual_departure"] - route["start_minute"] for route, stop in completed) / len(completed), 1))
        self.assertEqual(metrics["weight_utilization_pct"], round(100 * sum(row["weight_kg"] for row in routes) / sum(row["capacity"]["weight_kg"] for row in routes), 1))
        tables = self.workspace.tables
        scenario_id = summary["traffic"]["scenario_id"]
        matrix = {(row["origin_id"], row["destination_id"]): row for row in tables["TravelTimeMatrix"] if row["scenario_id"] == scenario_id}
        baseline_distance = travel_minutes = 0
        for route in routes:
            planned = [route["depot_id"]] + [row["site"]["site_id"] for row in route["stops"]] + [route["depot_id"]]
            baseline = [route["depot_id"]] + [row["site"]["site_id"] for row in sorted(route["stops"], key=lambda row: row["delivery_id"])] + [route["depot_id"]]
            travel_minutes += sum(matrix[origin, target]["travel_minutes"] for origin, target in zip(planned, planned[1:]))
            baseline_distance += sum(matrix[origin, target]["distance_m"] for origin, target in zip(baseline, baseline[1:]))
        self.assertEqual(metrics["total_travel_minutes"], travel_minutes)
        self.assertEqual(summary["efficiency"]["baseline_distance_m"], baseline_distance)
        self.assertEqual(summary["efficiency"]["distance_saved_m"], baseline_distance - metrics["total_distance_m"])
        self.assertEqual(metrics["total_waiting_minutes"] + metrics["total_service_minutes"] + travel_minutes, sum(row["end_minute"] - row["start_minute"] for row in routes))

    def test_zero_assignment_metrics_and_explanations(self):
        tables = self.workspace.tables
        isolated = Workspace(self.temporary.name)
        isolated.tables = dict(tables, Route=[], RouteStop=[], RouteEvent=[])
        isolated.tables["DeliveryOutcome"] = [dict(row, stop_id=None, status="NOT_ATTEMPTED", actual_arrival=None, actual_departure=None, late_minutes=0, reason_code="NO_FEASIBLE_VEHICLE") for row in tables["DeliveryOutcome"]]
        summary = isolated.summary()
        self.assertEqual(summary["metrics"]["unserved_delivery_pct"], 100)
        for key in ("on_time_delivery_pct", "vehicle_utilization_pct", "average_delivery_minutes", "average_route_distance_km", "total_travel_minutes"):
            self.assertEqual(summary["metrics"][key], 0)
        self.assertEqual(summary["efficiency"]["improvement_pct"], 0)
        self.assertTrue(all(row["allocation"]["vehicle_checks"] for row in summary["exceptions"]))

    def test_anomaly_cache_follows_snapshot(self):
        isolated = Workspace(self.temporary.name)
        with patch("planner.service.evaluate_anomalies", return_value={"results": []}) as evaluator:
            isolated.anomalies()
            isolated.anomalies()
            self.assertEqual(evaluator.call_count, 1)
            isolated.tables = dict(isolated.tables)
            isolated.anomalies()
            self.assertEqual(evaluator.call_count, 2)

    def test_quality_and_anomalies(self):
        with self.request("/api/validate", {}) as response:
            self.assertTrue(json.load(response)["passed"])
        with self.request("/api/anomalies", {}) as response:
            evaluation = json.load(response)
            self.assertTrue(all(row["passed"] for row in evaluation["results"]))
            self.assertEqual(evaluation["true_positives"], 6)
            self.assertEqual(evaluation["false_positives"], 0)
        with self.request("/api/quality") as response:
            self.assertTrue(json.load(response)["manifest"]["table_sha256"])

    def test_invalid_requests(self):
        for path, payload, expected in [("/api/plan", {"scale": "enormous"}, 400), ("/api/plan", {"seed": -1}, 400), ("/api/table?name=bogus", None, 400), ("/api/table?page=0", None, 400), ("/api/state?date=1900-01-01", None, 400), ("/../requirements.txt", None, 404)]:
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as caught:
                    self.request(path, payload)
                self.assertEqual(caught.exception.code, expected)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/plan", {}, "https://untrusted.example")
        self.assertEqual(caught.exception.code, 403)

    def test_tables_and_downloads(self):
        with self.request("/api/table?name=Delivery&page=2") as response:
            result = json.load(response)
            self.assertEqual(len(result["rows"]), 25)
            self.assertEqual(result["total"], 160)
        with self.request("/api/export") as response:
            self.assertEqual(response.read(2), b"PK")
        with self.request("/api/routes.csv") as response:
            self.assertIn(b"route_id,vehicle_id,driver_id", response.read())

    def test_reload_and_immutable_versions(self):
        restored = Workspace(self.temporary.name)
        self.assertEqual(restored.current, self.workspace.current)
        self.assertEqual(restored.tables, self.workspace.tables)
        self.assertEqual(restored.plan("starter"), self.workspace.current)


if __name__ == "__main__":
    unittest.main()