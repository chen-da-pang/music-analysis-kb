from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_kb.errors import ValidationError
from music_kb.publish_state import load_publish_state, record_publish_result


def _result(*, dry_run: bool = False) -> dict[str, object]:
    return {
        "dry_run": dry_run,
        "release_name": "music-kb-2026w30",
        "succeeded_count": 1,
        "failed_count": 1,
        "peers": [
            {
                "name": "online",
                "status": "succeeded",
                "stages": [
                    {"name": "preflight", "ok": True, "stdout": "secret-ish noise"},
                    {"name": "install", "ok": True, "returncode": 0},
                ],
            },
            {
                "name": "offline",
                "status": "failed",
                "stages": [{"name": "preflight", "ok": False, "error": "timeout", "stderr": "details"}],
            },
        ],
    }


def test_missing_state_is_versioned_and_dry_run_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "state" / "publish-state.json"
    assert load_publish_state(path) == {
        "version": 1,
        "updated_at": None,
        "last_publish": None,
        "peers": {},
    }

    result = record_publish_result(path, _result(dry_run=True), occurred_at="2026-07-15T10:00:00Z")
    assert result["peers"] == {}
    assert not path.exists()


def test_record_publish_result_persists_summary_without_raw_command_output(tmp_path: Path) -> None:
    path = tmp_path / "publish-state.json"
    state = record_publish_result(
        path,
        _result(),
        release_sha256="a" * 64,
        occurred_at="2026-07-15T10:00:00Z",
    )

    assert state["last_publish"] == {
        "release_name": "music-kb-2026w30",
        "release_sha256": "a" * 64,
        "occurred_at": "2026-07-15T10:00:00Z",
        "succeeded_count": 1,
        "failed_count": 1,
    }
    assert state["peers"]["online"]["last_success"]["status"] == "succeeded"
    assert state["peers"]["offline"]["last_attempt"]["status"] == "failed"
    rendered = path.read_text(encoding="utf-8")
    assert "secret-ish noise" not in rendered
    assert "details" not in rendered
    assert "a" * 64 in rendered
    assert list(path.parent.glob(".*.tmp")) == []


def test_failed_retry_preserves_previous_success(tmp_path: Path) -> None:
    path = tmp_path / "publish-state.json"
    record_publish_result(path, _result(), release_sha256="a" * 64, occurred_at="2026-07-15T10:00:00Z")
    retry = {
        "dry_run": False,
        "release_name": "music-kb-2026w31",
        "succeeded_count": 0,
        "failed_count": 1,
        "peers": [{"name": "online", "status": "failed", "stages": [{"name": "preflight", "ok": False}]}],
    }
    state = record_publish_result(path, retry, release_sha256="b" * 64, occurred_at="2026-07-22T10:00:00Z")
    assert state["peers"]["online"]["last_attempt"]["release_name"] == "music-kb-2026w31"
    assert state["peers"]["online"]["last_success"]["release_name"] == "music-kb-2026w30"


def test_invalid_state_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "publish-state.json"
    path.write_text(json.dumps({"version": 99, "peers": {}}), encoding="utf-8")
    with pytest.raises(ValidationError, match="version must be 1"):
        load_publish_state(path)
