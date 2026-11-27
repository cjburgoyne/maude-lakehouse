"""Command-line entry point. Keep this thin: parse arguments, call library code, print results."""

import argparse
import json
from pathlib import Path
from zipfile import BadZipFile

import httpx

from maude_lakehouse.ingestion.download import check_file, download_file, make_client
from maude_lakehouse.ingestion.landing import land_file
from maude_lakehouse.profiling.source_profile import profile_zip, write_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maude-lakehouse")
    commands = parser.add_subparsers(dest="command", required=True)

    land = commands.add_parser("land", help="Validate a source zip and land it in the raw zone")
    land.add_argument("source", type=Path, help="Path to the downloaded zip file")
    land.add_argument("--dataset", required=True, help="Dataset name, e.g. device, foitext, mdrfoi")
    land.add_argument("--source-url", help="URL from which the file was downloaded")
    land.add_argument("--project-root", type=Path, default=Path.cwd(), help="Project root (default: current folder)")

    profile = commands.add_parser("profile", help="Profile source zips: encoding, line breaks, field counts, columns")
    profile.add_argument("sources", nargs="+", type=Path, help="Zip files to profile (same file family)")
    profile.add_argument(
        "--out-dir",
        type=Path,
        default=Path("metadata/profiles"),
        help="Where to write the reports (default: metadata/profiles, which git ignores)",
    )
    profile.add_argument("--max-records", type=int, help="Stop after this many records per file")
    profile.add_argument(
        "--samples",
        action="store_true",
        help="Include sample values and raw lines. They can contain names and narrative text; do not commit.",
    )

    download = commands.add_parser(
        "download", help="Download FDA files and land them, skipping files the server reports unchanged"
    )
    download.add_argument("filenames", nargs="+", help="FDA file names, e.g. deviceadd.zip mdrfoiadd.zip")
    download.add_argument("--dataset", help="Dataset name for all files (default: inferred from each file name)")
    download.add_argument("--force", action="store_true", help="Download even if the server reports no change")
    download.add_argument(
        "--check-only", action="store_true", help="Only report new / changed / unchanged; download nothing"
    )
    download.add_argument(
        "--project-root", type=Path, default=Path.cwd(), help="Project root (default: current folder)"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "land":
        result = land_file(args.source, args.dataset, args.project_root, args.source_url)
        print(json.dumps(result, indent=2))

    elif args.command == "profile":
        profiles = [
            profile_zip(source, max_records=args.max_records, include_samples=args.samples, progress=True)
            for source in args.sources
        ]
        print(json.dumps(write_reports(profiles, args.out_dir), indent=2))

    elif args.command == "download":
        return run_downloads(args)

    return 0


def run_downloads(args: argparse.Namespace) -> int:
    """Process each file in turn. One failure doesn't stop the rest, but makes the exit code 1."""
    results = []
    with make_client() as client:
        for filename in args.filenames:
            try:
                if args.check_only:
                    result = check_file(filename, args.project_root, client=client)
                else:
                    result = download_file(
                        filename,
                        args.project_root,
                        client=client,
                        dataset=args.dataset,
                        force=args.force,
                        progress=True,
                    )
            except (httpx.HTTPError, ValueError, BadZipFile) as error:
                result = {"filename": filename, "status": "error", "error": str(error)}
            results.append(result)

    print(json.dumps(results, indent=2))
    return 1 if any(r["status"] == "error" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
