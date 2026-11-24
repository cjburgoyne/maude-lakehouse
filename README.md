# maude-lakehouse

A lakehouse for the FDA's MAUDE medical device adverse event data: raw public files landed unchanged, parsed
into a source-preserving bronze layer, and processed into typed silver tables and targeted gold models.

**Status:** early. File landing (checksums, content-addressed storage, manifest) is in place; parsing and the
bronze layer are in progress.

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
FDA download page
      │  download, validate, checksum
      ▼
raw         original zip files, unchanged, content-addressed (sha256), recorded in a manifest
      │  parse; quarantine rows that fail, never drop them silently
      ▼
bronze      source-preserving tables with file-level lineage
      │  merge monthly Add/Change files (CDC), type and deduplicate
      ▼
silver      typed, deduplicated tables (Databricks Free Edition, Delta Lake)
      │  dbt models with tests
      ▼
gold        targeted dimensional models
```

## What's here now

- `src/maude_lakehouse/ingestion/landing.py`: validates a source zip, stores it by SHA-256 so identical files
  are never duplicated, copies atomically (temporary file, verify, rename), and records each file in a SQLite
  manifest with its source URL, size, and processing status.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
uv run pytest
```

Design decisions are recorded in [`docs/adr/`](docs/adr/); conventions are in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Non-goals

- Not a safety or regulatory analysis product. MAUDE cannot be used to establish incidence rates: reports have
  no denominator, are subject to reporting bias, and may be duplicated.
- No manufacturer comparisons or rankings. Any published analysis is at product-category level.

## Roadmap

1. Parse and profile each file family; document what's actually in the files
2. Bronze layer with quarantine and lineage
3. Silver layer with idempotent monthly Add/Change merges
4. Gold models with dbt tests
5. Harold, an optional AI layer, comes later

## License

TBD
