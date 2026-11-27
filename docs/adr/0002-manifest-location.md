# ADR 0002: Where the landing manifest lives

- Status: Proposed
- Date: 2026-11-23 (context updated 2026-11-26)

## Context

The landing step records every source file in a manifest: one row per unique content
(`source_file_versions`) and one row per time a file is seen (`source_file_observations`). The downloader adds
a third table, `source_file_checks`, with one row per check of an FDA URL (ADR 0004). Today the manifest is a
SQLite file in `metadata/ingestion.db`, next to the raw files.

Processing into silver and gold will run on Databricks Free Edition. Free Edition restricts outbound internet
access to a limited set of trusted domains, so downloading from the FDA has to happen outside the workspace (a
laptop or GitHub Actions). Raw files then get uploaded to a Unity Catalog volume. Landing and processing
therefore run in different places, and both need the manifest: landing writes it, processing reads it and
updates `processing_status`.

## Options

1. **SQLite stored alongside the raw files**, uploaded with them and read by Databricks.
   - Simple, already built, no extra services.
   - Two writers (landing outside, status updates inside Databricks) can conflict; SQLite on object storage
     isn't safe for concurrent writes.
2. **Delta table in Unity Catalog** as the manifest; the landing CLI writes to it through the Databricks SDK or
   SQL statement API after uploading.
   - One source of truth where processing happens; queryable with the rest of the lakehouse; time travel on
     the manifest itself.
   - Landing depends on Databricks being reachable; local development needs a stand-in.
3. **Split:** SQLite stays the landing system's record; Databricks keeps its own processing-status table,
   loaded from the uploaded files (or a manifest export) on each run.
   - Each side owns its own writes, so no shared-writer problem.
   - Two records to reconcile; needs a check that every landed version was processed.

## Leaning

Option 3: it keeps a single writer for each table and works with Free Edition's network limits. Reconciling
landed versions against processed versions is also a useful data quality check in its own right. The download
checks support this split: they are written and read only where downloading happens, and Databricks never needs
them.

## To decide

- Confirm how files reach the Unity Catalog volume (Databricks CLI or SDK from the landing machine).
- Confirm Free Edition quotas for SQL statements and jobs are enough for monthly runs.
- Decide whether the SQLite manifest itself is archived with each upload (for replay).
