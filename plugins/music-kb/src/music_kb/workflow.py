"""Publisher workflows composed from the tested Music KB primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .campaign_delivery import load_campaign_delivery_file
from .distribution import CommandRunner, publish_snapshot
from .publish_state import record_publish_result
from .repository import MusicKBRepository, iter_import_file
from .snapshot import create_snapshot, install_snapshot, verify_snapshot


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
    local_snapshot_dir: str | Path | None = None,
    install_local: bool | None = None,
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
    local_target = (
        Path(local_snapshot_dir).expanduser().resolve()
        if local_snapshot_dir is not None
        else db.parent
    )
    install_local_enabled = publish if install_local is None else bool(install_local)
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

    source_link_status: dict[str, Any] | None = None
    if input_kind == "campaign":
        source_link_status = _with_repository(db, lambda repo: repo.status()["counts"])
        if (
            source_link_status["source_tracks"] <= 0
            or source_link_status["source_links"] != source_link_status["source_tracks"]
        ):
            raise ValueError(
                "Source-link completeness gate failed: "
                f"source_tracks={source_link_status['source_tracks']} "
                f"source_links={source_link_status['source_links']}"
            )

    release = create_snapshot(db, output_dir, release_name=release_name)
    verified = verify_snapshot(Path(release["manifest"]))
    if install_local_enabled:
        local_install = install_snapshot(release["release_dir"], local_target)
        local_install.update({"status": "succeeded"})
    else:
        local_install = {
            "status": "skipped",
            "reason": "publisher-local install disabled",
            "target_dir": str(local_target),
        }
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
        "source_link_status": source_link_status,
        "release": release,
        "release_verification": {
            "valid": verified["valid"],
            "release_name": verified["release_name"],
            "sha256": verified["sha256"],
        },
        "local_install": local_install,
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
