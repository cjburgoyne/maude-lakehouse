# ADR 0003: Bronze layer design

- Status: Proposed
- Date: 2026-11-24

## Context

Landing stores each source zip unchanged and records it in the manifest (ADR 0002). The next step parses those
files into a bronze layer that silver can build on.

MAUDE files are pipe-delimited text with known problems: embedded delimiters and line breaks inside free-text
fields, encoding issues, and columns that change across years. A parser that skips rows it can't read loses data
without anyone noticing, and MAUDE has no row counts of its own to check against later.

Bronze runs outside Databricks (on a laptop or in Github Actions), before files are uploaded to a Unity Catalog
volume. Some details (exact encoding, how line breaks appear, how often rows are malformed) depend on profiling
the actual files and are listed under "To decide".

## Decision

1. **Bronze preserves the source.** Every column is stored as a string, with the column names from the file's
header. No type casting, trimming, or deduplication; those belong in silver, where the rules can be tested and
changed without re-parsing.
2. **Every source record goes to exactly one place: a bronze row or a quarantine row.** For each parsed file:

```
records read = bronze rows + quarantine rows
```

The parser checks this before reporting success. A mismatch fails the run.
3. **Checks in bronze are structural only:**
 - the record decodes in the expected encoding (strict decoding; bad bytes are never silently replaced)
 - the record has the same number of fields as the header

 Records that fail go to quarantine wit hteh raw text (or raw bytes if decoding failed), the reason, and the
 line number. Content checks (valid dates, known codes) are silver's job.
 4. **Lineage columns on every bronze and quarantine row:**

 | Column | Meaning |
 |---|---|
 | `_source_version_id` | `source_file_versions.version_id` from the manifest |
 | `_source_sha256` | SHA-256 of the landed zip |
 | `_source_member` | file name inside the zip |
 | `_source_line_number | line where the record starts (1-based, header is line 1) |
 | `_parser_version` | package version that produced the row |

 Any silver or gold row can be tracked back to a specific line in a specific landed file.
 5. **Output is Parquet, one set of files per source version**, written atomically (temporary file, then rename),
 as landing does:

 ```
 data/bronze/<dataset>/version_id=<id>/data.parquet
 data/bronze/<dataset>/version_id=<id>/quarantine.parquet
 ```

 Parsing the same version again replaces its output rather than adding to it, so reruns are safe. The manifest's
 `processing_status` moves from `pending` to `parsed` (or `failed`) for that version.
 6. **The parser is a streaming reader in Python that writes batches with PyArrow.** It reads to zip member line
 by line, so memory stays flat for large files, and it controls exactly which records are quarantined and
 why. PyArrow is already a dependency.

## Alternatives considered

 - **Typed bronze (cast dates and numbers on read).** Rejected: a bad date would either fail the row or be cast to
 null, and both lose the original value. Casting in silver keeps the source text available.
 - **DuckDB or Polars CSV Readers.** Fast and well tested. Their error-skipping options drop bad rows by default;
 DuckDB can store rejected rows (`store_rejects`), but its control over multi-line records and its reason codes
 are less direct. Worth comparing during profiling; revisit if the Python reader is too slow on the largest files.
 - **Delta tables for bronze.** Rejected for now: bronze runs outside Databricks, and Parquet files can be uploaded
 to a volume and read there directly.
 - **Dropping or repairing bad rows silently.** Rejected: it breaks the row-count check and hides source problems
 that are worth reporting.

## Consequences

 - Bronze is larger than a typed copy would be (strings everywhere); acceptable at this size.
 - Quarantine counts per file become a data quality metric; a suddent drop means FDA changed something.
 - Silver owns all typing rules, so changing a rule means re-running silver, not re-parsing raw files.
 - Each source version can be reparsed on its own, for example after a parser fix.

## To decide (after profiling)

 - **Encoding:** which encoding the files use, and whether it differs between file families or years.
 - **Embedded line breaks:** whether records split across lines can be rejoined reliably (for example, append the
   next line while the field count is short). If so, rejoin and record the number of lines spanned; if not,
   quarantine. Either way, the row-count check counts records, not lines.
 - **Schema drift:** where to record each version's header (Parquet metadata, a manifest table, or both) and
   whether a header change should fail the run or just be flagged.
 - **Performance:** whether the Python reader is fast enough for the largest narrative (`foitext`) files.
 