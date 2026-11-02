import unittest
from collections import Counter
from datetime import date, timedelta
from unittest.mock import patch

from planner.engine import allocation_explanations, generate, plan, planning_period, simulate
from planner.validation import validate


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = generate(start_date="2026-11-02", end_date="2026-11-08")

    def test_volume_and_coverage(self):
        self.assertEqual(len(self.tables["CustomerSite"]), 61)
        self.assertEqual(len(self.tables["Delivery"]), 160)
        self.assertEqual(len(self.tables["Vehicle"]), 12)
        self.assertEqual(len(self.tables["TrafficScenario"]), 7)
        self.assertEqual(len(self.tables["DeliveryOutcome"]), 160)

    def test_route_constraints(self):
        tables = self.tables
        deliveries = {row["delivery_id"]: row for row in tables["Delivery"]}
        shifts = {row["shift_id"]: row for row in tables["Shift"]}
        capacities = {row["vehicle_id"]: row for row in tables["VehicleCapacity"]}
        windows = {row["delivery_id"]: row for row in tables["TimeWindow"]}
        assigned = []
        for route in tables["Route"]:
            shift = shifts[route["shift_id"]]
            stops = [row for row in tables["RouteStop"] if row["route_id"] == route["route_id"]]
            self.assertLessEqual(route["end_minute"], shift["end_minute"])
            for unit in ["weight_kg", "volume_l"]:
                self.assertLessEqual(sum(deliveries[row["delivery_id"]][unit] for row in stops), capacities[shift["vehicle_id"]][unit])
            previous_departure = route["start_minute"]
            for stop in stops:
                window = windows[stop["delivery_id"]]
                self.assertGreaterEqual(stop["arrival_minute"], previous_departure)
                self.assertGreaterEqual(stop["arrival_minute"], window["start_minute"])
                self.assertLessEqual(stop["arrival_minute"], window["end_minute"])
                previous_departure = stop["departure_minute"]
                assigned.append(stop["delivery_id"])
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertGreater(len(assigned), 100)
        self.assertTrue(all(row["reason_code"] for row in tables["DeliveryOutcome"] if row["status"] != "DELIVERED"))

    def test_reproducibility(self):
        self.assertEqual(self.tables, generate(start_date="2026-11-02", end_date="2026-11-08"))

    def test_custom_period(self):
        tables = generate(start_date="2025-12-30", end_date="2026-01-02")
        dates = ["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"]
        self.assertEqual([row["scenario_date"] for row in tables["TrafficScenario"]], dates)
        self.assertEqual(len(tables["Delivery"]), 92)
        self.assertEqual(len(tables["DeliveryOutcome"]), 92)
        for day in dates:
            self.assertEqual(sum(row["shift_date"] == day for row in tables["Shift"]), 12)
            self.assertTrue(any(row["delivery_date"] == day for row in tables["Delivery"]))
            self.assertTrue(any(row["shift_id"].startswith(f"SHF-{dates.index(day) + 1:03d}-") for row in tables["Route"]))
        self.assertEqual(len(tables["TimeWindow"]), 92)
        self.assertTrue(validate(tables)["passed"])

    def test_invalid_period(self):
        for start, end in [("2026-11-02", None), (None, "2026-11-02"), ("bad", "2026-11-02"), ("2026-11-03", "2026-11-02"), (42, 43), ("2027-02-29", "2027-03-01"), ("2028-02-30", "2028-03-01"), ("", ""), ("2028-2-01", "2028-03-01")]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                generate(start_date=start, end_date=end)

    def test_longer_horizons(self):
        for end, days, count in [("2026-11-30", 30, 686), ("2026-12-05", 35, 800)]:
            with self.subTest(days=days):
                tables = generate(start_date="2026-11-01", end_date=end)
                self.assertEqual(len(tables["TrafficScenario"]), days)
                self.assertEqual(len(tables["Delivery"]), count)
                self.assertEqual(len(tables["DeliveryOutcome"]), count)
                self.assertEqual(len(tables["Shift"]), days * 12)
                self.assertEqual(tables["TrafficScenario"][-1]["scenario_date"], end)
                self.assertTrue(validate(tables)["passed"])

    def test_calendar_periods_cover_every_day(self):
        periods = [("2029-04-04", "2029-04-10", 7), ("2030-06-01", "2030-06-30", 30), ("2031-07-01", "2031-07-31", 31), ("2029-04-28", "2029-05-03", 6), ("2029-12-29", "2030-01-03", 6), ("2028-02-27", "2028-03-02", 5), ("0001-01-01", "0001-01-01", 1), ("9999-12-31", "9999-12-31", 1)]
        for start, end, days in periods:
            with self.subTest(start=start, end=end):
                tables = generate(start_date=start, end_date=end)
                dates = [(date.fromisoformat(start) + timedelta(days=offset)).isoformat() for offset in range(days)]
                self.assertEqual([row["scenario_date"] for row in tables["TrafficScenario"]], dates)
                demand = Counter(row["delivery_date"] for row in tables["Delivery"])
                self.assertEqual(set(demand), set(dates))
                shifts = {row["shift_id"]: row for row in tables["Shift"]}
                self.assertEqual({shifts[row["shift_id"]]["shift_date"] for row in tables["Route"]}, set(dates))
                self.assertEqual(len(tables["DeliveryOutcome"]), sum(demand.values()))
                self.assertTrue(validate(tables)["passed"])
                self.assertEqual(tables, generate(start_date=start, end_date=end))

    def test_default_period_uses_today(self):
        with patch("planner.engine.date", wraps=date) as calendar:
            calendar.today.return_value = date(2032, 2, 27)
            self.assertEqual(planning_period("starter"), (date(2032, 2, 27), date(2032, 3, 4)))

    def replan(self, tables):
        for name in ("Route", "RouteStop", "DeliveryOutcome", "DeliveryEvent", "RouteEvent"):
            tables[name] = []
        plan(tables)
        simulate(tables)
        return tables

    def test_no_active_vehicles_is_explicitly_unserved(self):
        tables = dict(self.tables)
        tables["Vehicle"] = [dict(row, state="MAINTENANCE") for row in tables["Vehicle"]]
        self.replan(tables)
        self.assertEqual(tables["Route"], [])
        self.assertTrue(all(row["status"] == "NOT_ATTEMPTED" and row["reason_code"] == "NO_FEASIBLE_VEHICLE" for row in tables["DeliveryOutcome"]))
        self.assertTrue(validate(tables)["passed"])

    def test_no_active_drivers_is_explicitly_unserved(self):
        tables = dict(self.tables, Driver=[dict(row, state="INACTIVE") for row in self.tables["Driver"]])
        self.replan(tables)
        self.assertEqual(tables["Route"], [])
        self.assertEqual(len(tables["DeliveryOutcome"]), 160)
        reasons = allocation_explanations(tables, tables["TrafficScenario"][0])
        self.assertTrue(all("DRIVER_INACTIVE" in check["reason_codes"] for row in reasons.values() for check in row["vehicle_checks"]))
        self.assertTrue(validate(tables)["passed"])

    def test_day_without_driver_shifts(self):
        day = self.tables["TrafficScenario"][1]["scenario_date"]
        tables = dict(self.tables, Shift=[row for row in self.tables["Shift"] if row["shift_date"] != day])
        self.replan(tables)
        affected = {row["delivery_id"] for row in tables["Delivery"] if row["delivery_date"] == day}
        self.assertTrue(all(row["status"] == "NOT_ATTEMPTED" for row in tables["DeliveryOutcome"] if row["delivery_id"] in affected))
        self.assertEqual(len(tables["TrafficScenario"]), 7)
        self.assertTrue(validate(tables)["passed"])

    def test_resource_effective_dates(self):
        tables = dict(self.tables, VehicleCapacity=[dict(row, effective_from="2027-01-01") for row in self.tables["VehicleCapacity"]])
        self.replan(tables)
        self.assertEqual(tables["Route"], [])
        self.assertTrue(validate(tables)["passed"])
        reasons = allocation_explanations(tables, tables["TrafficScenario"][0])
        self.assertTrue(all("CAPACITY_UNAVAILABLE" in check["reason_codes"] for row in reasons.values() for check in row["vehicle_checks"]))
        expired = [dict(restriction_id=f"TEST-{index}-{kind}", site_id=site["site_id"], vehicle_type=kind, effective_from="2025-01-01", effective_to="2025-12-31") for index, site in enumerate(tables["CustomerSite"]) for kind in ("VAN", "TRUCK")]
        tables = dict(self.tables, SiteVehicleRestriction=expired)
        self.replan(tables)
        self.assertTrue(tables["Route"])
        self.assertTrue(validate(tables)["passed"])

    def test_insufficient_capacity_is_not_overloaded(self):
        tables = dict(self.tables)
        tables["VehicleCapacity"] = [dict(row, weight_kg=1) for row in tables["VehicleCapacity"]]
        self.replan(tables)
        self.assertEqual(tables["RouteStop"], [])
        self.assertTrue(validate(tables)["passed"])
        reasons = allocation_explanations(tables, tables["TrafficScenario"][0])
        self.assertTrue(reasons)
        self.assertTrue(all("WEIGHT_CAPACITY_EXCEEDED" in check["reason_codes"] for row in reasons.values() for check in row["vehicle_checks"]))

    def test_service_duration_changes_planning_feasibility(self):
        tables = dict(self.tables)
        tables["Delivery"] = [dict(row, service_minutes=1500) for row in tables["Delivery"]]
        self.replan(tables)
        self.assertEqual(tables["RouteStop"], [])
        reasons = allocation_explanations(tables, tables["TrafficScenario"][0])
        self.assertTrue(all("WINDOW_OR_SHIFT_INFEASIBLE" in check["reason_codes"] for row in reasons.values() for check in row["vehicle_checks"]))

    def test_all_vehicle_classes_restricted(self):
        tables = dict(self.tables)
        tables["SiteVehicleRestriction"] = [dict(restriction_id=f"TEST-{index}-{kind}", site_id=site["site_id"], vehicle_type=kind, effective_from="2026-01-01", effective_to=None) for index, site in enumerate(tables["CustomerSite"]) if site["site_type"] == "CUSTOMER" for kind in ("VAN", "TRUCK")]
        self.replan(tables)
        self.assertEqual(tables["Route"], [])
        self.assertEqual(len(tables["DeliveryOutcome"]), 160)
        self.assertTrue(validate(tables)["passed"])

    def test_seed_variation_and_invalid_scale(self):
        self.assertNotEqual(self.tables["CustomerSite"], generate(seed=42)["CustomerSite"])
        with self.assertRaises(ValueError):
            generate("invalid")


if __name__ == "__main__":
    unittest.main()