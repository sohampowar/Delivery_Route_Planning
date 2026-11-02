"""Immutable dataset versions, transactional SQLite storage and portable exports."""

import csv
import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

from .engine import SEED, generate, planning_period
from .schema import SCHEMA, ddl, dictionary
from .validation import anomaly_cases, evaluate_anomalies, validate

DATASET_VERSION = "1.3"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def checksums(tables):
    return {name: hashlib.sha256(canonical(rows)).hexdigest() for name, rows in tables.items()}


def persist(tables, path):
    path = Path(path)
    if path.exists():
        raise FileExistsError("Dataset versions are immutable; use a new version path")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(ddl())
        with connection:
            for table, fields in SCHEMA.items():
                columns = ",".join(f'"{item.name}"' for item in fields)
                placeholders = ",".join("?" for item in fields)
                connection.executemany(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', [tuple(row[item.name] for item in fields) for row in tables[table]])
            for table in ("DeliveryEvent", "RouteEvent"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f'CREATE TRIGGER "immutable_{table}_{operation}" BEFORE {operation} ON "{table}" BEGIN SELECT RAISE(ABORT, \'Event history is append-only\'); END;')
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Foreign-key verification failed")
    finally:
        connection.close()


def load(path):
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {name: [dict(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in SCHEMA}
    finally:
        connection.close()


def export(tables, destination, seed=SEED, scale="starter", reproducibility=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    validation = validate(tables)
    if not validation["passed"]:
        raise ValueError(f"Refusing to export an invalid operational baseline: {validation['issues'][:3]}")
    inputs, truth = anomaly_cases(tables)
    validation["anomaly_evaluation"] = evaluate_anomalies(tables)
    manifest = dict(seed=seed, scale=scale, source="Entirely synthetic; no external datasets or real drivers/customers", source_url=None, dataset_version=DATASET_VERSION, retrieval_date=None, license="Generated synthetic records: CC0-1.0; OR-Tools dependency: Apache-2.0", attribution="No external data used", transformations="Seeded triangular demand, synthetic coordinates, 1.22 road-distance multiplier, traffic-adjusted travel and OR-Tools routing", fields_discarded=[], row_counts=validation["row_counts"], table_sha256=checksums(tables), reproducibility=reproducibility, feature_tables=[name for name in SCHEMA if name not in ("DeliveryOutcome", "DeliveryEvent", "RouteEvent")], target_tables=["DeliveryOutcome", "DeliveryEvent", "RouteEvent"], ground_truth="ground_truth.json / ground_truth.csv; never passed to planner or detector")
    dates = sorted(row["scenario_date"] for row in tables["TrafficScenario"])
    manifest.update(start_date=dates[0], end_date=dates[-1], days=len(dates))
    for table, fields in SCHEMA.items():
        with (destination / f"{table}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[item.name for item in fields])
            writer.writeheader()
            writer.writerows(tables[table])
    for name, content in [("dataset.json", tables), ("ground_truth.json", truth), ("anomaly_inputs.json", inputs), ("validation_summary.json", validation), ("source_manifest.json", manifest)]:
        (destination / name).write_bytes(canonical(content))
    with (destination / "ground_truth.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(truth[0]) if truth else ["case_id", "issue_type", "ground_truth", "expected_classification", "expected_action", "entity_id"])
        writer.writeheader()
        writer.writerows(truth)
    (destination / "DATA_DICTIONARY.md").write_text(dictionary(), encoding="utf-8")
    (destination / "schema.sql").write_text(ddl(), encoding="utf-8")
    persist(tables, destination / "planning.sqlite")
    connection = sqlite3.connect(destination / "planning.sqlite")
    try:
        with (destination / "seed.sql").open("w", encoding="utf-8") as handle:
            handle.write(ddl() + "\nBEGIN TRANSACTION;\nPRAGMA defer_foreign_keys=ON;\n")
            for statement in connection.iterdump():
                if statement.startswith(("INSERT INTO", "CREATE TRIGGER")):
                    handle.write(statement + "\n")
            handle.write("COMMIT;\n")
    finally:
        connection.close()
    (destination / "README.md").write_text(f"# Delivery planning dataset\n\nSeed: {seed}. Scale: {scale}. Period: {dates[0]} to {dates[-1]} ({len(dates)} days, inclusive). All data is synthetic.\n\nRegenerate: `python -m planner generate --scale {scale} --seed {seed} --start-date {dates[0]} --end-date {dates[-1]} --output artifacts/new-version --verify`\n\nCSV blank cells represent SQL NULL. dataset.json preserves null types. planning.sqlite and seed.sql contain the same operational records. SQL tables are dependency ordered by the loader; SQLite enforces all references at transaction commit. Ground truth and anomaly patches are deliberately separate from baseline input features. Apply each anomaly independently with planner.validation.apply_case. Consult DATA_DICTIONARY.md, source_manifest.json, and validation_summary.json. The application README documents rules, relationships, and limitations.\n", encoding="utf-8")
    file_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(destination.iterdir())}
    (destination / "checksums.json").write_bytes(canonical(file_hashes))
    return manifest, validation


def archive(directory):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(Path(directory).iterdir()):
            if path.is_file():
                info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, path.read_bytes())
    return buffer.getvalue()


def build(scale, seed, output, verify=False, start_date=None, end_date=None):
    start, end = planning_period(scale, start_date, end_date)
    start_date, end_date = start.isoformat(), end.isoformat()
    first = generate(scale, seed, start_date, end_date)
    comparison = None
    if verify:
        second = generate(scale, seed, start_date, end_date)
        comparison = dict(passed=checksums(first) == checksums(second), first=checksums(first), second=checksums(second), row_counts_match={name: len(rows) for name, rows in first.items()} == {name: len(rows) for name, rows in second.items()})
        if not comparison["passed"]:
            raise ValueError("Fixed-seed reproducibility check failed")
    return first, export(first, output, seed, scale, comparison)