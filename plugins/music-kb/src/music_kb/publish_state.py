"""Publisher-side state for SSH release fan-out.

The peer inventory describes desired destinations. This module stores observed
publish outcomes separately so a failed attempt never edits connection config.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ValidationError


STATE_VERSION = 1


def default_publish_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at": None,
        "last_publish": None,
        "peers": {},
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Publish state must be a JSON object")
    if value.get("version") != STATE_VERSION:
        raise ValidationError(
            f"Publish state version must be {STATE_VERSION}",
            details={"actual": value.get("version")},
        )
    peers = value.get("peers")
    if not isinstance(peers, dict):
        raise ValidationError("Publish state peers must be a JSON object")
    return value


def load_publish_state(path: str | Path) -> dict[str, Any]:
    """Load state, returning an empty versioned state when it does not exist."""

    state_path = Path(path).expanduser().resolve()
    if not state_path.exists():
        return default_publish_state()
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Publish state cannot be read: {state_path}") from exc
    return _validate_state(value)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_summary(stages: object) -> list[dict[str, Any]]:
    if not isinstance(stages, list):
        return []
    summary: list[dict[str, Any]] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        item: dict[str, Any] = {"name": str(stage.get("name", "unknown")), "ok": bool(stage.get("ok"))}
        for key in ("returncode", "error"):
            if key in stage:
                item[key] = stage[key]
        summary.append(item)
    return summary


def record_publish_result(
    path: str | Path,
    result: dict[str, Any],
    *,
    release_sha256: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Atomically record one non-dry-run fan-out result.

    The input is the machine-readable result returned by ``publish_snapshot``.
    Dry-runs are deliberately side-effect free and return the current state.
    """

    state_path = Path(path).expanduser().resolve()
    state = load_publish_state(state_path)
    if result.get("dry_run"):
        return state

    timestamp = occurred_at or _utc_now()
    release_name = result.get("release_name")
    if not isinstance(release_name, str) or not release_name:
        raise ValidationError("Publish result release_name must be a non-empty string")
    peers = result.get("peers")
    if not isinstance(peers, list):
        raise ValidationError("Publish result peers must be a list")

    state["updated_at"] = timestamp
    state["last_publish"] = {
        "release_name": release_name,
        "release_sha256": release_sha256,
        "occurred_at": timestamp,
        "succeeded_count": int(result.get("succeeded_count", 0)),
        "failed_count": int(result.get("failed_count", 0)),
    }

    peer_state = state["peers"]
    for peer in peers:
        if not isinstance(peer, dict):
            raise ValidationError("Publish result contains a non-object peer result")
        name = peer.get("name")
        status = peer.get("status")
        if not isinstance(name, str) or not name:
            raise ValidationError("Publish result peer name must be a non-empty string")
        if status not in {"succeeded", "failed", "planned"}:
            raise ValidationError(f"Unsupported publish result status for {name}: {status}")
        if status == "planned":
            continue

        attempt = {
            "release_name": release_name,
            "release_sha256": release_sha256,
            "occurred_at": timestamp,
            "status": status,
            "stages": _stage_summary(peer.get("stages")),
        }
        entry = peer_state.setdefault(name, {})
        if not isinstance(entry, dict):
            raise ValidationError(f"Publish state entry for {name} must be an object")
        entry["last_attempt"] = attempt
        if status == "succeeded":
            entry["last_success"] = attempt

    _write_state(state_path, state)
    return state
