# ADR 0003: Bronze layer design

- Status: Accepted
- Date: 2026-11-24 (accepted 2026-11-27, after profiling)

## Context

Landing stores each source zip unchanged and records it in the manifest (ADR 0002). The next step parses those
files into a bronze layer that silver can build on.

MAUDE files are pipe-delimited text with known problems: embedded delimiters and line breaks inside free-text
fields, encoding issues, and columns that change across years. A parser that skips rows it can't read loses data
without anyone noticing, and MAUDE has no row counts of its own to check against later.

Bronze runs outside Databricks (on a laptop or in GitHub Actions), before files are uploaded to a Unity Catalog
volume. Some details (exact encoding, how line breaks appear, how often rows are malformed) depended on profiling
the actual files; decisions 7 to 10 record what profiling found, with the evidence under "Profiling results".

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
   - the record decodes strictly as UTF-8 or cp1252 (decision 8; bad bytes are never silently replaced)
   - the record has the same number of fields as the header

   Records that fail go to quarantine with the raw text (or raw bytes if decoding failed), the reason, and the
   line number. Content checks (valid dates, known codes) are silver's job.
4. **Lineage columns on every bronze and quarantine row:**

   | Column | Meaning |
   |---|---|
   | `_source_version_id` | `source_file_versions.version_id` from the manifest |
   | `_source_sha256` | SHA-256 of the landed zip |
   | `_source_member` | file name inside the zip |
   | `_source_line_number` | line where the record starts (1-based, header is line 1) |
   | `_source_line_count` | physical lines the record spans (more than 1 when a field contains a line break) |
   | `_source_encoding` | `utf-8` or `cp1252`, whichever decoded the record (decision 8); null if neither did |
   | `_parser_version` | package version that produced the row |

   Any silver or gold row can be traced back to a specific line in a specific landed file.
5. **Output is Parquet, one set of files per source version**, written atomically (temporary file, then rename),
   as landing does:

   ```
   data/bronze/<dataset>/version_id=<id>/data.parquet
   data/bronze/<dataset>/version_id=<id>/quarantine.parquet
   ```

   Parsing the same version again replaces its output rather than adding to it, so reruns are safe. The manifest's
   `processing_status` moves from `pending` to `parsed` (or `failed`) for that version.
6. **The parser is a streaming reader in Python that writes batches with PyArrow.** It reads the zip member line
   by line, so memory use stays flat for large files, and it controls exactly which records are quarantined and
   why. PyArrow is already a dependency. Profiling showed it is fast enough (see "Profiling results").
7. **Records end at CRLF.** The reader splits on `\r\n` only. A bare `\n` is a line break inside a field and stays
   in the value. The field-count check still runs on every record; in the profiled files it never failed for a
   record split this way. Fields are split on `|` with no quote handling: double quotes are literal text.
8. **Each record is decoded strictly as UTF-8, and if that fails, strictly as cp1252.** The files mix the two
   line by line. Whichever succeeds is stored in `_source_encoding`. A record that fails both goes to quarantine
   with its raw bytes (reason `decode_error`).
9. **Columns are mapped by header name, not position.** Each version's header is recorded in the manifest (one
   row per column: version, member, position, name) and in the Parquet schema metadata. A header not seen before
   for the dataset is reported in the run output but does not fail the run. A header with blank or duplicate
   names fails the file, since its columns can't be mapped by name.
10. **Malformed records are quarantined, not repaired.** A record with too many fields goes to quarantine with
    reason `too_many_fields`; one with too few, `too_few_fields`. Shifted fields are not guessed back into place.

## Alternatives considered

- **Typed bronze (cast dates and numbers on read).** Rejected: a bad date would either fail the row or be cast to
  null, and both lose the original value. Casting in silver keeps the source text available.
- **DuckDB or Polars CSV readers.** Fast and well tested. Their error-skipping options drop bad rows by default;
  DuckDB can store rejected rows (`store_rejects`), but its control over multi-line records and its reason codes
  are less direct. Not needed: profiling showed the Python reader is fast enough.
- **Rejoining split records by field count** (append lines while the record is short). Rejected: it agreed with
  the CRLF rule on every profiled file, but the CRLF rule is exact and needs no lookahead.
- **One encoding per file.** Rejected: every fully profiled file with non-ASCII text has both UTF-8 and cp1252
  records, so a per-file choice would either fail strict decoding or garble valid UTF-8.
- **Repairing shifted records** (for example, dropping an empty field next to an extra delimiter). Rejected: the
  shift is in a different place in each case, and there are too few cases to test a rule against.
- **Delta tables for bronze.** Rejected for now: bronze runs outside Databricks, and Parquet files can be uploaded
  to a volume and read there directly.
- **Dropping or repairing bad rows silently.** Rejected: it breaks the row-count check and hides source problems
  that are worth reporting.

## Consequences

- Bronze is larger than a typed copy would be (strings everywhere); acceptable at this size.
- Quarantine counts per file become a data quality metric: a sudden jump means FDA changed something.
- Silver owns all typing rules, so changing a rule means re-running silver, not re-parsing raw files.
- Each source version can be re-parsed on its own, for example after a parser fix.
- FDA can republish a file with a different header at the same URL (device files have been rebuilt this way).
  Change detection (ADR 0004) downloads the new version, and decision 9 parses it without code changes.
- Silver needs a union schema per dataset, a date format per source file, and its own rule for device identity
  (see "Profiling results").

## Profiling results (2026-11-26 to 2026-11-27)

Profiled with `maude-lakehouse profile`: `device` thru 1997 to 2025 plus current, Add, and Change (1999 to 2023
on the first 1,000 records only); `mdrfoi` thru 2025, current, Add, and Change; `foitext` thru 1995, 2024, 2025,
current, Add, and Change.

**Line breaks.** Every record ends with CRLF. Bare LF lines occur only in `device` and `mdrfoi`, and in every file
their count equals the extra lines of the records that span more than one line (for example 3,341 in
`mdrfoithru2025`: 3,333 two-line and 4 three-line records). `foitext` has none. No file has a bare CR.

**Encoding.** No record failed both UTF-8 and cp1252. Non-ASCII records are mostly cp1252, with a few hundred to a
few thousand valid UTF-8 records per large file. `0xBF` (`¿`) is the most common non-ASCII byte in `foitext`
(1.1 to 1.5 million per large file) and appears to stand for characters lost before FDA published the files.
Bronze keeps it; silver decides how to treat it.

**Malformed records.** 3 records with too many fields in `mdrfoithru2025` (23.6 million records), all from 2022
to 2024 reports, with the extra field in the manufacturer contact address. None in any other file. No short
records anywhere.

**Headers.** `mdrfoi` (86 columns) and `foitext` (6 columns) are the same in every profiled file. `device` has
three versions, which follow when FDA last rebuilt each file rather than the year of the data:

| Device files | Columns | Last modified on the FDA site | Date format |
|---|---|---|---|
| `foidevthru1997`, `foidev1998`, `foidev1999` | 45 | 2013 to 2015 | `MM/DD/YYYY` |
| `device2000`, `device2002` to `device2012` | 31 | October to November 2022 | `YYYY/MM/DD` |
| `device2001`, `device2013` to `device2025`, current, Add, Change | 34 | December 2025 or later | `YYYY/MM/DD` |

Only the 45-column files populate `DEVICE_EVENT_KEY`; they also have 17 `BASELINE_*` columns and lack the UDI and
combination-product columns; the 34-column files add `IMPLANT_DATE_YEAR`, `DATE_REMOVED_YEAR`, and
`SERVICED_BY_3RD_PARTY_FLAG`. In the 34-column files `DEVICE_SEQUENCE_NO` is not a per-report sequence (in the
current-year file it runs from 1 to the row count), so silver can't use it as part of a key.

**Performance.** The profiler, which does more work per line than the parser will, read 160,000 to 195,000
lines per second on `foitext`, 53,000 to 81,000 on `device`, and 40,000 to 46,000 on `mdrfoi` (86 columns). The
largest file, `mdrfoithru2025` (6.8 GB uncompressed), took about 8.5 minutes; `foitext2025` (3.8 GB) about 40
seconds.

**For silver.** Double quotes are literal text (about 319,000 lines in `foitext2024`). `*` is used as a
placeholder in many code and flag columns. Some values have leading or trailing spaces. `mdrfoi` dates are
`MM/DD/YYYY` except `DATE_FACILITY_AWARE` (`YYYY/MM/DD HH:MM:SS.fff`), and `SUPPL_DATES_*` hold `;`-separated
lists. About 25 `mdrfoi` columns and `foitext.DATE_REPORT` are empty in current files. Single `FOI_TEXT` values
reach about 10 million characters.
