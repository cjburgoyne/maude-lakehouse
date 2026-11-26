"""Download MAUDE files from the FDA, skipping files that haven't changed (ADR 0004).

Each file is checked with a HEAD request first. If the server reports the same Last-Modified, Content-Length, and
ETag as the last download of that URL, nothing is downloaded. Otherwise the file is streamed to a temporary file,
its size is checked, and it is landed: validated, hashed, stored by SHA-256, and recorded in the manifest.
"""

import datetime
import re
import tempfile
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import httpx

from maude_lakehouse.ingestion import manifest
from maude_lakehouse.ingestion.landing import land_file

BASE_URL = "https://www.accessdata.fda.gov/MAUDE/ftparea/"
CHUNK_SIZE = 1024 * 1024
FILENAME = re.compile(r"^[a-z0-9_]+\.zip$")

# File name prefix -> dataset. More specific prefixes come first
DATASETS = [
    (re.compile(r"^patientproblemcodes\d"), "patient_problem_codes"),
    (re.compile(r"^patientproblemcode"), "patient_problem"),
    (re.compile(r"^deviceproblemcodes"), "device_problem_codes"),
    (re.compile(r"^foidevproblem"), "device_problem"),
    (re.compile(r"^mdrfoi"), "mdrfoi"),
    (re.compile(r"^patient"), "patient"),
    (re.compile(r"^foitext"), "foitext"),
    (re.compile(r"^(foidev|device)"), "device"),
]


@dataclass
class RemoteFile:
    """What the server days about a file."""

    url: str
    etag: str | None
    last_modified: str | None
    content_length: int | None

    @classmethod
    def from_response(cls, response: httpx.Response) -> "RemoteFile":
        length = response.headers.get("content-length")
        return cls(
            url=str(response.url),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            content_length=int(length) if length is not None else None,
        )

    def matches(self, previous: dict) -> bool:
        """True if the server describes the same file as the previous download.

        Last-Modified and Content-Length must be present and equal. ETag is compared only when both sides have one.
        When the server doesn't send enough to tell, the answer is False and the file is downloaded; landing then
        recognizes indentical content by its SHA-256.
        """
        if self.last_modified is None or self.content_length is None:
            return False
        same_etag = self.etag is None or previous["etag"] is None or self.etag == previous["etag"]
        return (
            self.last_modified == previous["last_modified"]
            and self.content_length == previous["content_length"]
            and same_etag
        )


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": f"maude-lakehouse/{version('maude-lakehouse')}",
            "Accept-Encoding": "identity",  # byte counts must match Content-Length
        },
        timeout=httpx.Timeout(30.0, read=300.0),
        follow_redirects=True,
        transport=httpx.HTTPTransport(retries=3),
    )


def dataset_for(filename: str) -> str:
    """Map an FDA file name to its dataset, e.g. device2025.zip -> device."""
    for pattern, dataset in DATASETS:
        if pattern.match(filename):
            return dataset
    raise ValueError(f"Unknown MAUDE file family: {filename!r} (pass a dataset explicitly)")


def check_file(filename: str, project_root: Path, *, client: httpx.Client) -> dict:
    """Report whether a file is new, changed, or unchanged since its last download. Changes nothing."""
    url = _url(filename)
    remote = _head(client, url)
    previous = manifest.last_download(_db_path(project_root), url)

    if previous is None:
        status = "new"
    elif remote.matches(previous):
        status = "unchanged"
    else:
        status = "changed"

    return {"filename": filename, "url": url, "status": status, **_headers(remote)}


def download_file(
    filename: str,
    project_root: Path,
    *,
    client: httpx.Client,
    dataset: str | None = None,
    force: bool = False,
) -> dict:
    """Download and land a file unless the server reports it unchanged since its last download."""
    url = _url(filename)
    dataset = dataset or dataset_for(filename)
    project_root = project_root.resolve()
    db_path = _db_path(project_root)

    remote = _head(client, url)
    previous = manifest.last_download(db_path, url)

    if previous is not None and not force and remote.matches(previous):
        manifest.record_check(
            db_path,
            source_url=url,
            checked_at=_now(),
            action="unchanged",
            version_id=previous["version_id"],
            **_headers(remote),
        )
        return {"filename": filename, "url": url, "status": "unchanged", "version_id": previous["version_id"]}

    downloads_dir = project_root / "data" / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=downloads_dir) as temp_dir:
        path = Path(temp_dir) / filename  # keep the FDA name; landing records it
        downloaded = _stream(client, url, path)
        landed = land_file(path, dataset, project_root, source_url=url)

    manifest.record_check(
        db_path,
        source_url=url,
        checked_at=_now(),
        action="downloaded",
        version_id=landed["version_id"],
        **_headers(downloaded),
    )
    return {
        "filename": filename,
        "url": url,
        "status": "downloaded",
        "dataset": dataset,
        "version_id": landed["version_id"],
        "new_version": landed["new_version"],
        "sha256": landed["sha256"],
        "size_bytes": landed["size_bytes"],
        "object_path": landed["object_path"],
    }


def _url(filename: str) -> str:
    if not FILENAME.fullmatch(filename):
        raise ValueError(f"Invalid file name (expected something like device2025.zip): {filename}")
    return BASE_URL + filename


def _db_path(project_root: Path) -> Path:
    return project_root / "metadata" / "ingestion.db"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _headers(remote: RemoteFile) -> dict:
    return {"etag": remote.etag, "last_modified": remote.last_modified, "content_length": remote.content_length}


def _head(client: httpx.Client, url: str) -> RemoteFile:
    response = client.head(url)
    response.raise_for_status()
    return RemoteFile.from_response(response)


def _stream(client: httpx.Client, url: str, path: Path) -> RemoteFile:
    """Stream a file to disk and check that the full body arrived. Returns the GET response's headers."""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        remote = RemoteFile.from_response(response)
        size = 0
        with path.open("wb") as out:
            for chunk in response.iter_bytes(CHUNK_SIZE):
                out.write(chunk)
                size += len(chunk)

    if remote.content_length is not None and size != remote.content_length:
        raise ValueError(f"Incomplete download: got {size:,} of {remote.content_length:,} bytes from {url}")
    return remote
