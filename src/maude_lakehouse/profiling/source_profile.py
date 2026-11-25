"""Profile MAUDE source zips before building the bronze parser.

Answers the open questions in ADR 0003: which encoding the files use, how lines end, whether records split across
lines can be rejoined, which records have extra delimiters, what each column looks like, and how fast a
line-by-line Python reader is. Zip members are read as bytes, one line at a time, so memory stays flat.

Sample values and raw lines can contain manufacturer names and narrative text. They are collected only when
include_samples=True, and reports that contain them must not be committed.
"""

import json
import re
import string
import time
from collections import Counter
from pathlib import Path
from zipfile import ZipFile, ZipInfo

DELIMITER = b"|"
BOM = b"\xef\xbb\xbf"
MAX_SPAN = 50  # give up rejoining a record after this many physical lines
DISTINCT_CAP = 10_000
SHAPE_CAP = 1_000
SHAPE_MAX_LENGTH = 30
SAMPLE_LIMIT = 5
SAMPLE_TEXT_LENGTH = 200

_SHAPE_TABLE = str.maketrans({**dict.fromkeys(string.digits, "9"), **dict.fromkeys(string.ascii_letters, "A")})
_LETTER_RUNS = re.compile("A+")


def decode(body: bytes) -> str:
    """Best effort decode for statistics only. Bronze will decode strictly (ADR 0003)."""
    for encoding in ("utf-8", "cp1252"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            pass
    return body.decode("latin-1")


def value_shape(value: str) -> str:
    """Summarize a value's format: digits become 9, runs of letters become A. "03/15/2024" -> "99/99/9999"."""
    if len(value) > SHAPE_MAX_LENGTH:
        return "<long text>"
    return _LETTER_RUNS.sub("A", value.translate(_SHAPE_TABLE))


class _ColumnStats:
    def __init__(self, name: str, include_samples: bool) -> None:
        self.name = name
        self.include_samples = include_samples
        self.empty = 0
        self.padded = 0
        self.max_length = 0
        self.distinct: set[str] = set()
        self.distinct_capped = False
        self.shapes: Counter[str] = Counter()
        self.samples: list[str] = []

    def add(self, value: str) -> None:
        if not value.strip():
            self.empty += 1
            return
        if value != value.strip():
            self.padded += 1
        self.max_length = max(self.max_length, len(value))

        if value not in self.distinct:
            if len(self.distinct) < DISTINCT_CAP:
                self.distinct.add(value)
            else:
                self.distinct_capped = True

        shape = value_shape(value)
        if shape in self.shapes or len(self.shapes) < SHAPE_CAP:
            self.shapes[shape] += 1

        if self.include_samples and len(self.samples) < SAMPLE_LIMIT and value not in self.samples:
            self.samples.append(value[:50])

    def to_dict(self, records: int) -> dict:
        result = {
            "name": self.name,
            "empty_pct": round(100 * self.empty / records, 2) if records else 0.0,
            "distinct": len(self.distinct),
            "distinct_capped": self.distinct_capped,
            "max_length": self.max_length,
            "padded": self.padded,
            "top_shapes": dict(self.shapes.most_common(5)),
        }
        if self.include_samples:
            result["samples"] = self.samples
        return result


class _MemberProfiler:
    """Accumulates statistics for one zip member, fed one physical line at a time."""

    def __init__(self, include_samples: bool) -> None:
        self.include_samples = include_samples
        self.header: list[str] | None = None
        self.columns: list[_ColumnStats] = []
        self.has_bom = False
        self.lines = 0
        self.line_endings: Counter[str] = Counter()
        self.lines_with_bare_cr = 0
        self.lines_with_quotes = 0
        self.encoding: Counter[str] = Counter()
        self.non_utf8_bytes: Counter[int] = Counter()
        self.non_utf8_line_numbers: list[int] = []
        self.field_counts: Counter[int] = Counter()
        self.span_counts: Counter[int] = Counter()  # complete records by number of physical lines
        self.failures: Counter[str] = Counter()
        self.lines_accounted = 0
        self.samples: dict[str, list[dict]] = {}
        self.pending: list[bytes] = []
        self.pending_start = 0
        self.pending_fields = 0

    @property
    def records(self) -> int:
        return self.span_counts.total() + self.failures.total()

    def feed(self, line_number: int, raw: bytes) -> None:
        self.lines += 1
        body = self._strip_line_ending(raw)

        if self.header is None:
            self._read_header(body)
            return

        self._check_encoding(line_number, body)
        if b'"' in body:
            self.lines_with_quotes += 1

        field_count = body.count(DELIMITER) + 1
        self.field_counts[field_count] += 1
        self._assemble(line_number, body, field_count)

    def finish(self) -> None:
        if self.pending:
            self._fail("short_record", self.pending_start, self.pending)

    def _strip_line_ending(self, raw: bytes) -> bytes:
        if raw.endswith(b"\r\n"):
            self.line_endings["crlf"] += 1
            body = raw[:-2]
        elif raw.endswith(b"\n"):
            self.line_endings["lf"] += 1
            body = raw[:-1]
        else:
            self.line_endings["none"] += 1
            body = raw
        if b"\r" in body:
            self.lines_with_bare_cr += 1
        return body

    def _read_header(self, body: bytes) -> None:
        if body.startswith(BOM):
            self.has_bom = True
            body = body[len(BOM) :]
        self.header = [name.strip() for name in decode(body).split("|")]
        self.columns = [_ColumnStats(name, self.include_samples) for name in self.header]
        self.lines_accounted += 1

    def _check_encoding(self, line_number: int, body: bytes) -> None:
        if body.isascii():
            self.encoding["ascii"] += 1
            return
        try:
            body.decode("utf-8")
            self.encoding["utf8"] += 1
            return
        except UnicodeDecodeError:
            pass

        self.non_utf8_bytes.update(byte for byte in body if byte >= 0x80)
        if len(self.non_utf8_line_numbers) < SAMPLE_LIMIT:
            self.non_utf8_line_numbers.append(line_number)
        try:
            body.decode("cp1252")
            self.encoding["cp1252"] += 1
        except UnicodeDecodeError:
            self.encoding["undecodable"] += 1

    def _assemble(self, line_number: int, body: bytes, field_count: int) -> None:
        """Group physical lines into records the way a rejoining parser would.

        A line with too few fields starts a pending record. Following lines are appended while the combined field
        count is still short (the last field of one line and the first of the next are one field split by a line
        break). Reaching the header's count exactly completes the record; overshooting means the pending lines were
        a short record on their own, and the current line is then judged by itself.
        """
        expected = len(self.header)

        if self.pending:
            combined = self.pending_fields + field_count - 1
            lines = [*self.pending, body]
            if combined == expected:
                self._complete(self.pending_start, lines)
                self.pending = []
                return
            if combined < expected and len(lines) < MAX_SPAN:
                self.pending = lines
                self.pending_fields = combined
                return
            self._fail("short_record", self.pending_start, self.pending)
            self.pending = []

        if field_count == expected:
            self._complete(line_number, [body])
        elif field_count > expected:
            self._fail("too_many_fields", line_number, [body])
        else:
            self.pending = [body]
            self.pending_start = line_number
            self.pending_fields = field_count

    def _complete(self, line_number: int, lines: list[bytes]) -> None:
        self.span_counts[len(lines)] += 1
        self.lines_accounted += len(lines)
        body = b"\n".join(lines)
        for column, value in zip(self.columns, decode(body).split("|"), strict=True):
            column.add(value)
        if len(lines) > 1:
            self._sample("rejoined", line_number, body)

    def _fail(self, reason: str, line_number: int, lines: list[bytes]) -> None:
        self.failures[reason] += 1
        self.lines_accounted += len(lines)
        self._sample(reason, line_number, b"\n".join(lines))

    def _sample(self, kind: str, line_number: int, body: bytes) -> None:
        if not self.include_samples:
            return
        bucket = self.samples.setdefault(kind, [])
        if len(bucket) < SAMPLE_LIMIT:
            bucket.append({"line": line_number, "text": decode(body)[:SAMPLE_TEXT_LENGTH]})

    def to_dict(self) -> dict:
        header = self.header or []
        complete_records = self.span_counts.total()
        result = {
            "header": header,
            "column_count": len(header),
            "duplicate_columns": sorted(name for name, n in Counter(header).items() if n > 1),
            "has_bom": self.has_bom,
            "lines": self.lines,
            "unaccounted_lines": self.lines - self.lines_accounted,
            "line_endings": dict(self.line_endings),
            "lines_with_bare_cr": self.lines_with_bare_cr,
            "lines_with_quotes": self.lines_with_quotes,
            "encoding": dict(self.encoding),
            "non_utf8_bytes": {f"0x{byte:02X}": n for byte, n in self.non_utf8_bytes.most_common(10)},
            "non_utf8_line_numbers": self.non_utf8_line_numbers,
            "records": {
                "total": self.records,
                "single_line": self.span_counts[1],
                "rejoined": complete_records - self.span_counts[1],
                "too_many_fields": self.failures["too_many_fields"],
                "short_record": self.failures["short_record"],
            },
            "rejoined_spans": {str(span): n for span, n in sorted(self.span_counts.items()) if span > 1},
            "field_counts": {str(count): n for count, n in self.field_counts.most_common(10)},
            "columns": [column.to_dict(complete_records) for column in self.columns],
        }
        if self.include_samples:
            result["samples"] = self.samples
        return result


def profile_member(
    archive: ZipFile, info: ZipInfo, *, max_records: int | None = None, include_samples: bool = False
) -> dict:
    profiler = _MemberProfiler(include_samples)
    truncated = False
    started = time.perf_counter()

    with archive.open(info) as member:
        for line_number, raw in enumerate(member, start=1):
            profiler.feed(line_number, raw)
            if max_records is not None and profiler.records >= max_records:
                truncated = True
                break
    profiler.finish()

    elapsed = time.perf_counter() - started
    return {
        "member": info.filename,
        "uncompressed_bytes": info.file_size,
        "truncated": truncated,
        "elapsed_seconds": round(elapsed, 3),
        "lines_per_second": round(profiler.lines / elapsed) if elapsed > 0 else None,
        **profiler.to_dict(),
    }


def profile_zip(path: Path, *, max_records: int | None = None, include_samples: bool = False) -> dict:
    """Profile every file inside a source zip."""
    with ZipFile(path) as archive:
        members = [
            profile_member(archive, info, max_records=max_records, include_samples=include_samples)
            for info in archive.infolist()
            if not info.is_dir()
        ]
    return {"source": path.name, "file_size_bytes": path.stat().st_size, "members": members}


def compare_headers(profiles: list[dict]) -> list[dict]:
    """Compare each member's header with the first member's. Pass files from the same family."""
    members = [(f"{p['source']}/{m['member']}", m["header"]) for p in profiles for m in p["members"]]
    if not members:
        return []

    _, baseline = members[0]
    differences = []
    for name, header in members[1:]:
        added = [column for column in header if column not in baseline]
        removed = [column for column in baseline if column not in header]
        reordered = not added and not removed and header != baseline
        if added or removed or reordered:
            differences.append({"file": name, "added": added, "removed": removed, "reordered": reordered})
    return differences


def render_markdown(profiles: list[dict]) -> str:
    lines = ["# Source profile", ""]

    for profile in profiles:
        for m in profile["members"]:
            records = m["records"]
            lines += [
                f"## {profile['source']} / {m['member']}",
                "",
                "| Measure | Value |",
                "|---|---|",
                f"| Uncompressed size | {m['uncompressed_bytes']:,} bytes |",
                f"| Lines | {m['lines']:,}{' (truncated)' if m['truncated'] else ''} |",
                f"| Unaccounted lines | {m['unaccounted_lines']} |",
                f"| Columns | {m['column_count']} |",
                f"| Duplicate column names | {', '.join(m['duplicate_columns']) or 'none'} |",
                f"| Byte order mark | {'yes' if m['has_bom'] else 'no'} |",
                f"| Line endings | {_counts(m['line_endings'])} |",
                f"| Lines with a bare CR | {m['lines_with_a_bare_cr']:,} |",
                f"| Lines with a double quote | {m['lines_with_quotes']:,} |",
                f"| Encoding (data lines) | {_counts(m['encoding'])} |",
                f"| Non-UTF-8 bytes (top) | {_counts(m['non_utf8_bytes']) or 'none'} |",
                f"| Records | {records['total']:,} |",
                f"| Single-line records | {records['single_line']:,} |",
                f"| Rejoined records | {records['rejoined']:,} (spans: {_counts(m['rejoined_spans']) or 'none'}) |",
                f"| Too many fields | {records['too_many_fields']:,} |",
                f"| Short records | {records['short_record']:,} |",
                f"| Lines per second | {m['lines_per_second'] or 'n/a'} |",
                "",
                "| Column | Empty % | Distinct | Max length | Padded | Top shapes |",
                "|---|---|---|---|---|---|",
            ]
            for c in m["columns"]:
                distinct = f"{c['distinct']:,}{'+' if c['distinct_capped'] else ''}"
                shapes = ", ".join(f"`{shape}` ({n:,})" for shape, n in c["top_shapes"].items())
                lines.append(
                    f"| {c['name']} | {c['empty_pct']} | {distinct} | {c['max_length']} | {c['padded']} | {shapes} |"
                )
            lines.append("")

    differences = compare_headers(profiles)
    lines += ["## Header comparison", ""]
    if not differences:
        lines.append("All headers match the first file.")
    else:
        lines += ["| File | Added | Removed | Reordered |", "|---|---|---|---|"]
        for d in differences:
            lines.append(
                f"| {d['file']} | {', '.join(d['added'])} | {', '.join(d['removed'])} | {'yes' if d['reordered'] else ''} |"
            )
    lines.append("")
    return "\n".join(lines)


def write_reports(profiles: list[dict], out_dir: Path) -> dict:
    """Write one JSON file per source zip and a combined Markdown report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_paths = []
    for profile in profiles:
        path = out_dir / f"{Path(profile['source']).stem}.json"
        path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        json_paths.append(str(path))

    report_path = out_dir / "report.md"
    report_path.write_text(render_markdown(profiles), encoding="utf-8")
    return {"json": json_paths, "report": str(report_path)}


def _counts(counts: dict) -> str:
    return ", ".join(f"{key}: {n:,}" for key, n in counts.items())
