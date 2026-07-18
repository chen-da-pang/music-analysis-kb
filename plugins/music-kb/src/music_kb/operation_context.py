"""Shared run/atom audit context for the publisher workflow.

The context is deliberately small and file based. It gives every atom a
stable run id, a versioned copy of the validated operating decisions, an
exclusive publisher lock, and one JSON receipt per atom.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


OPERATIONS_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_validated_operations(path: str | Path, *, required_atom: str | None = None) -> dict[str, Any]:
    """Load the versioned record of operations validated from prior conversations."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"validated operations file does not exist: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"validated operations file is unreadable: {source}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != OPERATIONS_SCHEMA_VERSION:
        raise ValueError(
            f"validated operations schema must be {OPERATIONS_SCHEMA_VERSION}: {source}"
        )
    operations = value.get("operations")
    if not isinstance(operations, dict) or not operations:
        raise ValueError("validated operations must contain a non-empty operations object")
    if required_atom is not None:
        operation = operations.get(required_atom)
        if not isinstance(operation, dict) or not operation.get("effective_method"):
            raise ValueError(f"validated operation is missing for atom: {required_atom}")
    return value


class RunContext:
    """Own the publisher lock, aggregate state, and per-atom JSON receipts."""

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: str | Path,
        operations_file: str | Path,
    ) -> None:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        if not run_id or any(character not in allowed for character in run_id):
            raise ValueError("run_id contains unsafe characters")
        self.run_id = run_id
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.operations_file = Path(operations_file).expanduser().resolve()
        self.lock_path = self.run_dir.parent / ".weekly-update.lock"
        self.state_path = self.run_dir / "run-state.json"
        self.atoms_dir = self.run_dir / "atoms"
        self._lock_handle: Any = None
        self.operations: dict[str, Any] = {}
        self.state: dict[str, Any] = {}

    def __enter__(self) -> "RunContext":
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.atoms_dir.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise RuntimeError(f"another weekly run holds the publisher lock: {self.lock_path}") from exc

        self.operations = load_validated_operations(self.operations_file)
        self.state = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": "running",
            "started_at": now_iso(),
            "finished_at": None,
            "operations_file": str(self.operations_file),
            "operations_sha256": sha256_file(self.operations_file),
            "atoms": {},
            "errors": [],
        }
        self._write_state()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if exc_value is not None:
            self.state["status"] = "failed"
            self.state["errors"].append(
                {"type": exc_type.__name__ if exc_type else "Error", "message": str(exc_value)}
            )
        elif self.state.get("status") == "running":
            self.state["status"] = "succeeded"
        self.state["finished_at"] = now_iso()
        self._write_state()
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def require_operation(self, atom: str) -> dict[str, Any]:
        current_hash = sha256_file(self.operations_file)
        if current_hash != self.state.get("operations_sha256"):
            raise ValueError(
                "validated operations changed during the run; refusing to continue: "
                f"{self.operations_file}"
            )
        self.operations = load_validated_operations(self.operations_file, required_atom=atom)
        operations = self.operations.get("operations", {})
        operation = operations.get(atom)
        if not isinstance(operation, dict) or not operation.get("effective_method"):
            raise ValueError(f"validated operation is missing for atom: {atom}")
        return operation

    def start_atom(self, atom: str, *, inputs: Mapping[str, Any], command: list[str] | None = None) -> None:
        entry = {
            "status": "running",
            "started_at": now_iso(),
            "inputs": dict(inputs),
            "command": list(command or []),
        }
        self.state["atoms"][atom] = entry
        self._write_atom(atom, entry)
        self._write_state()

    def finish_atom(self, atom: str, *, outputs: Mapping[str, Any], artifacts: list[str] | None = None) -> dict[str, Any]:
        entry = dict(self.state["atoms"].get(atom, {}))
        entry.update(
            {
                "status": "succeeded",
                "finished_at": now_iso(),
                "outputs": dict(outputs),
                "artifacts": list(artifacts or []),
            }
        )
        self.state["atoms"][atom] = entry
        self._write_atom(atom, entry)
        self._write_state()
        return entry

    def fail_atom(self, atom: str, *, error: str, outputs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        entry = dict(self.state["atoms"].get(atom, {}))
        entry.update(
            {
                "status": "failed",
                "finished_at": now_iso(),
                "error": error,
                "outputs": dict(outputs or {}),
            }
        )
        self.state["atoms"][atom] = entry
        self.state["errors"].append({"atom": atom, "message": error})
        self._write_atom(atom, entry)
        self._write_state()
        return entry

    def _write_atom(self, atom: str, value: Mapping[str, Any]) -> None:
        atomic_write_json(self.atoms_dir / f"{atom}.json", value)

    def _write_state(self) -> None:
        atomic_write_json(self.state_path, self.state)


@contextmanager
def atom(
    context: RunContext,
    name: str,
    *,
    inputs: Mapping[str, Any],
    command: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Record a bounded atom and persist its result even when it fails."""

    context.require_operation(name)
    context.start_atom(name, inputs=inputs, command=command)
    outputs: dict[str, Any] = {}
    try:
        yield outputs
    except Exception as exc:
        context.fail_atom(name, error=str(exc), outputs=outputs)
        raise
    else:
        context.finish_atom(name, outputs=outputs)
