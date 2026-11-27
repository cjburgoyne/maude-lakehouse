import io
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from maude_lakehouse import cli
from maude_lakehouse.ingestion.download import check_file, dataset_for, download_file

LAST_MODIFIED = "Wed, 18 Nov 2026 00:03:33 GMT"


def zip_bytes(content: bytes = b"MDR_REPORT_KEY|TEXT\n1|hello\n") -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("sample.txt", content)
    return buffer.getvalue()


class FakeFDA:
    """Serves files over httpx.MockTransport and records every request."""

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []

    def publish(self, name: str, content: bytes, last_modified: str = LAST_MODIFIED, etag: str = '"a1:0"') -> None:
        self.files[name] = {"content": content, "last_modified": last_modified, "etag": etag}

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((request.method, name))
        if name not in self.files:
            return httpx.Response(404)

        file = self.files[name]
        headers = {
            "Last-Modified": file["last_modified"],
            "ETag": file["etag"],
            "Content-Length": str(len(file["content"])),
        }
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, content=file["content"])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def gets(self) -> int:
        return sum(1 for method, _ in self.requests if method == "GET")


@pytest.fixture
def fda() -> FakeFDA:
    return FakeFDA()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def rows(project_root: Path, table: str) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(project_root / "metadata" / "ingestion.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


@pytest.mark.parametrize(
    ("filename", "dataset"),
    [
        ("mdrfoithru2025.zip", "mdrfoi"),
        ("mdrfoiadd.zip", "mdrfoi"),
        ("patientthru2025.zip", "patient"),
        ("patientchange.zip", "patient"),
        ("foidevthru1997.zip", "device"),
        ("device2025.zip", "device"),
        ("deviceadd.zip", "device"),
        ("foitext2024.zip", "foitext"),
        ("foidevproblem.zip", "device_problem"),
        ("foidevproblem_thru2025.zip", "device_problem"),
        ("patientproblemcode_thru2025.zip", "patient_problem"),
        ("patientproblemcodes2026.zip", "patient_problem_codes"),
        ("deviceproblemcodes2025.zip", "device_problem_codes"),
    ],
)
def test_dataset_for(filename, dataset):
    assert dataset_for(filename) == dataset


def test_unknown_family_raises():
    with pytest.raises(ValueError, match="Unknown MAUDE file family"):
        dataset_for("somethingelse.zip")


@pytest.mark.parametrize("bad_name", ["../escape.zip", "device2025", "Device2025.zip", "a/b.zip"])
def test_invalid_file_name_raises_before_any_request(fda, project_root, bad_name):
    with pytest.raises(ValueError, match="Invalid file name"):
        download_file(bad_name, project_root, client=fda.client())

    assert fda.requests == []


def test_first_download_lands_file(fda, project_root):
    fda.publish("deviceadd.zip", zip_bytes())

    result = download_file("deviceadd.zip", project_root, client=fda.client())

    assert result["status"] == "downloaded"
    assert result["dataset"] == "device"
    assert result["new_version"] is True
    assert (project_root / result["object_path"]).is_file()
    assert result["object_path"].startswith("data/raw/device/")  # forward slashes on every OS
    assert fda.requests == [("HEAD", "deviceadd.zip"), ("GET", "deviceadd.zip")]

    (check,) = rows(project_root, "source_file_checks")
    assert check["action"] == "downloaded"
    assert check["last_modified"] == LAST_MODIFIED
    assert check["etag"] == '"a1:0"'
    assert check["version_id"] == result["version_id"]

    (version,) = rows(project_root, "source_file_versions")
    assert version["source_filename"] == "deviceadd.zip"
    assert version["source_url"] == "https://www.accessdata.fda.gov/MAUDE/ftparea/deviceadd.zip"


def test_unchanged_file_is_not_downloaded_again(fda, project_root):
    fda.publish("deviceadd.zip", zip_bytes())
    client = fda.client()
    first = download_file("deviceadd.zip", project_root, client=client)

    second = download_file("deviceadd.zip", project_root, client=client)

    assert second["status"] == "unchanged"
    assert second["version_id"] == first["version_id"]
    assert fda.gets() == 1
    assert [row["action"] for row in rows(project_root, "source_file_checks")] == ["downloaded", "unchanged"]
    assert len(rows(project_root, "source_file_observations")) == 1


def test_new_timestamp_with_same_content_downloads_but_adds_no_version(fda, project_root):
    content = zip_bytes()
    fda.publish("deviceadd.zip", content)
    client = fda.client()
    first = download_file("deviceadd.zip", project_root, client=client)

    fda.publish("deviceadd.zip", content, last_modified="Wed, 16 Dec 2026 00:00:00 GMT")
    second = download_file("deviceadd.zip", project_root, client=client)

    assert second["status"] == "downloaded"
    assert second["new_version"] is False
    assert second["version_id"] == first["version_id"]
    assert len(rows(project_root, "source_file_versions")) == 1
    assert len(rows(project_root, "source_file_observations")) == 2


def test_changed_content_creates_new_version(fda, project_root):
    fda.publish("deviceadd.zip", zip_bytes(b"A\n1\n"))
    client = fda.client()
    download_file("deviceadd.zip", project_root, client=client)

    fda.publish("deviceadd.zip", zip_bytes(b"A\n2\n"), last_modified="Wed, 16 Dec 2026 00:00:00 GMT", etag='"b2:0"')
    second = download_file("deviceadd.zip", project_root, client=client)

    assert second["new_version"] is True
    assert len(rows(project_root, "source_file_versions")) == 2


def test_changed_etag_alone_triggers_download(fda, project_root):
    content = zip_bytes()
    fda.publish("deviceadd.zip", content)
    client = fda.client()
    download_file("deviceadd.zip", project_root, client=client)

    fda.publish("deviceadd.zip", content, etag='"c3:0"')
    second = download_file("deviceadd.zip", project_root, client=client)

    assert second["status"] == "downloaded"


def test_force_downloads_unchanged_file(fda, project_root):
    fda.publish("deviceadd.zip", zip_bytes())
    client = fda.client()
    download_file("deviceadd.zip", project_root, client=client)

    second = download_file("deviceadd.zip", project_root, client=client, force=True)

    assert second["status"] == "downloaded"
    assert fda.gets() == 2


def test_missing_last_modified_always_downloads(project_root):
    content = zip_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Length": str(len(content))}
        return httpx.Response(200, headers=headers, content=b"" if request.method == "HEAD" else content)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    download_file("deviceadd.zip", project_root, client=client)
    second = download_file("deviceadd.zip", project_root, client=client)

    assert second["status"] == "downloaded"
    assert second["new_version"] is False


def test_incomplete_download_raises_and_lands_nothing(project_root):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Last-Modified": LAST_MODIFIED, "Content-Length": "999"}
        return httpx.Response(200, headers=headers, content=b"" if request.method == "HEAD" else b"truncated")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="Incomplete download"):
        download_file("deviceadd.zip", project_root, client=client)

    assert not (project_root / "data" / "raw").exists()
    assert list((project_root / "data" / "downloads").iterdir()) == []


def test_missing_remote_file_raises(fda, project_root):
    with pytest.raises(httpx.HTTPStatusError):
        download_file("deviceadd.zip", project_root, client=fda.client())


def test_check_file_reports_status_without_changing_anything(fda, project_root):
    fda.publish("deviceadd.zip", zip_bytes())
    client = fda.client()

    assert check_file("deviceadd.zip", project_root, client=client)["status"] == "new"
    assert not (project_root / "metadata").exists()

    download_file("deviceadd.zip", project_root, client=client)
    assert check_file("deviceadd.zip", project_root, client=client)["status"] == "unchanged"

    fda.publish("deviceadd.zip", zip_bytes(), last_modified="Wed, 16 Dec 2026 00:00:00 GMT")
    assert check_file("deviceadd.zip", project_root, client=client)["status"] == "changed"

    assert fda.gets() == 1
    assert len(rows(project_root, "source_file_checks")) == 1


def test_download_command_continues_after_an_error(fda, project_root, monkeypatch, capsys):
    fda.publish("deviceadd.zip", zip_bytes())
    monkeypatch.setattr(cli, "make_client", fda.client)

    exit_code = cli.main(["download", "missing.zip", "deviceadd.zip", "--project-root", str(project_root)])

    results = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert [r["status"] for r in results] == ["error", "downloaded"]


def test_download_command_check_only(fda, project_root, monkeypatch, capsys):
    fda.publish("deviceadd.zip", zip_bytes())
    monkeypatch.setattr(cli, "make_client", fda.client)

    exit_code = cli.main(["download", "deviceadd.zip", "--check-only", "--project-root", str(project_root)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "new"
    assert fda.gets() == 0


def test_download_progress_bar_goes_to_stderr(fda, project_root, capsys, monkeypatch):
    fda.publish("deviceadd.zip", zip_bytes())
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    with fda.client() as client:
        result = download_file("deviceadd.zip", project_root, client=client, progress=True)

    captured = capsys.readouterr()
    assert result["status"] == "downloaded"
    assert "deviceadd.zip" in captured.err
    assert captured.out == ""
