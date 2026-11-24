
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
from zipfile import ZipFile


def calculate_sha256(path: Path) -> str:
    """Calculate the SHA-256 checksum of a file."""
    digest = sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def validate_zip(path: Path) -> None:
    """Raise an exception if the ZIP archive is corrupt."""
    with ZipFile(path) as archive:
        bad_file = archive.testzip()

        if bad_file is not None:
            raise ValueError(
                f"Corrupt file inside ZIP archive: {bad_file}"
            )


def initialize_manifest(db_path: Path) -> None:
    """Create the ingestion metadata database and tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
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
            )
        """)


def land_file(
    source_path: Path,
    dataset_name: str,
    project_root: Path,
    source_url: str | None = None,
) -> dict:
    """
    Validate and land a source ZIP file.

    Stores the original artifact in the raw zone and
    registers its metadata in SQLite.
    """
    source_path = source_path.resolve()
    project_root = project_root.resolve()

    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    # Validate the original artifact.
    validate_zip(source_path)

    # Calculate its content identity.
    checksum = calculate_sha256(source_path)
    file_size = source_path.stat().st_size

    # Build a content-addressed destination.
    raw_dir = project_root / "data" / "raw" / dataset_name
    raw_dir.mkdir(parents=True, exist_ok=True)

    destination = raw_dir / f"sha256-{checksum}.zip"

    # Copy only if this exact artifact isn't already present.
    if destination.exists():
        if calculate_sha256(destination) != checksum:
            raise ValueError(
                f"Existing raw artifact failed checksum: {destination}"
            )
        already_exists = True

    else:
        temp_path = destination.with_suffix(".zip.tmp")

        try:
            shutil.copyfile(source_path, temp_path)

            if calculate_sha256(temp_path) != checksum:
                raise ValueError("Checksum mismatch during copy")

            temp_path.replace(destination)

        finally:
            temp_path.unlink(missing_ok=True)

        already_exists = False

    # Register the artifact.
    db_path = project_root / "metadata" / "ingestion.db"
    initialize_manifest(db_path)

    landed_at = datetime.now(timezone.utc).isoformat()

    relative_path = destination.relative_to(project_root)

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO source_file_versions (
                dataset_name,
                source_filename,
                source_url,
                object_path,
                sha256,
                file_size_bytes,
                landed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            dataset_name,
            source_path.name,
            source_url,
            str(relative_path),
            checksum,
            file_size,
            landed_at,
        ))

    return {
        "dataset": dataset_name,
        "filename": source_path.name,
        "sha256": checksum,
        "size_bytes": file_size,
        "object_path": str(relative_path),
        "already_exists": already_exists,
    }
