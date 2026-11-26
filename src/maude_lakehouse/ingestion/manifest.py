"""SQLite manifest of landed source files.

Three Tables:
 - source_file_versions: one row per unique file content (dataset + SHA-256). Points at the stored artifact.
 - source_file_observations: one row each time a file is landed, including repeats of content already stored.
   This records that the FDA published a given file on a given date, even when its content hadn't changed.
 - source_file_checks: one row each time the downloader checks a URL, with the server's ETag, Last-Modified,
   and Content-Length, and whether the file was downloaded or skipped as unchanged (ADR 0004).
"""

import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_file_versions (
    version_id INTEGER PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_url TEXT,
    object_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    landed_at TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'pending',
    processed_at TEXT,
    UNIQUE(dataset_name, sha256)
);

CREATE TABLE IF NOT EXISTS source_file_observations (
    observation_id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES source_file_versions(version_id),
    source_filename TEXT NOT NULL,
    source_url TEXT,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_file_checks (
    check_id INTEGER PRIMARY KEY,
    source_url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    content_length INTEGER,
    action TEXT NOT NULL CHECK (action IN ('downloaded', 'unchanged')),
    version_id INTEGER REFERENCES source_file_versions(version_id)
);

CREATE INDEX IF NOT EXISTS source_file_checks_by_url ON source_file_checks (source_url, check_id);
"""


@contextmanager
def connect(db_path: Path) -> Generator[sqlite3.Connection]:
    """Open the manifest, commit on success, roll back on error, and always close the connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:  # transaction: commit or roll back
            yield conn


def initialize(db_path: Path) -> None:
    """Create the manifest tables if they don't exist."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def register_landing(
    db_path: Path,
    *,
    dataset_name: str,
    source_filename: str,
    source_url: str | None,
    object_path: str,
    sha256: str,
    file_size_bytes: int,
    observed_at: str,
) -> dict:
    """Record a landed file in one transaction.

    Creates the version row if this content is new, and always adds an observation row.
    Returns the version_id and whether a new version was created.
    """

    initialize(db_path)

    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO source_file_versions (
                dataset_name, source_filename, source_url, object_path,
                sha256, file_size_bytes, landed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_name,
                source_filename,
                source_url,
                object_path,
                sha256,
                file_size_bytes,
                observed_at,
            ),
        )
        new_version = cursor.rowcount == 1

        (version_id,) = conn.execute(
            "SELECT version_id FROM source_file_versions WHERE dataset_name = ? AND sha256 = ?",
            (dataset_name, sha256),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO source_file_observations (version_id, source_filename, source_url, observed_at)
            VALUES (?, ?, ?, ?)
            """,
            (version_id, source_filename, source_url, observed_at),
        )

    return {"version_id": version_id, "new_version": new_version}


def record_check(
    db_path: Path,
    *,
    source_url: str,
    checked_at: str,
    etag: str | None,
    last_modified: str | None,
    content_length: int | None,
    action: str,
    version_id: int | None,
) -> None:
    """Record one downloader check of a URL."""
    initialize(db_path)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO source_file_checks (
                source_url, checked_at, etag, last_modified, content_length, action, version_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source_url, checked_at, etag, last_modified, content_length, action, version_id),
        )


def last_download(db_path: Path, source_url: str) -> dict | None:
    """Return the most recent check of this URL that resulted in a download, or None."""
    if not db_path.exists():
        return None

    initialize(db_path)

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT * FROM source_file_checks
            WHERE source_url = ? AND action = 'downloaded'
            ORDER BY check_id DESC
            LIMIT 1
            """,
            (source_url,),
        ).fetchone()

    return dict(row) if row else None
