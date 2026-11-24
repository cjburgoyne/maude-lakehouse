# ADR 0001: Project structure and entry point

- Status: Accepted
- Date: 2026-11-23

## Context

The first module (`ingestion/landing.py`) is a set of functions. More steps are coming (download, parse, upload
to Databricks), and they need to be run in order from a laptop, from GitHub Actions, and eventually from
Databricks jobs. The question is where that orchestration lives.

## Decision

1. All pipeline logic lives in the installable package under `src/maude_lakehouse/`.
2. Orchestration from the command line goes through one thin entry point, `maude_lakehouse/cli.py`, registered
   in `pyproject.toml` as `maude-lakehouse`. Each step is a subcommand (`land` now; `download`, `parse`,
   `upload` later) that parses arguments, calls library code, and prints the result.
3. Modules are split by responsibility, only when there's a reason to:

   ```
   src/maude_lakehouse/
     cli.py              thin command-line entry point
     ingestion/
       landing.py        land_file: validate, hash, store, record
       manifest.py       SQLite manifest (versions and observations)
     parsing/            bronze parsing (next)
   tests/
   notebooks/            exploration only, outputs stripped
   ```

   `integrity.py` (hashing and zip validation) and `config.py` (storage locations per environment) are split out
   when they're needed, not before.

## Alternatives considered

- **A `scripts/` folder of orchestration scripts.** Rejected: scripts outside the package import it through
  path workarounds, aren't installed with it, and are rarely tested. Fine for one-off admin tasks, not for the
  pipeline.
- **Notebooks as the pipeline.** Rejected: hard to test, review, and version. Notebooks stay for exploration.
- **A CLI framework (Typer, Click).** Not needed yet; `argparse` from the standard library is enough for a few
  subcommands and adds no dependency. Revisit if the CLI grows.
- **Classes or plugin interfaces for storage and steps.** Rejected for now: there is only one implementation of
  each. Add an abstraction when a second implementation exists.

## Consequences

- `uv run maude-lakehouse land <zip> --dataset device` works anywhere the package is installed.
- The same entry point can run as a Databricks job task from a built wheel (Python wheel task in Databricks
  Asset Bundles) without restructuring.
- Library functions stay testable without the CLI; the CLI gets its own small tests.
