"""Publisher helpers for materializing the no-audio CC lyric backfill queue."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .repository import MusicKBRepository


def _atomic_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_lyric_backfill_queue(
    database: str | Path,
    output: str | Path,
    *,
    unresolved_only: bool = True,
    chart_database: str | Path | None = None,
) -> dict[str, Any]:
    """Write an immutable-input JSONL queue from the publisher master.

    The generated file is operational data and must remain outside the plugin
    repository.  It carries the canonical recording/source assertions so the
    worker receipt can be rejected if it drifts before import.
    """

    destination = Path(output).expanduser().resolve()
    with MusicKBRepository(database, read_only=True) as repository:
        plan = repository.prepare_lyric_backfill_queue(
            unresolved_only=unresolved_only,
            chart_database=chart_database,
        )
    rows = plan.pop("rows")
    assert isinstance(rows, list)
    _atomic_write_jsonl(destination, rows)
    resolved_chart_database = plan.get("chart_database")
    chart_sha256 = (
        _sha256_file(Path(str(resolved_chart_database)))
        if resolved_chart_database
        else None
    )
    return {
        **plan,
        "queue": str(destination),
        "queue_bytes": destination.stat().st_size,
        "queue_sha256": _sha256_file(destination),
        "chart_database_sha256": chart_sha256,
    }
