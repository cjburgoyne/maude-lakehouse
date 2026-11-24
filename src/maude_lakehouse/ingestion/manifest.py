"""SQLite manifest of landed source files.

Two Tables:
 - source_file_versions: one row per unique file content (dataset + SHA-256). Points at the stored artifact.
 - source_file_observations: one row each time a file is landed, including repeats of content already stored.
   This records that the FDA published a given file on a given date, even when its content hadn't changed.
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
