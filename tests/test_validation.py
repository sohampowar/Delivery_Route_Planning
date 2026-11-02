import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from planner.engine import generate, plan, simulate
from planner.schema import SCHEMA
from planner.storage import archive, checksums, export, load, persist
from planner.validation import anomaly_cases, apply_case, evaluate_anomalies, validate


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = generate()

    def test_baseline(self):
        result = validate(self.tables)
        self.assertTrue(result["passed"], result["issues"][:5])

    def test_missing_planning_day_is_reported(self):
        tables = dict(self.tables, TrafficScenario=self.tables["TrafficScenario"][:1] + self.tables["TrafficScenario"][2:])
        tables["TravelTimeMatrix"] = [row for row in tables["TravelTimeMatrix"] if row["scenario_id"] != self.tables["TrafficScenario"][1]["scenario_id"]]
        issues = {row["issue_type"] for row in validate(tables)["issues"]}
        self.assertIn("PLANNING_DATE_GAP", issues)
        self.assertIn("MISSING_SCENARIO", issues)

    def test_empty_period_export_and_reload(self):
        tables = dict(self.tables)
        for name in ("Order", "Delivery", "TimeWindow", "Route", "RouteStop", "DeliveryOutcome", "DeliveryEvent", "RouteEvent"):
            tables[name] = []
        plan(tables)
        simulate(tables)
        self.assertTrue(validate(tables)["passed"])
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "empty"
            manifest, result = export(tables, destination)
            self.assertEqual(manifest["days"], 7)
            self.assertEqual(result["anomaly_evaluation"]["cases"], 0)
            self.assertEqual(load(destination / "planning.sqlite"), tables)
            self.assertTrue((destination / "ground_truth.csv").read_text().startswith("case_id,"))
            self.assertGreater(len(archive(destination)), 0)

    def test_all_six_anomalies(self):
        cases, truth = anomaly_cases(self.tables)
        for case, label in zip(cases, truth):
            with self.subTest(case=case["case_id"]):
                result = validate(apply_case(self.tables, case))
                self.assertIn(label["issue_type"], [row["issue_type"] for row in result["issues"]])
        self.assertTrue(validate(self.tables)["passed"])
        self.assertNotIn("ground_truth", SCHEMA)
        self.assertTrue(all("issue_type" not in case for case in cases))

    def test_detection_performance(self):
        result = evaluate_anomalies(self.tables)
        self.assertEqual((result["true_positives"], result["false_positives"], result["false_negatives"], result["true_negatives"]), (6, 0, 0, 6))
        self.assertEqual(result["detection_rate_pct"], 100)
        with patch("planner.validation.validate", return_value={"passed": True, "issues": []}):
            missed = evaluate_anomalies(self.tables)
        self.assertEqual(missed["false_negatives"], 6)
        self.assertIsNone(missed["precision_pct"])
        with patch("planner.validation.validate", return_value={"passed": False, "issues": [{"issue_type": "STOP_OMITTED"}]}):
            noisy = evaluate_anomalies(self.tables)
        self.assertEqual(noisy["false_positives"], 1)
        self.assertEqual(noisy["true_positives"], 1)
        self.assertEqual(noisy["false_negatives"], 5)
        self.assertTrue(all(row["expected_action"] for row in result["results"]))

    def test_integrity_errors(self):
        for table, key, value, expected in [("Order", "site_id", "NO-SITE", "FOREIGN_KEY"), ("Delivery", "priority", "INVALID", "INVALID_ENUM"), ("Delivery", "weight_kg", -3, "NONPOSITIVE_VALUE"), ("DeliveryEvent", "state", "FAILED", "LIFECYCLE_TRANSITION"), ("DeliveryOutcome", "late_minutes", 999, "SIMULATION_RECONCILIATION")]:
            with self.subTest(table=table, key=key):
                changed = dict(self.tables)
                changed[table] = [dict(row) for row in self.tables[table]]
                changed[table][0][key] = value
                self.assertIn(expected, [row["issue_type"] for row in validate(changed)["issues"]])

    def test_storage_and_append_only_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.sqlite"
            persist(self.tables, path)
            self.assertEqual(checksums(load(path)), checksums(self.tables))
            with self.assertRaises(FileExistsError):
                persist(self.tables, path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute('DELETE FROM "DeliveryEvent"')
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute('UPDATE "RouteEvent" SET state = \'PLANNED\'')

    def test_export_and_sql_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "export"
            manifest, result = export(self.tables, destination)
            self.assertTrue(result["passed"])
            self.assertEqual(result["anomaly_evaluation"]["detected"], 6)
            self.assertEqual(manifest["table_sha256"], checksums(self.tables))
            connection = sqlite3.connect(":memory:")
            connection.executescript((destination / "seed.sql").read_text(encoding="utf-8"))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM "Delivery"').fetchone()[0], 160)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            connection.close()
            self.assertEqual(json.loads((destination / "dataset.json").read_bytes()), self.tables)
            self.assertGreater(len(archive(destination)), 10000)


if __name__ == "__main__":
    unittest.main()