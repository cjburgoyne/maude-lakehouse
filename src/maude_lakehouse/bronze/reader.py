"""Turn the lines of a MAUDE source file into bronze records (ADR 0003, decisions 7, 8, and 10).

Rules, all taken from profiling the FDA files:

- The header must end with CRLF. A file whose header doesn't is in a format the profiling didn't cover, so it is
  rejected rather than guessed at.
- Records end at CRLF. A bare LF is a line break inside a field: it stays in the value and the record continues
  on the next physical line.
- Each record is decoded strictly as UTF-8, and if that fails, strictly as cp1252. The files mix the two line by
  line. Nothing is ever replaced; a record that fails both is a decode error.
- Fields are split on "|" with no quote handling. Double quotes are literal text.
- A record whose field count differs from the header's is malformed. It is reported, never repaired.

This module does no I/O. It takes an iterable of byte lines, such as an open zip member, and yields one Record per
source record, good or bad, so the caller can check that every record ends up in exactly one place.
"""

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

DELIMITER = "|"
CRLF = b"\r\n"
BOM = b"\xef\xbb\xbf"
ENCODINGS = ("utf-8", "cp1252")

DECODE_ERROR = "decode_error"
TOO_MANY_FIELDS = "too_many_fields"
TOO_FEW_FIELDS = "too_few_fields"


class FormatError(ValueError):
    """The file can't be parsed at all: no header, a header that doesn't end with CRLF, or unusable column names."""


@dataclass(frozen=True, slots=True)
class Record:
    """One source record.

    For a good record, `reason` is None and `fields` has one value per header column. For a malformed record,
    `reason` says why and `fields` is None. `encoding` is None only for decode errors. `raw` is the record's bytes
    without the final CRLF, kept for quarantine.
    """

    line_number: int  # physical line where the record starts; the header is line 1
    line_count: int  # physical lines the record spans
    raw: bytes
    encoding: str | None
    field_count: int | None  # None for decode errors
    fields: list[str] | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.reason is None


def read_header(line: bytes) -> list[str]:
    """Parse the header line into column names. Strips a byte order mark and spaces around names."""
    if not line.endswith(CRLF):
        raise FormatError("Header line does not end with CRLF; this file format hasn't been profiled")

    body = line.removesuffix(CRLF).removeprefix(BOM)

    decoded = _decode(body)
    if decoded is None:
        raise FormatError("Header line is neither valid UTF-8 nor cp1252")
    _, text = decoded

    names = [name.strip() for name in text.split(DELIMITER)]
    blank = [position for position, name in enumerate(names, start=1) if not name]
    if blank:
        raise FormatError(f"Header has blank column names at positions {blank}")
    duplicates = sorted(name for name, n in Counter(names).items() if n > 1)
    if duplicates:
        raise FormatError(f"Header has duplicate column names: {duplicates}")
    return names


def read_records(lines: Iterable[bytes]) -> tuple[list[str], Iterator[Record]]:
    """Read the header from the first line and return it with an iterator over the remaining records.

    Raises FormatError right away if the header is missing or unusable.
    """
    iterator = iter(lines)
    first = next(iterator, None)
    if first is None:
        raise FormatError("File is empty; expected a header line")

    header = read_header(first)
    return header, _records(iterator, len(header))


def _records(lines: Iterator[bytes], expected: int) -> Iterator[Record]:
    for line_number, line_count, raw in _split(lines, first_line_number=2):
        decoded = _decode(raw)
        if decoded is None:
            yield Record(line_number, line_count, raw, None, None, None, DECODE_ERROR)
            continue

        encoding, text = decoded
        fields = text.split(DELIMITER)
        count = len(fields)
        if count == expected:
            yield Record(line_number, line_count, raw, encoding, count, fields, None)
        else:
            reason = TOO_MANY_FIELDS if count > expected else TOO_FEW_FIELDS
            yield Record(line_number, line_count, raw, encoding, count, None, reason)


def _split(lines: Iterator[bytes], first_line_number: int) -> Iterator[tuple[int, int, bytes]]:
    """Group physical lines into records ending at CRLF. Yields (start line, line count, bytes without CRLF).

    Lines come from iterating a binary file, so each one ends with b"\\n" except possibly the last. A line ending
    in a bare LF is kept whole (its LF belongs to the value) and joined with the lines after it. A final line with
    no line ending is still a record.
    """
    pending: list[bytes] = []
    start = first_line_number
    number = first_line_number - 1

    for line in lines:
        number += 1
        if line.endswith(CRLF):
            if pending:
                pending.append(line[: -len(CRLF)])
                yield start, len(pending), b"".join(pending)
                pending = []
            else:
                yield number, 1, line[: -len(CRLF)]
            start = number + 1
        else:
            pending.append(line)

    if pending:
        yield start, len(pending), b"".join(pending)


def _decode(raw: bytes) -> tuple[str, str] | None:
    """Return (encoding, text) for the first encoding that decodes strictly, or None."""
    if raw.isascii():
        return ENCODINGS[0], raw.decode("ascii")
    for encoding in ENCODINGS:
        try:
            return encoding, raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return None
