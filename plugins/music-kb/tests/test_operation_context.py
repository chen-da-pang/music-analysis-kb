from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_kb.operation_context import RunContext, atom, load_validated_operations


OPERATIONS = {
    "schema_version": 1,
    "operations": {"fixture": {"effective_method": "fixture method"}},
}


def _write_operations(path: Path) -> None:
    path.write_text(json.dumps(OPERATIONS) + "\n", encoding="utf-8")


def test_run_context_writes_success_receipt_and_operation_hash(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(operations)

    with RunContext(run_id="fixture-run", run_dir=tmp_path / "run", operations_file=operations) as context:
        with atom(context, "fixture", inputs={"count": 1}, command=["fixture"] ) as outputs:
            outputs["count"] = 1

    state = json.loads((tmp_path / "run" / "run-state.json").read_text(encoding="utf-8"))
    receipt = json.loads((tmp_path / "run" / "atoms" / "fixture.json").read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    assert state["operations_sha256"]
    assert receipt["status"] == "succeeded"
    assert receipt["outputs"] == {"count": 1}


def test_run_context_persists_failure_receipt(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(operations)

    with pytest.raises(RuntimeError, match="expected failure"):
        with RunContext(run_id="fixture-failure", run_dir=tmp_path / "run", operations_file=operations) as context:
            with atom(context, "fixture", inputs={}):
                raise RuntimeError("expected failure")

    state = json.loads((tmp_path / "run" / "run-state.json").read_text(encoding="utf-8"))
    receipt = json.loads((tmp_path / "run" / "atoms" / "fixture.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert receipt["status"] == "failed"
    assert receipt["error"] == "expected failure"


def test_operation_loader_rejects_missing_atom(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(operations)

    with pytest.raises(ValueError, match="missing for atom"):
        load_validated_operations(operations, required_atom="missing")


def test_run_context_rejects_operation_record_changes_between_atoms(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(operations)

    with RunContext(run_id="fixture-change", run_dir=tmp_path / "run", operations_file=operations) as context:
        operations.write_text(
            json.dumps({"schema_version": 1, "operations": {"fixture": {"effective_method": "changed"}}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="changed during the run"):
            with atom(context, "fixture", inputs={}):
                pass
