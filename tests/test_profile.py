import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from maude_lakehouse.cli import main
from maude_lakehouse.profiling.source_profile import compare_headers, profile_zip, render_markdown, value_shape


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def profile_bytes(tmp_path: Path, content: bytes, **kwargs) -> dict:
    """Profile a zip with one member and return that member's profile."""
    path = make_zip(tmp_path / "source.zip", {"source.txt": content})
    return profile_zip(path, **kwargs)["members"][0]


def test_clean_file(tmp_path):
    m = profile_bytes(tmp_path, b"A|B|C\r\n1|x|y\r\n2|p|q\r\n")

    assert m["header"] == ["A", "B", "C"]
    assert m["lines"] == 3
    assert m["line_endings"] == {"crlf": 3}
    assert m["encoding"] == {"ascii": 2}
    assert m["records"] == {
        "total": 2,
        "single_line": 2,
        "rejoined": 0,
        "too_many_fields": 0,
        "short_record": 0,
    }
    assert m["unaccounted_lines"] == 0


def test_record_split_by_line_break_is_rejoined(tmp_path):
    m = profile_bytes(tmp_path, b"A|B|C\n1|hello\nworld|z\n2|p|q\n")

    assert m["records"]["single_line"] == 1
    assert m["records"]["rejoined"] == 1
    assert m["rejoined_spans"] == {"2": 1}
    assert m["columns"][1]["max_length"] == len("hello\nworld")
    assert m["unaccounted_lines"] == 0


def test_record_split_over_three_lines(tmp_path):
    m = profile_bytes(tmp_path, b"A|B|C\n1|one\ntwo\nthree|z\n")

    assert m["records"]["rejoined"] == 1
    assert m["rejoined_spans"] == {"3": 1}


def test_extra_delimiter_counts_as_too_many_fields(tmp_path):
    m = profile_bytes(tmp_path, b"A|B\n1|x|extra\n2|y\n")

    assert m["records"]["too_many_fields"] == 1
    assert m["records"]["single_line"] == 1
    assert m["field_counts"] == {"3": 1, "2": 1}


def test_short_record_followed_by_good_record(tmp_path):
    m = profile_bytes(tmp_path, b"A|B|C\n1|x\n2|p|q\n")

    assert m["records"]["short_record"] == 1
    assert m["records"]["single_line"] == 1
    assert m["unaccounted_lines"] == 0


def test_short_record_at_end_of_file(tmp_path):
    m = profile_bytes(tmp_path, b"A|B|C\n1|x\n")

    assert m["records"]["short_record"] == 1
    assert m["line_endings"] == {"lf": 2}


def test_encoding_counts(tmp_path):
    content = b"A|B\n1|plain\n2|caf\xc3\xa9\n3|caf\xe9\n4|bad\x81\n"
    m = profile_bytes(tmp_path, content)

    assert m["encoding"] == {"ascii": 1, "utf8": 1, "cp1252": 1, "undecodable": 1}
    assert m["non_utf8_bytes"] == {"0xE9": 1, "0x81": 1}
    assert m["non_utf8_line_numbers"] == [4, 5]


def test_byte_order_mark_is_detected_and_stripped(tmp_path):
    m = profile_bytes(tmp_path, b"\xef\xbb\xbfA|B\n1|2\n")

    assert m["has_bom"] is True
    assert m["header"] == ["A", "B"]


def test_bare_cr_and_quotes_are_counted(tmp_path):
    m = profile_bytes(tmp_path, b'A|B\n1|a\rb\n2|"quoted"\n')

    assert m["lines_with_bare_cr"] == 1
    assert m["lines_with_quotes"] == 1


def test_column_stats(tmp_path):
    m = profile_bytes(tmp_path, b"DATE_RECEIVED|NAME\n03/15/2024|  padded\n04/01/2024|\n|x\n")
    date, name = m["columns"]

    assert date["empty_pct"] == pytest.approx(33.33)
    assert date["distinct"] == 2
    assert date["top_shapes"] == {"99/99/9999": 2}
    assert name["padded"] == 1


def test_duplicate_column_names_are_reported(tmp_path):
    m = profile_bytes(tmp_path, b"A|B|A\n1|2|3\n")

    assert m["duplicate_columns"] == ["A"]


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("03/15/2024", "99/99/9999"),
        ("20240315", "99999999"),
        ("Y", "A"),
        ("ACME CORP.", "A A."),
        ("x" * 31, "<long text>"),
    ],
)
def test_value_shape(value, shape):
    assert value_shape(value) == shape


def test_max_records_stops_early(tmp_path):
    content = b"A|B\n" + b"".join(f"{i}|x\n".encode() for i in range(100))
    m = profile_bytes(tmp_path, content, max_records=10)

    assert m["truncated"] is True
    assert m["records"]["total"] == 10


def test_samples_are_off_by_default(tmp_path):
    content = b"A|B|C\n1|hello\nworld|z\n"
    without = profile_bytes(tmp_path, content)
    with_samples = profile_bytes(tmp_path, content, include_samples=True)

    assert "samples" not in without
    assert "samples" not in without["columns"][0]
    assert with_samples["samples"]["rejoined"] == [{"line": 2, "text": "1|hello\nworld|z"}]
    assert with_samples["columns"][1]["samples"] == ["hello\nworld"]


def test_empty_member(tmp_path):
    m = profile_bytes(tmp_path, b"")

    assert m["header"] == []
    assert m["records"]["total"] == 0


def test_compare_headers_reports_added_removed_reordered(tmp_path):
    base = make_zip(tmp_path / "base.zip", {"a.txt": b"A|B|C\n"})
    changed = make_zip(tmp_path / "changed.zip", {"b.txt": b"A|C|D\n"})
    reordered = make_zip(tmp_path / "reordered.zip", {"c.txt": b"C|B|A\n"})

    differences = compare_headers([profile_zip(p) for p in (base, changed, reordered)])

    assert differences == [
        {"file": "changed.zip/b.txt", "added": ["D"], "removed": ["B"], "reordered": False},
        {"file": "reordered.zip/c.txt", "added": [], "removed": [], "reordered": True},
    ]


def test_render_markdown(tmp_path):
    path = make_zip(tmp_path / "source.zip", {"source.txt": b"A|B\n1|x\n"})
    report = render_markdown([profile_zip(path)])

    assert "## source.zip / source.txt" in report
    assert "All headers match the first file." in report


def test_profile_command_writes_reports(tmp_path, capsys):
    source = make_zip(tmp_path / "foidevadd.zip", {"foidevadd.txt": b"MDR_REPORT_KEY|TEXT\n1|hello\n"})
    out_dir = tmp_path / "profiles"

    exit_code = main(["profile", str(source), "--out-dir", str(out_dir)])

    assert exit_code == 0
    paths = json.loads(capsys.readouterr().out)
    profile = json.loads(Path(paths["json"][0]).read_text(encoding="utf-8"))
    assert profile["members"][0]["records"]["total"] == 1
    assert Path(paths["report"]).read_text(encoding="utf-8").startswith("# Source profile")
