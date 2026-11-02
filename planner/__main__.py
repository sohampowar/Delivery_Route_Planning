import argparse
import json
from pathlib import Path

from .engine import SEED
from .storage import build, load
from .validation import validate


def main():
    parser = argparse.ArgumentParser(description="Waypoint delivery planning")
    commands = parser.add_subparsers(dest="command", required=True)
    generator = commands.add_parser("generate", help="Generate CSV, JSON, SQLite and SQL exports")
    generator.add_argument("--scale", choices=["starter", "full"], default="full")
    generator.add_argument("--seed", type=int, default=SEED)
    generator.add_argument("--start-date", help="Inclusive planning start (YYYY-MM-DD); supply with --end-date")
    generator.add_argument("--end-date", help="Inclusive planning end (YYYY-MM-DD)")
    generator.add_argument("--output", type=Path, required=True)
    generator.add_argument("--verify", action="store_true", help="Generate twice and compare every table checksum")
    validator = commands.add_parser("validate", help="Validate an exported SQLite dataset")
    validator.add_argument("database", type=Path)
    server = commands.add_parser("serve", help="Start the local browser application")
    server.add_argument("--port", type=int, default=8000)
    server.add_argument("--scale", choices=["starter", "full"], default="full")
    server.add_argument("--data", type=Path, default=Path("data"))
    server.add_argument("--start-date", help="Select a planning start date; supply with --end-date")
    server.add_argument("--end-date", help="Select an inclusive planning end date")
    args = parser.parse_args()
    if args.command == "generate":
        if not 0 <= args.seed <= 4294967295:
            parser.error("Seed must be between 0 and 4294967295")
        tables, (manifest, result) = build(args.scale, args.seed, args.output, args.verify, args.start_date, args.end_date)
        print(json.dumps(dict(output=str(args.output), passed=result["passed"], rows=manifest["row_counts"], anomalies=result["anomaly_evaluation"]["detected"], reproducible=manifest["reproducibility"]), indent=2))
    elif args.command == "validate":
        result = validate(load(args.database))
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["passed"] else 1)
    else:
        from .service import serve
        serve(args.port, args.data, args.scale, args.start_date, args.end_date)


if __name__ == "__main__":
    main()