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
    assert receipt["operations_sha256"] == state["operations_sha256"]
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
    assert receipt["operations_sha256"] == state["operations_sha256"]


def test_run_context_resumes_failed_state_without_erasing_prior_atoms(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(operations)
    run_dir = tmp_path / "run"
    with pytest.raises(RuntimeError):
        with RunContext(run_id="fixture-resume", run_dir=run_dir, operations_file=operations) as context:
            with atom(context, "fixture", inputs={}) as outputs:
                outputs["attempt"] = 1
                raise RuntimeError("first attempt")

    with RunContext(run_id="fixture-resume", run_dir=run_dir, operations_file=operations) as context:
        assert context.resumed is True
        with atom(context, "fixture", inputs={}) as outputs:
            outputs["attempt"] = 2

    state = json.loads((run_dir / "run-state.json").read_text(encoding="utf-8"))
    receipt = json.loads((run_dir / "atoms" / "fixture.json").read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    assert state["resume_count"] == 1
    assert receipt["outputs"] == {"attempt": 2}
    assert receipt["operations_sha256"] == state["operations_sha256"]


def test_run_context_does_not_replay_succeeded_run(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(operations)
    run_dir = tmp_path / "run"
    with RunContext(run_id="fixture-complete", run_dir=run_dir, operations_file=operations):
        pass
    with pytest.raises(ValueError, match="already succeeded"):
        with RunContext(run_id="fixture-complete", run_dir=run_dir, operations_file=operations):
            pass


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
