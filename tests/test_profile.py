from pathlib import Path
from zipfile import ZipFile

from maude_lakehouse.profiling.source_profile import profile_zip


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
    assert m["reecords"]["rejoined"] == 1
    assert m["rejoined_spans"] == {"2": 1}
    assert m["columns"][1]["max_length"] == len("hellow\nworld")
    assert m["unaccounted_lines"] == 0
