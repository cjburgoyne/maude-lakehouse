"""Command-line entry point. Keep this thin: parse arguments, call library code, print results."""

import argparse
import json
from pathlib import Path

from maude_lakehouse.ingestion.landing import land_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maude-lakehouse")
    commands = parser.add_subparsers(dest="command", required=True)

    land = commands.add_parser("land", help="Validate a source zip and land it in the raw zone")
    land.add_argument("source", type=Path, help="Path to the downloaded zip file")
    land.add_argument("--dataset", required=True, help="Dataset name, e.g. device, foitext, mdrfoi")
    land.add_argument("--source-url", help="URL from which the file was downloaded")
    land.add_argument("--project-root", type=Path, default=Path.cwd(), help="Project root (default: current folder)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "land":
        result = land_file(args.source, args.dataset, args.project_root, args.source_url)
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
