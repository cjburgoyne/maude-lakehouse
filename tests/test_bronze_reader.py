""" "Tests for maude_lakehouse.bronze.reader.

Inputs are byte strings read through io.BytesIO, which splits lines the same way iterating an open zip member
does. The cases come from profiling the FDA files (ADR 0003).
"""

import io

import pytest

from maude_lakehouse.bronze.reader import (
    DECODE_ERROR,
    TOO_FEW_FIELDS,
    TOO_MANY_FIELDS,
    FormatError,
    read_header,
    read_records,
)


def parse(data: bytes):
    header, records = read_records(io.BytesIO(data))
    return header, list(records)


def test_clean_files():
    header, records = parse(b"A|B|C\r\n1|x|y\r\n2|p|q\r\n")

    assert header == ["A", "B", "C"]
    assert [r.fields for r in records] == [["1", "x", "y"], ["2", "p", "q"]]
    assert [(r.line_number, r.line_count) for r in records] == [(2, 1), (3, 1)]
    assert all(r.ok and r.encoding == "utf-8" for r in records)


def test_bare_lf_is_kept_inside_the_field():
    _, records = parse(b"A|B\r\n1|first line\nsecond line\r\n2|z\r\n")

    assert records[0].fields == ["1", "first line\nsecond line"]
    assert (records[0].line_number, records[0].line_count) == (2, 2)
    assert (records[1].line_number, records[1].line_count) == (4, 1)


def test_record_spanning_three_lines():
    _, records = parse(b"A|B|C\r\n1|a\nb|c\nd\r\n")

    assert records[0].fields == ["1", "a\nb", "c\nd"]
    assert records[0].line_count == 3


def test_final_line_without_line_ending_is_a_record():
    _, records = parse(b"A|B\r\n1|x\r\n2|y")

    assert [r.fields for r in records] == [["1", "x"], ["2", "y"]]
    assert records[-1].line_number == 3


def test_header_only_file_has_no_records():
    header, records = parse(b"A|B\r\n")

    assert header == ["A", "B"]
    assert records == []


def test_double_quotes_are_literal_text():
    _, records = parse(b'A|B\r\n1|he said "stop|now" twice\r\n2|"quoted"\r\n')

    assert records[0].reason == TOO_MANY_FIELDS  # the | inside quotes is still a delimiter
    assert records[1].fields == ["2", '"quoted"']


def test_cp1252_record_falls_back_from_utf8():
    _, records = parse(b"A|B\r\n1|patient\xbfs device\r\n")

    assert records[0].encoding == "cp1252"
    assert records[0].fields == ["1", "patient¿s device"]


def test_utf8_record_is_decoded_as_utf8():
    _, records = parse("A|B\r\n1|café\r\n".encode())

    assert records[0].encoding == "utf-8"
    assert records[0].fields == ["1", "café"]


def test_encodings_are_chosen_per_record():
    _, records = parse(b"A|B\r\n1|M\xc3\xb6lndal\r\n2|M\xf6lndal\r\n")

    assert [r.encoding for r in records] == ["utf-8", "cp1252"]
    assert records[0].fields[1] == records[1].fields[1] == "Mölndal"


def test_bytes_undefined_in_cp1252_are_a_decode_error():
    _, records = parse(b"A|B\r\n1|bad \x81 byte\r\n2|ok\r\n")

    assert records[0].reason == DECODE_ERROR
    assert records[0].encoding is None
    assert records[0].fields is None
    assert records[0].raw == b"1|bad \x81 byte"
    assert records[1].ok


def test_too_many_and_too_few_fields():
    _, records = parse(b"A|B|C\r\n1|x|y|extra\r\n2|x\r\n3|x|y\r\n")

    assert [r.reason for r in records] == [TOO_MANY_FIELDS, TOO_FEW_FIELDS, None]
    assert [r.field_count for r in records] == [4, 2, 3]
    assert records[0].fields is None
    assert records[0].raw == b"1|x|y|extra"


def test_empty_line_is_a_short_record_not_skipped():
    _, records = parse(b"A|B\r\n\r\n1|x\r\n")

    assert [r.reason for r in records] == [TOO_FEW_FIELDS, None]
    assert records[0].raw == b""


def test_values_are_not_trimmed():
    _, records = parse(b"A|B\r\n 1 |  x\r\n")

    assert records[0].fields == [" 1 ", "  x"]


def test_header_strips_bom_and_spaces():
    assert read_header(b"\xef\xbb\xbfA | B\r\n") == ["A", "B"]


def test_every_line_is_accounted_for():
    data = b"A|B\r\n1|x\r\n2|a\nb\nc\r\n3|x|y\r\n4|\x81\r\n5|y"
    _, records = parse(data)

    physical_lines = data.count(b"\n") + 1  # the last line has no line ending
    assert len(records) == 5
    assert sum(r.line_count for r in records) == physical_lines - 1  # every line after the header


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "empty"),
        (b"A|B\n1|x\n", "CRLF"),
        (b"A|B", "CRLF"),
        (b"A||B\r\n", "blank"),
        (b"A|B|A\r\n", "duplicate"),
        (b"A|\x81\r\n", "neither"),
    ],
)
def test_unusable_files_are_rejected(data, message):
    with pytest.raises(FormatError, match=message):
        read_records(io.BytesIO(data))
