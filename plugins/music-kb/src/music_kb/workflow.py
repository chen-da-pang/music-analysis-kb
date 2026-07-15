"""Publisher workflows composed from the tested Music KB primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .campaign_delivery import load_campaign_delivery_file
from .distribution import CommandRunner, publish_snapshot
from .publish_state import record_publish_result
from .repository import MusicKBRepository, iter_import_file
from .snapshot import create_snapshot, verify_snapshot


def _with_repository(path: Path, operation: Any) -> dict[str, Any]:
    with MusicKBRepository(path, read_only=False) as repository:
        return operation(repository)


def run_weekly_update(
    *,
    database: str | Path,
    input_path: str | Path,
    input_kind: str,
    expected_count: int | None,
    batch_size: int,
    output_dir: str | Path,
    release_name: str | None,
    peers_file: str | Path,
    peer_names: Sequence[str] = (),
    publish: bool = False,
    state_file: str | Path,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Run one publisher update and optionally distribute its release.

    A verified release is always created before any SSH transport. Without
    ``publish=True`` the transport is a dry-run only.
    """

    db = Path(database).expanduser().resolve()
    source = Path(input_path).expanduser().resolve()
    if input_kind == "campaign":
        entries = load_campaign_delivery_file(source, expected_count=expected_count)
        import_result = _with_repository(db, lambda repo: repo.import_campaign_delivery(entries))
    elif input_kind == "generic":
        import_result = _with_repository(
            db,
            lambda repo: repo.import_analyses(iter_import_file(source), batch_size=batch_size),
        )
    else:
        raise ValueError(f"Unsupported weekly update input kind: {input_kind}")

    if input_kind == "campaign":
        tag_result = _with_repository(
            db,
            lambda repo: repo.enrich_campaign_tags(dry_run=False, batch_size=batch_size),
        )
    else:
        tag_result = {"skipped": True, "reason": "generic input must carry its own retrieval tags"}

    validation = _with_repository(db, lambda repo: repo.validate())
    if not validation["valid"]:
        raise ValueError("Master database failed validation")

    release = create_snapshot(db, output_dir, release_name=release_name)
    verified = verify_snapshot(Path(release["manifest"]))
    publish_result = publish_snapshot(
        release["release_dir"],
        peers_file,
        peer_names=peer_names,
        dry_run=not publish,
        runner=runner if runner is not None else _default_runner_for_workflow,
    )
    if publish:
        record_publish_result(
            state_file,
            publish_result,
            release_sha256=str(verified["sha256"]),
        )

    return {
        "workflow": "weekly-update",
        "input_kind": input_kind,
        "import": import_result,
        "tags": tag_result,
        "validation": validation,
        "release": release,
        "release_verification": {
            "valid": verified["valid"],
            "release_name": verified["release_name"],
            "sha256": verified["sha256"],
        },
        "publish": publish_result,
        "state_file": str(Path(state_file).expanduser().resolve()) if publish else None,
    }


def _default_runner_for_workflow(command: Sequence[str], timeout_seconds: int):
    import subprocess

    return subprocess.run(
        list(command),
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
