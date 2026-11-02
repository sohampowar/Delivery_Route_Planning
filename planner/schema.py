"""Executable schema, validation metadata, and export dictionary."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    type: str = "TEXT"
    required: bool = True
    reference: str = ""
    allowed: tuple = ()
    unit: str = ""
    rule: str = "Nonempty; immutable for IDs"


def field(name, type="TEXT", **kwargs):
    return Field(name, type, **kwargs)


SCHEMA = {
    "CustomerSite": [field("site_id"), field("name"), field("site_type", allowed=("CUSTOMER", "DEPOT")), field("latitude", "REAL", unit="degrees", rule="51.38 <= value <= 51.64"), field("longitude", "REAL", unit="degrees", rule="-0.30 <= value <= 0.13"), field("effective_from", rule="ISO date"), field("effective_to", required=False, rule="ISO date >= effective_from, or null"), field("state", allowed=("ACTIVE", "INACTIVE"))],
    "Order": [field("order_id"), field("site_id", reference="CustomerSite.site_id"), field("created_date", rule="ISO date <= delivery_date"), field("state", allowed=("CONFIRMED",))],
    "Delivery": [field("delivery_id"), field("order_id", reference="Order.order_id"), field("delivery_date", rule="ISO date"), field("weight_kg", "INTEGER", unit="kg", rule="Positive"), field("volume_l", "INTEGER", unit="litres", rule="Positive"), field("service_minutes", "INTEGER", unit="minutes", rule="Positive"), field("priority", allowed=("STANDARD", "HIGH", "CRITICAL")), field("state", allowed=("CREATED",))],
    "Vehicle": [field("vehicle_id"), field("name"), field("vehicle_type", allowed=("VAN", "TRUCK")), field("state", allowed=("ACTIVE", "MAINTENANCE"))],
    "VehicleCapacity": [field("capacity_id"), field("vehicle_id", reference="Vehicle.vehicle_id"), field("weight_kg", "INTEGER", unit="kg", rule="Positive"), field("volume_l", "INTEGER", unit="litres", rule="Positive"), field("effective_from", rule="ISO date"), field("effective_to", required=False, rule="ISO date >= effective_from, or null")],
    "Driver": [field("driver_id"), field("name"), field("state", allowed=("ACTIVE", "INACTIVE"))],
    "Shift": [field("shift_id"), field("driver_id", reference="Driver.driver_id"), field("vehicle_id", reference="Vehicle.vehicle_id"), field("shift_date", rule="ISO date"), field("start_minute", "INTEGER", unit="minutes after midnight", rule="0 <= value < end_minute"), field("end_minute", "INTEGER", unit="minutes after midnight", rule="start_minute < value <= 1440")],
    "TrafficScenario": [field("scenario_id"), field("scenario_date", rule="Unique ISO date"), field("factor", "REAL", unit="multiplier", rule="1.0 <= value <= 1.6"), field("weather", allowed=("CLEAR", "RAIN", "ROADWORKS"))],
    "TravelTimeMatrix": [field("matrix_id"), field("scenario_id", reference="TrafficScenario.scenario_id"), field("origin_id", reference="CustomerSite.site_id"), field("destination_id", reference="CustomerSite.site_id"), field("distance_m", "INTEGER", unit="metres", rule=">= 0; zero on diagonal"), field("travel_minutes", "INTEGER", unit="minutes", rule=">= 0; ceil(distance / 450 * traffic factor)")],
    "TimeWindow": [field("window_id"), field("delivery_id", reference="Delivery.delivery_id"), field("start_minute", "INTEGER", unit="minutes after midnight", rule="480 <= value < end_minute"), field("end_minute", "INTEGER", unit="minutes after midnight", rule="start_minute < value <= 1020")],
    "SiteVehicleRestriction": [field("restriction_id"), field("site_id", reference="CustomerSite.site_id"), field("vehicle_type", allowed=("VAN", "TRUCK")), field("effective_from", rule="ISO date"), field("effective_to", required=False, rule="ISO date >= effective_from, or null")],
    "Route": [field("route_id"), field("shift_id", reference="Shift.shift_id"), field("depot_id", reference="CustomerSite.site_id"), field("start_minute", "INTEGER", unit="minutes after midnight", rule="Within shift"), field("end_minute", "INTEGER", unit="minutes after midnight", rule="Includes return to depot; <= shift end"), field("distance_m", "INTEGER", unit="metres", rule="Sum of route legs including depot return"), field("state", allowed=("PLANNED",))],
    "RouteStop": [field("stop_id"), field("route_id", reference="Route.route_id"), field("delivery_id", reference="Delivery.delivery_id"), field("sequence", "INTEGER", rule="Consecutive starting at 1 per route"), field("arrival_minute", "INTEGER", unit="minutes after midnight", rule="Within delivery window; >= previous departure + travel"), field("departure_minute", "INTEGER", unit="minutes after midnight", rule="arrival + service duration")],
    "DeliveryOutcome": [field("outcome_id"), field("delivery_id", reference="Delivery.delivery_id"), field("stop_id", required=False, reference="RouteStop.stop_id"), field("actual_arrival", "INTEGER", required=False, unit="minutes after midnight", rule="Null only when not attempted"), field("actual_departure", "INTEGER", required=False, unit="minutes after midnight", rule="actual_arrival + service duration"), field("status", allowed=("DELIVERED", "FAILED", "NOT_ATTEMPTED")), field("reason_code", required=False, allowed=("SITE_CLOSED", "WINDOW_MISSED", "SHIFT_EXCEEDED", "NO_FEASIBLE_VEHICLE")), field("late_minutes", "INTEGER", unit="minutes", rule="max(0, actual arrival - window end)")],
    "DeliveryEvent": [field("event_id"), field("delivery_id", reference="Delivery.delivery_id"), field("sequence", "INTEGER", rule="Consecutive starting at 1 per delivery"), field("state", allowed=("CREATED", "ASSIGNED", "UNSERVED", "DELIVERED", "FAILED")), field("recorded_at", rule="ISO timestamp"), field("reason_code", required=False)],
    "RouteEvent": [field("event_id"), field("route_id", reference="Route.route_id"), field("sequence", "INTEGER", rule="Consecutive starting at 1 per route"), field("state", allowed=("PLANNED", "DISPATCHED", "COMPLETED")), field("recorded_at", rule="ISO timestamp")],
}

UNIQUE = {
    "VehicleCapacity": [("vehicle_id", "effective_from")],
    "Shift": [("driver_id", "shift_date"), ("vehicle_id", "shift_date")],
    "TrafficScenario": [("scenario_date",)],
    "TravelTimeMatrix": [("scenario_id", "origin_id", "destination_id")],
    "TimeWindow": [("delivery_id",)],
    "SiteVehicleRestriction": [("site_id", "vehicle_type", "effective_from")],
    "Route": [("shift_id",)],
    "RouteStop": [("route_id", "sequence"), ("delivery_id",)],
    "DeliveryOutcome": [("delivery_id",), ("stop_id",)],
    "DeliveryEvent": [("delivery_id", "sequence")],
    "RouteEvent": [("route_id", "sequence")],
}


def ddl():
    statements = ["PRAGMA foreign_keys = ON;"]
    for table, fields in SCHEMA.items():
        columns = []
        for index, item in enumerate(fields):
            definition = f'"{item.name}" {item.type}'
            if index == 0:
                definition += " PRIMARY KEY"
            if item.required:
                definition += " NOT NULL"
            if item.reference:
                parent, key = item.reference.split(".")
                definition += f' REFERENCES "{parent}"("{key}")'
            if item.allowed:
                values = ",".join(repr(value) for value in item.allowed)
                definition += f' CHECK ("{item.name}" IN ({values}))'
            columns.append(definition)
        for keys in UNIQUE.get(table, []):
            columns.append("UNIQUE (" + ",".join(f'"{key}"' for key in keys) + ")")
        statements.append(f'CREATE TABLE "{table}" (\n  ' + ",\n  ".join(columns) + "\n);")
    return "\n".join(statements)


def dictionary():
    lines = ["# Data Dictionary", "", "All times are local synthetic London minutes after midnight; dates are ISO 8601. All primary keys are unique and immutable. No personal data is used.", ""]
    for table, fields in SCHEMA.items():
        lines += [f"## {table}", "", "| Field | SQL type | Required | Units | Allowed / Validation | Unique / FK |", "|---|---|---|---|---|---|"]
        for index, item in enumerate(fields):
            rules = ", ".join(item.allowed) if item.allowed else item.rule
            relation = "PK" if index == 0 else item.reference or "-"
            lines.append(f"| {item.name} | {item.type} | {'yes' if item.required else 'no'} | {item.unit or '-'} | {rules} | {relation} |")
        for keys in UNIQUE.get(table, []):
            lines.append(f"\nComposite uniqueness: {', '.join(keys)}.")
        lines.append("")
    return "\n".join(lines)