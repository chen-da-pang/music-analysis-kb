from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .errors import SnapshotVerificationError, ValidationError
from .repository import MusicKBRepository
from .schema import SCHEMA_VERSION, connect, ensure_initialized


MANIFEST_VERSION = 1
SAFE_RELEASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def default_release_name() -> str:
    year, week, _ = date.today().isocalendar()
    return f"music-kb-{year}w{week:02d}"


def validate_release_name(value: object) -> str:
    """Accept only names that are safe in both filesystem and rsync contexts."""

    if not isinstance(value, str):
        raise ValidationError("release name must be a string")
    release_name = value.strip()
    if not SAFE_RELEASE_NAME.fullmatch(release_name) or release_name in {".", ".."}:
        raise ValidationError(
            "release name must start with a letter or number and use only letters, numbers, '.', '_' or '-'"
        )
    return release_name


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def create_snapshot(
    master_database: str | Path,
    output_dir: str | Path,
    *,
    release_name: str | None = None,
) -> dict[str, Any]:
    """Copy a consistent master state into a client-safe immutable release."""

    source_path = Path(master_database).expanduser().resolve()
    release_name = validate_release_name(release_name or default_release_name())
    destination_root = Path(output_dir).expanduser().resolve()
    release_dir = destination_root / release_name
    if release_dir.exists():
        raise ValidationError(f"Release already exists: {release_dir}")

    with MusicKBRepository(source_path, read_only=True) as master:
        validation = master.validate()
        if not validation["valid"]:
            raise ValidationError("Master database failed validation", details=validation)

    destination_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{release_name}.", dir=destination_root))
    database_name = f"{release_name}.sqlite"
    snapshot_path = staging / database_name
    try:
        source = connect(source_path, read_only=True)
        target = sqlite3.connect(snapshot_path)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()

        # A client release is intentionally public-surface only: remove all
        # noncanonical audit revisions after the consistent backup completes.
        with MusicKBRepository(snapshot_path, allow_snapshot_write=True) as snapshot:
            with snapshot.connection:
                snapshot.connection.execute(
                    """
                    DELETE FROM analysis_revision
                    WHERE NOT EXISTS (
                        SELECT 1 FROM recording r
                        WHERE r.canonical_analysis_id = analysis_revision.id
                    )
                    """
                )
                snapshot.connection.execute(
                    "DELETE FROM recording WHERE canonical_analysis_id IS NULL"
                )
                snapshot.connection.execute(
                    """
                    DELETE FROM tag
                    WHERE NOT EXISTS (SELECT 1 FROM analysis_tag at WHERE at.tag_id = tag.id)
                      AND NOT EXISTS (SELECT 1 FROM recording_tag rt WHERE rt.tag_id = tag.id)
                    """
                )
                snapshot.connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('database_kind', 'snapshot')"
                )
                snapshot.connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('release_name', ?)", (release_name,)
                )
                snapshot.connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            snapshot.rebuild_all_search_projections()
            snapshot.connection.execute("VACUUM")
            validation = snapshot.validate()
            if not validation["valid"]:
                raise ValidationError("Generated snapshot failed validation", details=validation)
            snapshot_status = snapshot.status()

        database_sha256 = sha256_file(snapshot_path)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "release_name": release_name,
            "database": {
                "filename": database_name,
                "sha256": database_sha256,
                "bytes": snapshot_path.stat().st_size,
                "schema_version": SCHEMA_VERSION,
            },
            "counts": snapshot_status["counts"],
            "distribution": {
                "database_kind": "snapshot",
                "read_only": True,
                "canonical_only": True,
            },
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        os.chmod(snapshot_path, 0o444)
        os.chmod(manifest_path, 0o444)
        os.replace(staging, release_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "release_dir": str(release_dir),
        "database": str(release_dir / database_name),
        "manifest": str(release_dir / "manifest.json"),
        "release_name": release_name,
        "sha256": database_sha256,
    }


def verify_snapshot(manifest_path: str | Path) -> dict[str, Any]:
    manifest_file = Path(manifest_path).expanduser().resolve()
    if not manifest_file.is_file():
        raise SnapshotVerificationError(f"Manifest does not exist: {manifest_file}")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotVerificationError(f"Manifest is not valid JSON: {exc.msg}") from exc
    try:
        if manifest["manifest_version"] != MANIFEST_VERSION:
            raise SnapshotVerificationError("Unsupported manifest version")
        raw_release_name = manifest["release_name"]
        database = manifest["database"]
        filename = str(database["filename"])
        expected_hash = str(database["sha256"])
    except (KeyError, TypeError) as exc:
        raise SnapshotVerificationError("Manifest is missing required fields") from exc
    try:
        release_name = validate_release_name(raw_release_name)
    except ValidationError as exc:
        raise SnapshotVerificationError("Manifest release name is unsafe") from exc
    if Path(filename).name != filename or not filename.endswith(".sqlite"):
        raise SnapshotVerificationError("Manifest database filename is unsafe")
    database_path = manifest_file.parent / filename
    if not database_path.is_file():
        raise SnapshotVerificationError(f"Snapshot database does not exist: {database_path}")
    actual_hash = sha256_file(database_path)
    if actual_hash != expected_hash:
        raise SnapshotVerificationError(
            "Snapshot SHA-256 mismatch", details={"expected": expected_hash, "actual": actual_hash}
        )
    try:
        with MusicKBRepository(database_path, read_only=True) as snapshot:
            status = snapshot.status()
            validation = snapshot.validate()
    except Exception as exc:
        raise SnapshotVerificationError(f"Snapshot database cannot be opened safely: {exc}") from exc
    if status["metadata"].get("database_kind") != "snapshot":
        raise SnapshotVerificationError("Database is not marked as a distributable snapshot")
    if status["metadata"].get("release_name") != release_name:
        raise SnapshotVerificationError("Database release name does not match manifest")
    if not validation["valid"]:
        raise SnapshotVerificationError("Snapshot invariants failed", details=validation)
    return {
        "valid": True,
        "manifest": str(manifest_file),
        "database": str(database_path),
        "release_name": release_name,
        "sha256": actual_hash,
        "status": status,
    }


def install_snapshot(release_dir: str | Path, target_dir: str | Path) -> dict[str, Any]:
    """Verify first, then atomically switch a local client's current.sqlite."""

    source_dir = Path(release_dir).expanduser().resolve()
    verified = verify_snapshot(source_dir / "manifest.json")
    source_database = Path(verified["database"])
    source_manifest = source_dir / "manifest.json"
    target = Path(target_dir).expanduser().resolve()
    releases_dir = target / "releases"
    incoming_dir = target / "incoming"
    releases_dir.mkdir(parents=True, exist_ok=True)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    release_name = str(verified["release_name"])
    current_link = target / "current.sqlite"
    if current_link.is_symlink():
        previous_current = os.readlink(current_link)
    elif current_link.exists():
        previous_current = str(current_link)
    else:
        previous_current = None
    destination_database = releases_dir / f"{release_name}.sqlite"
    destination_manifest = releases_dir / f"{release_name}.manifest.json"
    temporary_database = incoming_dir / f"{release_name}.sqlite.partial"
    temporary_manifest = incoming_dir / f"{release_name}.manifest.json.partial"

    shutil.copy2(source_database, temporary_database)
    if sha256_file(temporary_database) != str(verified["sha256"]):
        temporary_database.unlink(missing_ok=True)
        raise SnapshotVerificationError("Copied snapshot hash mismatch before install")
    shutil.copy2(source_manifest, temporary_manifest)
    os.chmod(temporary_database, 0o444)
    os.chmod(temporary_manifest, 0o444)
    os.replace(temporary_database, destination_database)
    os.replace(temporary_manifest, destination_manifest)

    temporary_link = target / ".current.sqlite.next"
    temporary_link.unlink(missing_ok=True)
    relative_target = os.path.relpath(destination_database, target)
    os.symlink(relative_target, temporary_link)
    os.replace(temporary_link, target / "current.sqlite")
    return {
        "installed": True,
        "release_name": release_name,
        "target_dir": str(target),
        "previous_current": previous_current,
        "current_target": os.readlink(target / "current.sqlite"),
        "current_database": str(target / "current.sqlite"),
        "database": str(destination_database),
        "manifest": str(destination_manifest),
    }
