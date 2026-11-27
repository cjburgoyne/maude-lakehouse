# maude-lakehouse
[![CI](https://github.com/cjburgoyne/maude-lakehouse/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/cjburgoyne/maude-lakehouse/actions/workflows/ci.yml)

A lakehouse for the FDA's MAUDE medical device adverse event data: raw public files landed unchanged, parsed
into a source-preserving bronze layer, and processed into typed silver tables and targeted gold models.

**Status:** early. Downloading with change detection, file landing (checksums, content-addressed storage,
manifest), and source profiling are in place. Next: profiling every file family, then the bronze parser.

## Why

MAUDE (Manufacturer and User Facility Device Experience) is the FDA's public database of medical device adverse
event reports. It is a realistic data engineering problem:

- Pipe-delimited text files with embedded delimiters, line breaks, and encoding issues
- Historical files split differently by file family (cumulative files for some, one file per year for others)
- Monthly "Add" and "Change" files that both update existing reports and add new child records, so loading
  them correctly is a change data capture problem, not an append
- Free-text manufacturer and brand names that need entity resolution

## Data source

[FDA MAUDE data files](https://www.fda.gov/medical-devices/medical-device-reporting-mdr-how-report-medical-device-problems/mdr-data-files),
updated monthly. FDA data is in the public domain. No data is stored in this repository.

## Architecture (planned)

```
FDA file server
      │  download if changed (HEAD check), validate, checksum      laptop or GitHub Actions
      ▼
raw         original zip files, unchanged, content-addressed (sha256), recorded in a manifest
      │  parse; quarantine rows that fail, never drop them silently
      ▼
bronze      source-preserving Parquet with file-level lineage
      │  upload to a Unity Catalog volume
      ▼
─────────────────────────────── Databricks Free Edition ───────────────────────────────
      │  merge monthly Add/Change files (CDC), type and deduplicate
      ▼
silver      typed, deduplicated Delta tables
      │  dbt models with tests
      ▼
gold        targeted dimensional models
```

## What's here now

- `src/maude_lakehouse/ingestion/download.py`: checks each FDA file with a `HEAD` request and downloads it only
  if the server reports a change since the last download; checks that the whole file arrived, then lands it
  ([ADR 0004](docs/adr/0004-download-change-detection.md)).
- `src/maude_lakehouse/ingestion/landing.py`: validates a source zip, stores it by SHA-256 so identical files
  are never duplicated, and copies atomically (temporary file, verify, rename).
- `src/maude_lakehouse/ingestion/manifest.py`: SQLite manifest (`metadata/ingestion.db`) with one row per
  unique file content, one row per landing, and one row per download check.
- `src/maude_lakehouse/profiling/source_profile.py`: profiles source zips (encoding, line endings, records
  split across lines, field counts, column shapes, header changes); its results settled the bronze design in
  [ADR 0003](docs/adr/0003-bronze-layer.md).
- `src/maude_lakehouse/cli.py`: the `maude-lakehouse` command, with `download`, `land`, and `profile`
  subcommands.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
uv run pytest
```

Check whether FDA files have changed, download and land them, then profile what was landed:

```bash
uv run maude-lakehouse download deviceadd.zip mdrfoiadd.zip --check-only
uv run maude-lakehouse download deviceadd.zip mdrfoiadd.zip
uv run maude-lakehouse profile data/raw/device/sha256-*.zip
```

Downloads land in `data/raw/<dataset>/`, the manifest is `metadata/ingestion.db`, and profile reports are
written to `metadata/profiles/`. Git ignores all three.

Design decisions are recorded in [`docs/adr/`](docs/adr/); conventions are in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Non-goals

- Not a safety or regulatory analysis product. MAUDE cannot be used to establish incidence rates: reports have
  no denominator, are subject to reporting bias, and may be duplicated.
- No manufacturer comparisons or rankings. Any published analysis is at product-category level.

## Roadmap

1. Spike: download and profile each file family; document what's actually in the files (`device`,
   `mdrfoi`, and `foitext` profiled, results in ADR 0003; patient and problem-code files next)
2. Bronze layer with quarantine and lineage
3. Silver layer with idempotent monthly Add/Change merges
4. Gold models with dbt tests
5. Harold, an optional AI layer, comes later

## License

[MIT](LICENSE)
