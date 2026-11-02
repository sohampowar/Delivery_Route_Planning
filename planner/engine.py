"""Seeded demand, constrained routing, and rule-derived delivery simulation."""

import math
import random
from collections import defaultdict
from datetime import date, timedelta

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .schema import SCHEMA

SEED = 20261022
SCALES = {"starter": (60, 160, 12, 7), "full": (500, 2000, 100, 30)}


def distance(origin, destination):
    latitude = math.radians((origin["latitude"] + destination["latitude"]) / 2)
    north = (origin["latitude"] - destination["latitude"]) * 111320
    east = (origin["longitude"] - destination["longitude"]) * 111320 * math.cos(latitude)
    return round(math.hypot(north, east) * 1.22)


def planning_period(scale, start_date=None, end_date=None):
    if scale not in SCALES:
        raise ValueError("Scale must be starter or full")
    if (start_date is None) != (end_date is None):
        raise ValueError("Provide both start_date and end_date")
    try:
        start = date.today() if start_date is None else date.fromisoformat(start_date)
        end = start + timedelta(days=SCALES[scale][3] - 1) if end_date is None else date.fromisoformat(end_date)
        if start_date is not None and (start.isoformat() != start_date or end.isoformat() != end_date):
            raise ValueError("Noncanonical date")
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Dates must use YYYY-MM-DD") from None
    if end < start:
        raise ValueError("End date must be on or after start date")
    return start, end


def generate(scale="starter", seed=SEED, start_date=None, end_date=None):
    if scale not in SCALES:
        raise ValueError("Scale must be starter or full")
    site_count, delivery_count, vehicle_count, default_days = SCALES[scale]
    start, end = planning_period(scale, start_date, end_date)
    days = (end - start).days + 1
    delivery_count = (delivery_count * days + default_days - 1) // default_days
    effective_from = date.fromordinal(max(date.min.toordinal(), start.toordinal() - 2)).isoformat()
    rng = random.Random(seed)
    tables = {name: [] for name in SCHEMA}
    tables["CustomerSite"].append(dict(site_id="SITE-000000", name="Bermondsey Distribution Hub", site_type="DEPOT", latitude=51.497, longitude=-0.075, effective_from=effective_from, effective_to=None, state="ACTIVE"))
    districts = ["Southwark", "Camden", "Greenwich", "Islington", "Westminster", "Hackney"]
    for index in range(1, site_count + 1):
        site_id = f"SITE-{index:06d}"
        tables["CustomerSite"].append(dict(site_id=site_id, name=f"{districts[index % 6]} Receiving {index:03d}", site_type="CUSTOMER", latitude=round(rng.uniform(51.40, 51.61), 6), longitude=round(rng.uniform(-0.26, 0.08), 6), effective_from=effective_from, effective_to=None, state="ACTIVE"))
        if index % 5 == 0:
            tables["SiteVehicleRestriction"].append(dict(restriction_id=f"RES-{index:06d}", site_id=site_id, vehicle_type="TRUCK", effective_from=effective_from, effective_to=None))
    for index in range(1, vehicle_count + 1):
        vehicle_id = f"VEH-{index:06d}"
        driver_id = f"DRV-{index:06d}"
        vehicle_type = "TRUCK" if index % 3 == 0 else "VAN"
        tables["Vehicle"].append(dict(vehicle_id=vehicle_id, name=f"{'Rigid' if vehicle_type == 'TRUCK' else 'Transit'} {index:03d}", vehicle_type=vehicle_type, state="ACTIVE"))
        tables["VehicleCapacity"].append(dict(capacity_id=f"CAP-{index:06d}", vehicle_id=vehicle_id, weight_kg=1800 if vehicle_type == "TRUCK" else 750, volume_l=9000 if vehicle_type == "TRUCK" else 4000, effective_from=effective_from, effective_to=None))
        tables["Driver"].append(dict(driver_id=driver_id, name=f"Driver {index:03d}", state="ACTIVE"))
        for day in range(days):
            tables["Shift"].append(dict(shift_id=f"SHF-{day + 1:03d}-{index:06d}", driver_id=driver_id, vehicle_id=vehicle_id, shift_date=(start + timedelta(days=day)).isoformat(), start_minute=480 + (index % 3) * 15, end_minute=1020 - (index % 2) * 30))
    for index in range(1, delivery_count + 1):
        day = (index - 1) % days
        delivery_date = (start + timedelta(days=day)).isoformat()
        order_id, delivery_id = f"ORD-{delivery_date[:4]}-{index:06d}", f"DEL-{delivery_date[:4]}-{index:06d}"
        site_id = f"SITE-{rng.randint(1, site_count):06d}"
        tables["Order"].append(dict(order_id=order_id, site_id=site_id, created_date=date.fromordinal(max(date.min.toordinal(), start.toordinal() + day - 2)).isoformat(), state="CONFIRMED"))
        priority = rng.choices(["STANDARD", "HIGH", "CRITICAL"], [70, 23, 7])[0]
        tables["Delivery"].append(dict(delivery_id=delivery_id, order_id=order_id, delivery_date=delivery_date, weight_kg=round(rng.triangular(15, 240, 55)), volume_l=rng.randint(80, 650), service_minutes=rng.randint(8, 24), priority=priority, state="CREATED"))
        window_start = rng.choice([510, 570, 630, 690, 750, 810])
        tables["TimeWindow"].append(dict(window_id=f"WIN-{index:06d}", delivery_id=delivery_id, start_minute=window_start, end_minute=window_start + rng.choice([90, 120, 180])))
    sites = {row["site_id"]: row for row in tables["CustomerSite"]}
    orders = {row["order_id"]: row for row in tables["Order"]}
    for day in range(days):
        scenario_id = f"TRF-{day + 1:03d}"
        scenario_date = (start + timedelta(days=day)).isoformat()
        weather = ["CLEAR", "RAIN", "CLEAR", "ROADWORKS", "CLEAR", "RAIN", "CLEAR"][day % 7]
        factor = {"CLEAR": 1.0, "RAIN": 1.25, "ROADWORKS": 1.5}[weather]
        tables["TrafficScenario"].append(dict(scenario_id=scenario_id, scenario_date=scenario_date, factor=factor, weather=weather))
        nodes = sorted({"SITE-000000"} | {orders[row["order_id"]]["site_id"] for row in tables["Delivery"] if row["delivery_date"] == scenario_date})
        for origin in nodes:
            for destination in nodes:
                metres = distance(sites[origin], sites[destination])
                tables["TravelTimeMatrix"].append(dict(matrix_id=f"MAT-{day + 1:03d}-{origin[5:]}-{destination[5:]}", scenario_id=scenario_id, origin_id=origin, destination_id=destination, distance_m=metres, travel_minutes=math.ceil(metres / 450 * factor)))
    plan(tables)
    simulate(tables)
    return tables


def candidate_shifts(available, day_index, demand_count):
    if not available:
        return []
    fleet_size = min(len(available), max(3, math.ceil(demand_count / 7)))
    offset = day_index * fleet_size % len(available)
    return (available[offset:] + available[:offset])[:fleet_size]


def effective_on(row, day):
    return row["effective_from"] <= day and (row["effective_to"] is None or day <= row["effective_to"])


def daily_resources(tables, day):
    vehicles = {row["vehicle_id"]: row for row in tables["Vehicle"]}
    drivers = {row["driver_id"]: row for row in tables["Driver"]}
    capacity_records = defaultdict(list)
    for row in tables["VehicleCapacity"]:
        if effective_on(row, day):
            capacity_records[row["vehicle_id"]].append(row)
    capacities = {vehicle_id: rows[0] for vehicle_id, rows in capacity_records.items() if len(rows) == 1}
    available = [row for row in tables["Shift"] if row["shift_date"] == day and vehicles[row["vehicle_id"]]["state"] == "ACTIVE" and drivers[row["driver_id"]]["state"] == "ACTIVE" and row["vehicle_id"] in capacities]
    restricted = {(row["site_id"], row["vehicle_type"]) for row in tables["SiteVehicleRestriction"] if effective_on(row, day)}
    return available, capacities, restricted


def allocation_explanations(tables, scenario):
    demands = [row for row in tables["Delivery"] if row["delivery_date"] == scenario["scenario_date"]]
    vehicles = {row["vehicle_id"]: row for row in tables["Vehicle"]}
    drivers = {row["driver_id"]: row for row in tables["Driver"]}
    orders = {row["order_id"]: row for row in tables["Order"]}
    windows = {row["delivery_id"]: row for row in tables["TimeWindow"]}
    shifts = [row for row in tables["Shift"] if row["shift_date"] == scenario["scenario_date"]]
    available, capacities, restricted = daily_resources(tables, scenario["scenario_date"])
    day_index = sorted(row["scenario_date"] for row in tables["TrafficScenario"]).index(scenario["scenario_date"])
    selected = {row["shift_id"] for row in candidate_shifts(available, day_index, len(demands))}
    matrix = {(row["origin_id"], row["destination_id"]): row for row in tables["TravelTimeMatrix"] if row["scenario_id"] == scenario["scenario_id"]}
    assigned = {row["delivery_id"] for row in tables["RouteStop"]}
    explanations = {}
    for delivery in demands:
        if delivery["delivery_id"] in assigned:
            continue
        site_id = orders[delivery["order_id"]]["site_id"]
        window = windows[delivery["delivery_id"]]
        checks = []
        for vehicle in vehicles.values():
            vehicle_shifts = [row for row in shifts if row["vehicle_id"] == vehicle["vehicle_id"]]
            reasons = []
            if vehicle["state"] != "ACTIVE":
                reasons.append("VEHICLE_INACTIVE")
            if not vehicle_shifts:
                reasons.append("NO_SHIFT")
            elif not any(drivers[row["driver_id"]]["state"] == "ACTIVE" for row in vehicle_shifts):
                reasons.append("DRIVER_INACTIVE")
            capacity = capacities.get(vehicle["vehicle_id"])
            if capacity is None:
                reasons.append("CAPACITY_UNAVAILABLE")
            if capacity is not None and delivery["weight_kg"] > capacity["weight_kg"]:
                reasons.append("WEIGHT_CAPACITY_EXCEEDED")
            if capacity is not None and delivery["volume_l"] > capacity["volume_l"]:
                reasons.append("VOLUME_CAPACITY_EXCEEDED")
            if (site_id, vehicle["vehicle_type"]) in restricted:
                reasons.append("SITE_ACCESS_RESTRICTED")
            if vehicle_shifts:
                if not any(row["shift_id"] in selected for row in vehicle_shifts):
                    reasons.append("OUTSIDE_CANDIDATE_FLEET")
                feasible_timing = False
                for shift in vehicle_shifts:
                    arrival = max(window["start_minute"], shift["start_minute"] + matrix["SITE-000000", site_id]["travel_minutes"])
                    depot_return = arrival + delivery["service_minutes"] + matrix[site_id, "SITE-000000"]["travel_minutes"]
                    feasible_timing |= arrival <= window["end_minute"] and depot_return <= shift["end_minute"]
                if not feasible_timing:
                    reasons.append("WINDOW_OR_SHIFT_INFEASIBLE")
            checks.append(dict(vehicle_id=vehicle["vehicle_id"], reason_codes=reasons))
        has_candidate = any(not row["reason_codes"] for row in checks)
        reason = "ROUTING_HEURISTIC_UNASSIGNED" if has_candidate else "CONSTRAINTS_PREVENT_ASSIGNMENT"
        explanations[delivery["delivery_id"]] = dict(reason_code=reason, vehicle_checks=checks, detail="A vehicle can serve this delivery alone, but the first-solution heuristic did not assign it among competing demand. This is not proof of fleet-wide infeasibility." if has_candidate else "Every vehicle has a listed constraint or candidate-fleet policy exclusion. Timing checks include service and depot return.")
    return explanations


def plan(tables):
    orders = {row["order_id"]: row for row in tables["Order"]}
    windows = {row["delivery_id"]: row for row in tables["TimeWindow"]}
    vehicles = {row["vehicle_id"]: row for row in tables["Vehicle"]}
    matrix_by_day = defaultdict(dict)
    for row in tables["TravelTimeMatrix"]:
        matrix_by_day[row["scenario_id"]][row["origin_id"], row["destination_id"]] = row
    for day_index, scenario in enumerate(sorted(tables["TrafficScenario"], key=lambda row: row["scenario_date"])):
        demands = [row for row in tables["Delivery"] if row["delivery_date"] == scenario["scenario_date"]]
        if not demands:
            continue
        available, capacities, restricted = daily_resources(tables, scenario["scenario_date"])
        if not available:
            continue
        shifts = candidate_shifts(available, day_index, len(demands))
        nodes = ["SITE-000000"] + [orders[row["order_id"]]["site_id"] for row in demands]
        matrix = matrix_by_day[scenario["scenario_id"]]
        manager = pywrapcp.RoutingIndexManager(len(nodes), len(shifts), 0)
        routing = pywrapcp.RoutingModel(manager)

        def transit(origin, destination):
            source, target = manager.IndexToNode(origin), manager.IndexToNode(destination)
            service = 0 if source == 0 else demands[source - 1]["service_minutes"]
            return matrix[nodes[source], nodes[target]]["travel_minutes"] + service

        transit_id = routing.RegisterTransitCallback(transit)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_id)
        routing.AddDimension(transit_id, 540, 1440, False, "Time")
        time_dimension = routing.GetDimensionOrDie("Time")
        for unit, name in [("weight_kg", "Weight"), ("volume_l", "Volume")]:
            def demand_callback(index, demand_unit=unit):
                node = manager.IndexToNode(index)
                return demands[node - 1][demand_unit] if node else 0
            callback_id = routing.RegisterUnaryTransitCallback(demand_callback)
            routing.AddDimensionWithVehicleCapacity(callback_id, 0, [capacities[row["vehicle_id"]][unit] for row in shifts], True, name)
        for node, demand in enumerate(demands, 1):
            index = manager.NodeToIndex(node)
            window = windows[demand["delivery_id"]]
            time_dimension.CumulVar(index).SetRange(window["start_minute"], window["end_minute"])
            allowed = [number for number, shift in enumerate(shifts) if (nodes[node], vehicles[shift["vehicle_id"]]["vehicle_type"]) not in restricted]
            if allowed:
                routing.SetAllowedVehiclesForIndex(allowed, index)
            else:
                routing.solver().Add(routing.ActiveVar(index) == 0)
            routing.AddDisjunction([index], {"STANDARD": 100000, "HIGH": 1000000, "CRITICAL": 10000000}[demand["priority"]])
        for number, shift in enumerate(shifts):
            time_dimension.CumulVar(routing.Start(number)).SetRange(shift["start_minute"], shift["start_minute"])
            time_dimension.CumulVar(routing.End(number)).SetRange(shift["start_minute"], shift["end_minute"])
            routing.AddVariableMinimizedByFinalizer(time_dimension.CumulVar(routing.End(number)))
        parameters = pywrapcp.DefaultRoutingSearchParameters()
        parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        parameters.solution_limit = 1
        solution = routing.SolveWithParameters(parameters)
        if solution is None:
            continue
        for number, shift in enumerate(shifts):
            cursor = routing.Start(number)
            if routing.IsEnd(solution.Value(routing.NextVar(cursor))):
                continue
            route_id = f"RTE-{scenario['scenario_date'][:4]}-{len(tables['Route']) + 1:06d}"
            route_distance, previous_site, sequence = 0, "SITE-000000", 0
            cursor = solution.Value(routing.NextVar(cursor))
            while not routing.IsEnd(cursor):
                sequence += 1
                demand = demands[manager.IndexToNode(cursor) - 1]
                site_id = orders[demand["order_id"]]["site_id"]
                route_distance += matrix[previous_site, site_id]["distance_m"]
                arrival = solution.Value(time_dimension.CumulVar(cursor))
                tables["RouteStop"].append(dict(stop_id=f"STOP-{route_id}-{sequence:03d}", route_id=route_id, delivery_id=demand["delivery_id"], sequence=sequence, arrival_minute=arrival, departure_minute=arrival + demand["service_minutes"]))
                previous_site = site_id
                cursor = solution.Value(routing.NextVar(cursor))
            route_distance += matrix[previous_site, "SITE-000000"]["distance_m"]
            tables["Route"].append(dict(route_id=route_id, shift_id=shift["shift_id"], depot_id="SITE-000000", start_minute=shift["start_minute"], end_minute=solution.Value(time_dimension.CumulVar(cursor)), distance_m=route_distance, state="PLANNED"))


def simulate(tables):
    deliveries = {row["delivery_id"]: row for row in tables["Delivery"]}
    orders = {row["order_id"]: row for row in tables["Order"]}
    windows = {row["delivery_id"]: row for row in tables["TimeWindow"]}
    shifts = {row["shift_id"]: row for row in tables["Shift"]}
    scenarios = {row["scenario_date"]: row for row in tables["TrafficScenario"]}
    matrices = {(row["scenario_id"], row["origin_id"], row["destination_id"]): row for row in tables["TravelTimeMatrix"]}
    outcomes = {}
    for route in tables["Route"]:
        shift = shifts[route["shift_id"]]
        scenario = scenarios[shift["shift_date"]]
        previous_departure, previous_site = route["start_minute"], route["depot_id"]
        for stop in [row for row in tables["RouteStop"] if row["route_id"] == route["route_id"]]:
            delivery = deliveries[stop["delivery_id"]]
            site_id = orders[delivery["order_id"]]["site_id"]
            leg = matrices[scenario["scenario_id"], previous_site, site_id]
            actual_arrival = max(stop["arrival_minute"], previous_departure + math.ceil(leg["travel_minutes"] * (1.12 if scenario["weather"] != "CLEAR" else 1.03)))
            actual_departure = actual_arrival + delivery["service_minutes"]
            late = max(0, actual_arrival - windows[delivery["delivery_id"]]["end_minute"])
            reason = "SITE_CLOSED" if int(site_id[5:]) % 37 == 0 else "SHIFT_EXCEEDED" if actual_departure > shift["end_minute"] else "WINDOW_MISSED" if late > 15 else None
            outcomes[delivery["delivery_id"]] = dict(outcome_id=f"OUT-{delivery['delivery_id']}", delivery_id=delivery["delivery_id"], stop_id=stop["stop_id"], actual_arrival=actual_arrival, actual_departure=actual_departure, status="FAILED" if reason else "DELIVERED", reason_code=reason, late_minutes=late)
            previous_departure, previous_site = actual_departure, site_id
        for sequence, state, minute in [(1, "PLANNED", 420), (2, "DISPATCHED", route["start_minute"]), (3, "COMPLETED", previous_departure + math.ceil(matrices[scenario["scenario_id"], previous_site, route["depot_id"]]["travel_minutes"] * (1.12 if scenario["weather"] != "CLEAR" else 1.03)))]:
            tables["RouteEvent"].append(dict(event_id=f"EVT-{route['route_id']}-{sequence}", route_id=route["route_id"], sequence=sequence, state=state, recorded_at=timestamp(shift["shift_date"], minute)))
    for delivery in tables["Delivery"]:
        delivery_id = delivery["delivery_id"]
        outcome = outcomes.get(delivery_id, dict(outcome_id=f"OUT-{delivery_id}", delivery_id=delivery_id, stop_id=None, actual_arrival=None, actual_departure=None, status="NOT_ATTEMPTED", reason_code="NO_FEASIBLE_VEHICLE", late_minutes=0))
        tables["DeliveryOutcome"].append(outcome)
        states = [("CREATED", 360, None), ("ASSIGNED" if outcome["stop_id"] else "UNSERVED", 420, None if outcome["stop_id"] else outcome["reason_code"])]
        if outcome["stop_id"]:
            states.append((outcome["status"], outcome["actual_departure"], outcome["reason_code"]))
        for sequence, (state, minute, reason) in enumerate(states, 1):
            tables["DeliveryEvent"].append(dict(event_id=f"EVT-{delivery_id}-{sequence}", delivery_id=delivery_id, sequence=sequence, state=state, recorded_at=timestamp(delivery["delivery_date"], minute), reason_code=reason))


def timestamp(day, minute):
    return f"{day}T{minute // 60:02d}:{minute % 60:02d}:00"