import re
import shutil
import datetime
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

from maude_lakehouse.ingestion import manifest

# Dataset names become folder names, so keep them to simple identifiers.
DATASET_NAME = re.compile(r"^[a-z0-9_]+$")


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



def land_file(
    source_path: Path,
    dataset_name: str,
    project_root: Path,
    source_url: str | None = None,
) -> dict:
    """
    Validate and land a source ZIP file.

    Stores the original artifact in the raw zone under its SHA-256 and records it in the manifest.
    Landing the same content again stores nothing new bit still records an observation.
    """
    if not DATASET_NAME.fullmatch(dataset_name):
        raise ValueError(f"Invalid dataset name (use lowercase letters, digits, underscores): {dataset_name!r}")

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
    relative_path = destination.relative_to(project_root)

    registration = manifest.register_landing(
        project_root / "metadata" / "ingestion.db",
        dataset_name=dataset_name,
        source_filename=source_path.name,
        source_url=source_url,
        object_path=(relative_path),
        sha256=checksum,
        file_size_bytes=file_size,
        observed_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )

    return {
        "dataset": dataset_name,
        "filename": source_path.name,
        "sha256": checksum,
        "size_bytes": file_size,
        "object_path": str(relative_path),
        "already_exists": already_exists,
        "version_id": registration["version_id"],
        "new_version": registration["new_version"],
    }
