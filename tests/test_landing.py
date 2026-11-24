"""Tests for maude_lakehouse.ingestion.landing.

Each test builds a small zip in pytest's tmp_path, so no FDA data or network access is needed.
"""

import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

import pytest

from maude_lakehouse.ingestion.landing import land_file

CONTENT = b"MDR_REPORT_KEY|TEXT\n1|hello world\n"


def make_zip(path: Path, member: str = "sample.txt", content: bytes = CONTENT) -> Path:
    """Write an uncompressed zip so tests can corrupt its bytes predictably."""

    with ZipFile(path, "w", compression=ZIP_STORED) as archive:
        archive.writestr(member, content)

    return path


def manifest_rows(
    project_root: Path, table: str = "source_file_versions"
) -> list[sqlite3.Row]:
    db_path = project_root / "metadata" / "ingestion.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def source_zip(tmp_path: Path) -> Path:
    return make_zip(tmp_path / "foidevadd.zip")


def test_lands_file_at_content_addressed_path(source_zip, project_root):
    result = land_file(source_zip, "device", project_root)

    expected_hash = sha256(source_zip.read_bytes()).hexdigest()
    landed = project_root / "data" / "raw" / "device" / f"sha256-{expected_hash}.zip"

    assert result["sha256"] == expected_hash
    assert result["already_exists"] == False
    assert landed.read_bytes() == source_zip.read_bytes()
    assert not landed.with_suffix(".zip.tmp").exists()


def test_records_file_in_manifest(source_zip, project_root):
    url = "https://example.test/foidevadd.zip"
    land_file(source_zip, "device", project_root, source_url=url)

    rows = manifest_rows(project_root)
    assert len(rows) == 1

    row = rows[0]
    assert row["dataset_name"] == "device"
    assert row["source_filename"] == "foidevadd.zip"
    assert row["source_url"] == url
    assert row["file_size_bytes"] == source_zip.stat().st_size
    assert row["processing_status"] == "pending"
    assert row["processed_at"] is None


def test_landing_same_file_twice_is_idempotent(source_zip, project_root):
    first = land_file(source_zip, "device", project_root)
    second = land_file(source_zip, "device", project_root)

    assert second["already_exists"] == True
    assert second["object_path"] == first["object_path"]
    assert len(manifest_rows(project_root)) == 1
    assert len(list((project_root / "data" / "raw" / "device").iterdir())) == 1


def test_repeat_landing_records_each_observation(source_zip, project_root):
    first = land_file(source_zip, "device", project_root)
    second = land_file(source_zip, "device", project_root)

    assert first["new_version"] == True
    assert second["new_version"] == False
    assert second["version_id"] == first["version_id"]

    observations = manifest_rows(project_root, "source_file_observations")
    assert len(observations) == 2
    assert {row["version_id"] for row in observations} == {first["version_id"]}


def test_same_content_under_new_name_is_one_version_two_observations(
    tmp_path, project_root
):
    january = make_zip(tmp_path / "foidevadd_jan.zip")
    february = tmp_path / "foidevadd_feb.zip"
    february.write_bytes(january.read_bytes())  # Identical content under a new name

    land_file(january, "device", project_root)
    land_file(february, "device", project_root)

    assert len(manifest_rows(project_root)) == 1
    names = [
        row["source_filename"]
        for row in manifest_rows(project_root, "source_file_observations")
    ]
    assert names == ["foidevadd_jan.zip", "foidevadd_feb.zip"]


@pytest.mark.parametrize("bad_name", ["../escape", "Device", "dev ice", ""])
def test_invalid_dataset_name_raises(source_zip, project_root, bad_name):
    with pytest.raises(ValueError, match="Invalid dataset name"):
        land_file(source_zip, bad_name, project_root)

    assert not (project_root / "data").exists()


def test_different_content_lands_separately(tmp_path, project_root):
    a = make_zip(tmp_path / "a.zip", content=b"one")
    b = make_zip(tmp_path / "b.zip", content=b"two")

    land_file(a, "device", project_root)
    land_file(b, "device", project_root)

    assert len(manifest_rows(project_root)) == 2


def test_missing_source_raises(tmp_path, project_root):
    with pytest.raises(FileNotFoundError):
        land_file(tmp_path / "missing.zip", "device", project_root)


def test_non_zip_source_raises(tmp_path, project_root):
    not_a_zip = tmp_path / "notes.zip"
    not_a_zip.write_text("not a zip file")

    with pytest.raises(BadZipFile):
        land_file(not_a_zip, "device", project_root)

    assert not (project_root / "data" / "raw").exists()


def test_corrupt_zip_member_raises(tmp_path, project_root):
    path = make_zip(tmp_path / "corrupt.zip")
    data = path.read_bytes()
    assert data.count(b"hello world") == 1
    path.write_bytes(
        data.replace(b"hello world", b"hellO world")
    )  # breaks the member's CRC

    with pytest.raises(ValueError, match="Corrupt file inside ZIP archive"):
        land_file(path, "device", project_root)


def test_tampered_existing_artifact_raises(source_zip, project_root):
    result = land_file(source_zip, "device", project_root)
    (project_root / result["object_path"]).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="failed checksum"):
        land_file(source_zip, "device", project_root)
