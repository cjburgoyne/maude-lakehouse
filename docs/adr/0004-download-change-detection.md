# ADR 0004: Detecting changed FDA files before downloading

- Status: Accepted
- Date: 2026-11-24

## Context

FDA publishes MAUDE as zip files at fixed URLs (`https://www.accessdata.fda.gov/MAUDE/ftparea/<name>.zip`) and
replaces them in place: teh monthly Add and Change files every month, the current-year files as they grow, and
the annual files occasionally. A scheduled run has to find out which files changed without downloading all of
them. The largest files are several gigabytes.

Checkced against the FDA server on 2026-11-24:

| Request | Result |
|---|---|
| `HEAD` | 200 with `Last-Modified`, `Content-Length`, and `ETag` (IIS format, e.g. `"80f8f40f076dd1:0"`) |
| Same `HEAD` repeated six times | same `ETag` and `Last-Modified` every time |
| `GET` with `If-Modified-Since` set to the file's `Last-Modified` | 304 Not Modified |
| `GET` with `If-None-Match` set to the file's `ETag` | 200: the full file. The server ignores `If-None-Match` |
| `HEAD` on a file that doesn't exist | 404 |

## Decision

1. **Check each file with `HEAD` first.** Compare `Last-Modified` and `Content-Length` with the last download of
   the same URL, and `ETag` when both sides have one. If they all match, skip the file. Otherwise download it.
2. **When the server doesn't send `Last-Modified` or `Content-Length`, download.** Landing recognizes identical
   content by SHA-256, so an unnecessary download costs bandwidth but never creates a duplicate version.
3. **Record every check** in a manifest table, `source_file_checks`: URL, time, the three headers, whether the
   file was downloaded or skipped, and the version it maps to. The comparison uses the headers from the most
   recent download, taken from the `GET` response so they describe the bytes actually received.
4. **Check the whole file arrived:** the number of bytes written must equal the `GET` response's
   `Content-Length`. Request `Accept-Encoding: identity` so the two are comparable.
5. **Download to a temporary folder under `data/downloads/`, keeping the FDA file name**, then land it. The
   temporary folder is removed whether landing succeds or fails.
6. **`--check-only`** reports `new`, `changed`, or `unchanged` per file using only `HEAD` and changes nothing.
   `--force` downloads regardless.

## Alternatives considered

- **Conditional `GET` with `If-Modified-Since`.** One request instead of two, and the server supports it. Not
  chosen as the main check: it relies on the date alone, and `HEAD` also gives the size and ETag up front, which
  `--check-only` reports. The second request is small next to a download. Coule be added as a fallback.
_ **`If-None-Match` with the ETag.** The server ignores it (returns 200 and the whole file).
- **Always download and compare SHA-256.** Simplest and never wrong, but downloads gigabytes every run.
- **Scrape the FDA download page for dates and sizes.** Fragile. The page is for people, and its layout can
  change. Worth doing once to build a list of file names, not for change detection.

## Consequences

- A monthly run that finds nothing new makes one small request per file.
- A file whose timestamp changes but whose content doesn't (a re-upload) is downloaded once more, then landing
  records an observation without a new version.
- The check history shows when FDA changed each file, which helps answer the spike's questions about when Add
  and Chnage files are replaced.
- If the server's behavior changes (ETag format, missing headers), the worst case is extra downloads, not missed
  changes. The one ga: a file replaced with a different content but the same size, timestamp,and ETag
  would be skipped. `--force` covers that.
