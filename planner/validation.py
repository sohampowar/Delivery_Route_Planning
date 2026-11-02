"""Independent structural, arithmetic, spatial and lifecycle checks."""

import math
import re
from collections import Counter, defaultdict
from datetime import date

from .engine import distance, simulate
from .schema import SCHEMA, UNIQUE


def validate(tables):
    issues = []

    def report(kind, entity, detail):
        issues.append(dict(issue_type=kind, entity_id=entity, detail=detail))

    indexes = {name: {row.get(fields[0].name): row for row in tables.get(name, [])} for name, fields in SCHEMA.items()}
    structural = False
    for name, fields in SCHEMA.items():
        rows = tables.get(name, [])
        primary = fields[0].name
        for value, count in Counter(row.get(primary) for row in rows).items():
            if count > 1:
                report("DUPLICATE_PK", value, name)
                structural = True
        for keys in UNIQUE.get(name, []):
            counts = Counter(tuple(row.get(key) for key in keys) for row in rows if all(row.get(key) is not None for key in keys))
            for value, count in counts.items():
                if count > 1:
                    report("STOP_DUPLICATED" if name == "RouteStop" and keys == ("delivery_id",) else "DUPLICATE_KEY", str(value), f"{name}: {keys}")
        for row in rows:
            entity = row.get(primary)
            for item in fields:
                value = row.get(item.name)
                if value is None:
                    if item.required:
                        report("REQUIRED_FIELD", entity, item.name)
                        structural = True
                    continue
                expected = {"TEXT": str, "INTEGER": int, "REAL": (int, float)}[item.type]
                if not isinstance(value, expected) or isinstance(value, bool):
                    report("FIELD_TYPE", entity, item.name)
                    structural = True
                if item.allowed and value not in item.allowed:
                    report("INVALID_ENUM", entity, item.name)
                if item.reference:
                    parent = item.reference.split(".")[0]
                    if value not in indexes[parent]:
                        report("FOREIGN_KEY", entity, f"{item.name} -> {value}")
                        structural = True
            for key in ("effective_from", "effective_to", "created_date", "delivery_date", "shift_date", "scenario_date"):
                if row.get(key) is not None:
                    try:
                        date.fromisoformat(row[key])
                    except (ValueError, TypeError):
                        report("INVALID_DATE", entity, key)
                        structural = True
            if row.get("effective_to") and row["effective_to"] < row["effective_from"]:
                report("DATE_ORDER", entity, "Effective period reversed")
            for key in ("weight_kg", "volume_l", "service_minutes"):
                if isinstance(row.get(key), (int, float)) and row[key] <= 0:
                    report("NONPOSITIVE_VALUE", entity, key)
    if structural:
        return dict(passed=False, checks=8, issues=issues, row_counts={name: len(rows) for name, rows in tables.items()})
    for name, pattern in [("Order", r"ORD-\d{4}-\d{6,}"), ("Delivery", r"DEL-\d{4}-\d{6,}"), ("Vehicle", r"VEH-\d{6,}"), ("Driver", r"DRV-\d{6,}"), ("Route", r"RTE-\d{4}-\d{6,}")]:
        for entity in indexes[name]:
            if not re.fullmatch(pattern, entity):
                report("ID_FORMAT", entity, name)
    for site in tables["CustomerSite"]:
        if not (51.38 <= site["latitude"] <= 51.64 and -0.30 <= site["longitude"] <= 0.13):
            report("COORDINATE_OUTSIDE_REGION", site["site_id"], "Outside synthetic Greater London bounds")
    for shift in tables["Shift"]:
        if not 0 <= shift["start_minute"] < shift["end_minute"] <= 1440:
            report("SHIFT_RANGE", shift["shift_id"], "Invalid driver shift")
    windows = {row["delivery_id"]: row for row in tables["TimeWindow"]}
    outcomes = {row["delivery_id"]: row for row in tables["DeliveryOutcome"]}
    assigned = Counter(row["delivery_id"] for row in tables["RouteStop"])
    for delivery in tables["Delivery"]:
        delivery_id = delivery["delivery_id"]
        if delivery_id not in windows:
            report("MISSING_WINDOW", delivery_id, "A delivery needs one window")
        elif not 480 <= windows[delivery_id]["start_minute"] < windows[delivery_id]["end_minute"] <= 1020:
            report("WINDOW_RANGE", delivery_id, "Invalid time window")
        if indexes["Order"][delivery["order_id"]]["created_date"] > delivery["delivery_date"]:
            report("DATE_ORDER", delivery_id, "Order created after delivery")
        outcome = outcomes.get(delivery_id)
        if not outcome:
            report("MISSING_OUTCOME", delivery_id, "Every demand must be accounted for")
        elif not assigned[delivery_id] and outcome["status"] != "NOT_ATTEMPTED":
            report("STOP_OMITTED", delivery_id, "Assigned outcome without a route stop")
        elif assigned[delivery_id] and outcome["status"] == "NOT_ATTEMPTED":
            report("ASSIGNMENT_MISMATCH", delivery_id, "Assigned demand marked unserved")
        if outcome and outcome["status"] != "DELIVERED" and not outcome["reason_code"]:
            report("MISSING_REASON", delivery_id, "Failure/unserved needs a reason")
    matrices = {(row["scenario_id"], row["origin_id"], row["destination_id"]): row for row in tables["TravelTimeMatrix"]}
    scenarios = {row["scenario_date"]: row for row in tables["TrafficScenario"]}
    scenario_dates = sorted(date.fromisoformat(day).toordinal() for day in scenarios)
    if not scenario_dates or scenario_dates[-1] - scenario_dates[0] + 1 != len(scenario_dates):
        report("PLANNING_DATE_GAP", "TrafficScenario", "Planning dates must cover a nonempty inclusive period")
    for name, key in [("Delivery", "delivery_date"), ("Shift", "shift_date")]:
        for row in tables[name]:
            if row[key] not in scenarios:
                report("MISSING_SCENARIO", row[SCHEMA[name][0].name], row[key])
    for row in tables["TravelTimeMatrix"]:
        origin, destination = indexes["CustomerSite"][row["origin_id"]], indexes["CustomerSite"][row["destination_id"]]
        metres = distance(origin, destination)
        factor = indexes["TrafficScenario"][row["scenario_id"]]["factor"]
        if row["distance_m"] != metres or row["travel_minutes"] != math.ceil(metres / 450 * factor):
            report("MATRIX_ARITHMETIC", row["matrix_id"], "Travel must reconcile with coordinates and scenario")
    stops_by_route = defaultdict(list)
    for stop in tables["RouteStop"]:
        stops_by_route[stop["route_id"]].append(stop)
    for route in tables["Route"]:
        route_id = route["route_id"]
        shift = indexes["Shift"][route["shift_id"]]
        vehicle = indexes["Vehicle"][shift["vehicle_id"]]
        valid_capacities = [row for row in tables["VehicleCapacity"] if row["vehicle_id"] == vehicle["vehicle_id"] and row["effective_from"] <= shift["shift_date"] and (row["effective_to"] is None or row["effective_to"] >= shift["shift_date"])]
        stops = sorted(stops_by_route[route_id], key=lambda row: row["sequence"])
        if len(valid_capacities) != 1:
            report("CAPACITY_EFFECTIVE_DATE", route_id, "Exactly one capacity record must apply")
        else:
            for unit in ("weight_kg", "volume_l"):
                total = sum(indexes["Delivery"][stop["delivery_id"]][unit] for stop in stops)
                if total > valid_capacities[0][unit]:
                    report("ROUTE_EXCEEDS_CAPACITY", route_id, f"{unit}: {total} > {valid_capacities[0][unit]}")
        if route["start_minute"] < shift["start_minute"] or route["end_minute"] > shift["end_minute"]:
            report("DRIVER_SHIFT_EXCEEDED", route_id, "Planned route outside assigned driver shift")
        if vehicle["state"] != "ACTIVE" or indexes["Driver"][shift["driver_id"]]["state"] != "ACTIVE":
            report("INACTIVE_RESOURCE", route_id, "Driver and vehicle must be active")
        scenario = scenarios.get(shift["shift_date"])
        if not scenario:
            report("MISSING_SCENARIO", route_id, shift["shift_date"])
            continue
        previous_site, previous_departure, metres = route["depot_id"], route["start_minute"], 0
        if [row["sequence"] for row in stops] != list(range(1, len(stops) + 1)):
            report("STOP_SEQUENCE", route_id, "Stop sequence must be contiguous")
        for stop in stops:
            delivery = indexes["Delivery"][stop["delivery_id"]]
            site_id = indexes["Order"][delivery["order_id"]]["site_id"]
            if stop["stop_id"] != f"STOP-{route_id}-{stop['sequence']:03d}":
                report("ID_FORMAT", stop["stop_id"], "Stop ID must embed route and sequence")
            leg = matrices.get((scenario["scenario_id"], previous_site, site_id))
            if not leg:
                report("MISSING_MATRIX_LEG", stop["stop_id"], f"{previous_site} -> {site_id}")
            else:
                metres += leg["distance_m"]
                if stop["arrival_minute"] < previous_departure + leg["travel_minutes"]:
                    report("ARRIVAL_BEFORE_PREVIOUS_DEPARTURE", stop["stop_id"], "Arrival precedes previous departure plus travel")
            if stop["departure_minute"] != stop["arrival_minute"] + delivery["service_minutes"]:
                report("SERVICE_ARITHMETIC", stop["stop_id"], "Departure must equal arrival + service")
            window = windows.get(delivery["delivery_id"])
            if window and not window["start_minute"] <= stop["arrival_minute"] <= window["end_minute"]:
                report("TIME_WINDOW_VIOLATION", stop["stop_id"], "Service must start within window")
            if delivery["delivery_date"] != shift["shift_date"]:
                report("DELIVERY_DATE_MISMATCH", stop["stop_id"], "Route date differs from demand")
            for restriction in tables["SiteVehicleRestriction"]:
                if restriction["site_id"] == site_id and restriction["vehicle_type"] == vehicle["vehicle_type"] and restriction["effective_from"] <= shift["shift_date"] and (restriction["effective_to"] is None or restriction["effective_to"] >= shift["shift_date"]):
                    report("RESTRICTED_VEHICLE", stop["stop_id"], "Vehicle class prohibited at site")
            previous_site, previous_departure = site_id, stop["departure_minute"]
        leg = matrices.get((scenario["scenario_id"], previous_site, route["depot_id"]))
        if leg:
            metres += leg["distance_m"]
            if route["end_minute"] < previous_departure + leg["travel_minutes"]:
                report("RETURN_TIME", route_id, "Depot return missing from route end")
        if metres != route["distance_m"]:
            report("DISTANCE_RECONCILIATION", route_id, f"Expected {metres} metres")
    for entity_name, event_name, owner, allowed_paths in [
        ("Delivery", "DeliveryEvent", "delivery_id", [("CREATED", "UNSERVED"), ("CREATED", "ASSIGNED", "DELIVERED"), ("CREATED", "ASSIGNED", "FAILED")]),
        ("Route", "RouteEvent", "route_id", [("PLANNED", "DISPATCHED", "COMPLETED")]),
    ]:
        events_by_owner = defaultdict(list)
        for event in tables[event_name]:
            events_by_owner[event[owner]].append(event)
        for entity in indexes[entity_name]:
            events = sorted(events_by_owner[entity], key=lambda row: row["sequence"])
            if tuple(row["state"] for row in events) not in allowed_paths or [row["sequence"] for row in events] != list(range(1, len(events) + 1)):
                report("LIFECYCLE_TRANSITION", entity, event_name)
            if [row["recorded_at"] for row in events] != sorted(row["recorded_at"] for row in events):
                report("EVENT_DATE_ORDER", entity, event_name)
    if not issues:
        replay = dict(tables, DeliveryOutcome=[], DeliveryEvent=[], RouteEvent=[])
        simulate(replay)
        for name in ("DeliveryOutcome", "DeliveryEvent", "RouteEvent"):
            if replay[name] != tables[name]:
                report("SIMULATION_RECONCILIATION", name, "Stored results differ from rule-derived replay")
    return dict(passed=not issues, checks=29, issues=issues, row_counts={name: len(rows) for name, rows in tables.items()})


def anomaly_cases(tables):
    routes = {row["route_id"]: row for row in tables["Route"]}
    grouped = defaultdict(list)
    for stop in tables["RouteStop"]:
        grouped[stop["route_id"]].append(stop)
    route_id = next((key for key, stops in grouped.items() if len(stops) >= 2), None)
    if route_id is None:
        return [], []
    route = routes[route_id]
    stops = sorted(grouped[route_id], key=lambda row: row["sequence"])
    shift = next(row for row in tables["Shift"] if row["shift_id"] == route["shift_id"])
    capacity = next(row for row in tables["VehicleCapacity"] if row["vehicle_id"] == shift["vehicle_id"])
    definitions = [
        ("ROUTE_EXCEEDS_CAPACITY", "VehicleCapacity", "update", capacity["capacity_id"], {"weight_kg": 1}),
        ("STOP_OMITTED", "RouteStop", "delete", stops[0]["stop_id"], {}),
        ("STOP_DUPLICATED", "RouteStop", "insert", None, dict(stops[0], stop_id=f"STOP-{route_id}-{len(stops) + 1:03d}", sequence=len(stops) + 1)),
        ("ARRIVAL_BEFORE_PREVIOUS_DEPARTURE", "RouteStop", "update", stops[1]["stop_id"], {"arrival_minute": stops[0]["departure_minute"] - 5, "departure_minute": stops[0]["departure_minute"] - 5 + next(row["service_minutes"] for row in tables["Delivery"] if row["delivery_id"] == stops[1]["delivery_id"])}),
        ("DRIVER_SHIFT_EXCEEDED", "Route", "update", route_id, {"end_minute": shift["end_minute"] + 30}),
        ("COORDINATE_OUTSIDE_REGION", "CustomerSite", "update", "SITE-000001", {"latitude": 52.3}),
    ]
    inputs, truth = [], []
    for index, (kind, table, operation, entity_id, values) in enumerate(definitions, 1):
        case_id = f"CASE-{index:03d}"
        inputs.append(dict(case_id=case_id, table=table, operation=operation, entity_id=entity_id, values=values))
        actions = {"ROUTE_EXCEEDS_CAPACITY": "REALLOCATE_LOAD", "STOP_OMITTED": "RESTORE_STOP", "STOP_DUPLICATED": "REMOVE_DUPLICATE", "ARRIVAL_BEFORE_PREVIOUS_DEPARTURE": "REPLAN_TIMING", "DRIVER_SHIFT_EXCEEDED": "REASSIGN_OR_SHORTEN_ROUTE", "COORDINATE_OUTSIDE_REGION": "CORRECT_COORDINATES"}
        truth.append(dict(case_id=case_id, issue_type=kind, ground_truth="ANOMALY", expected_classification=kind, expected_action=actions[kind], entity_id=entity_id or values["stop_id"]))
    return inputs, truth


def evaluate_anomalies(tables):
    cases, truth = anomaly_cases(tables)
    labels = {row["case_id"]: row for row in truth}
    baseline = validate(tables)
    baseline_types = {row["issue_type"] for row in baseline["issues"]}
    results = []
    for case in cases:
        label = labels[case["case_id"]]
        result = validate(apply_case(tables, case))
        detected = sorted({row["issue_type"] for row in result["issues"]})
        results.append(dict(case_id=case["case_id"], expected=label["issue_type"], expected_action=label["expected_action"], detected=detected, passed=label["issue_type"] in detected, issues=result["issues"]))
    true_positives = sum(row["passed"] for row in results)
    false_negatives = len(results) - true_positives
    false_positives = sum(label["issue_type"] in baseline_types for label in truth)
    return dict(cases=len(results), detected=true_positives, results=results,
                true_positives=true_positives, false_positives=false_positives,
                false_negatives=false_negatives, true_negatives=len(truth) - false_positives,
                detection_rate_pct=round(100 * true_positives / len(results), 1) if results else 0,
                precision_pct=round(100 * true_positives / (true_positives + false_positives), 1) if true_positives + false_positives else None,
                baseline_passed=baseline["passed"], baseline_issues=baseline["issues"],
                note="Targeted class checks: one positive patch and one clean-baseline control per required class. Secondary patch violations are retained, not scored. Not a population performance estimate." if results else "Anomaly evaluation unavailable: no route has at least two stops. No anomaly checks were scored.")


def apply_case(tables, case):
    modified = dict(tables)
    table = case["table"]
    primary = SCHEMA[table][0].name
    modified[table] = [dict(row) for row in tables[table]]
    if case["operation"] == "insert":
        modified[table].append(case["values"])
    elif case["operation"] == "delete":
        modified[table] = [row for row in modified[table] if row[primary] != case["entity_id"]]
        removed = next(row for row in tables[table] if row[primary] == case["entity_id"])
        modified["DeliveryOutcome"] = [dict(row, stop_id=None) if row["delivery_id"] == removed["delivery_id"] else row for row in tables["DeliveryOutcome"]]
    else:
        for row in modified[table]:
            if row[primary] == case["entity_id"]:
                row.update(case["values"])
    return modified