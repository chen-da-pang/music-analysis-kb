"""Read-only checks performed before a weekly publisher run."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterable

from .operation_context import load_validated_operations


def _check(name: str, ok: bool, *, detail: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": required, "detail": detail}


def run_preflight(
    *,
    workspace: str | Path,
    operations_file: str | Path,
    database: str | Path,
    inventory: str | Path,
    audio_root: str | Path,
    peers_file: str | Path | None,
    publish: bool,
    required_commands: Iterable[str] = ("kugou-cli", "claude", "rsync"),
    minimum_free_bytes: int = 1,
) -> dict[str, Any]:
    """Return a machine-readable preflight report without mutating state."""

    workspace_path = Path(workspace).expanduser().resolve()
    database_path = Path(database).expanduser().resolve()
    inventory_path = Path(inventory).expanduser().resolve()
    audio_path = Path(audio_root).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    try:
        load_validated_operations(operations_file, required_atom="preflight")
        checks.append(_check("validated_operations", True, detail=str(Path(operations_file).expanduser().resolve())))
    except ValueError as exc:
        checks.append(_check("validated_operations", False, detail=str(exc)))

    checks.append(_check("workspace", workspace_path.is_dir(), detail=str(workspace_path)))
    checks.append(_check("database", database_path.is_file(), detail=str(database_path)))
    checks.append(_check("inventory", inventory_path.is_file(), detail=str(inventory_path)))
    checks.append(
        _check(
            "inventory_parent_writable",
            inventory_path.parent.is_dir() and os.access(inventory_path.parent, os.W_OK),
            detail=str(inventory_path.parent),
        )
    )
    checks.append(
        _check(
            "audio_parent_writable",
            audio_path.parent.is_dir() and os.access(audio_path.parent, os.W_OK),
            detail=str(audio_path.parent),
        )
    )
    try:
        free_bytes = shutil.disk_usage(audio_path.parent).free
        checks.append(
            _check(
                "disk_free",
                free_bytes >= minimum_free_bytes,
                detail=f"{free_bytes} bytes free; minimum {minimum_free_bytes}",
            )
        )
    except OSError as exc:
        checks.append(_check("disk_free", False, detail=str(exc)))

    for command in required_commands:
        path = shutil.which(command)
        checks.append(_check(f"command:{command}", path is not None, detail=path or "not found"))

    if peers_file is None:
        checks.append(_check("peers_file", not publish, detail="not required for a non-publishing run"))
    else:
        peer_path = Path(peers_file).expanduser().resolve()
        checks.append(_check("peers_file", peer_path.is_file(), detail=str(peer_path), required=publish))

    failed_required = [item for item in checks if item["required"] and not item["ok"]]
    return {"valid": not failed_required, "checks": checks, "failed_required": failed_required}
