from __future__ import annotations

import json
from pathlib import Path

from music_kb.schema import initialize_database
from music_kb.weekly_orchestration import (
    _cleanup_gate_satisfied,
    _cnb_cleanup_receipt_is_acceptable,
    run_weekly_run,
)


FIXTURE = Path(__file__).parent / "fixtures" / "kugou_canonical_delivery.jsonl"
OPERATIONS = Path(__file__).parents[1] / "references" / "validated-operations.json"


def test_cleanup_gate_requires_publish_and_release() -> None:
    assert not _cleanup_gate_satisfied(
        publish=False,
        release_result={"release_dir": "/tmp/release"},
        skip_peers=True,
        publish_result={},
    )
    assert not _cleanup_gate_satisfied(
        publish=True,
        release_result=None,
        skip_peers=True,
        publish_result={},
    )


def test_cleanup_gate_accepts_explicit_peer_skip_after_release() -> None:
    assert _cleanup_gate_satisfied(
        publish=True,
        release_result={"release_dir": "/tmp/release"},
        skip_peers=True,
        publish_result={"peer_count": 0, "failed_count": 0},
    )


def test_cleanup_gate_requires_all_selected_peers_without_skip() -> None:
    release = {"release_dir": "/tmp/release"}
    assert _cleanup_gate_satisfied(
        publish=True,
        release_result=release,
        skip_peers=False,
        publish_result={"peer_count": 2, "failed_count": 0},
    )
    assert not _cleanup_gate_satisfied(
        publish=True,
        release_result=release,
        skip_peers=False,
        publish_result={"peer_count": 2, "failed_count": 1},
    )
    assert not _cleanup_gate_satisfied(
        publish=True,
        release_result=release,
        skip_peers=False,
        publish_result={"peer_count": 0, "failed_count": 0},
    )


def test_cnb_cleanup_accepts_visible_cleanup_while_server_gc_is_pending() -> None:
    assert _cnb_cleanup_receipt_is_acceptable(
        {
            "visible_cleanup_complete": True,
            "failures": [],
            "clean": False,
            "server_gc_pending": True,
        }
    )
    assert not _cnb_cleanup_receipt_is_acceptable(
        {
            "visible_cleanup_complete": True,
            "failures": [{"kind": "branch"}],
            "clean": False,
            "server_gc_pending": True,
        }
    )


def test_supplied_delivery_resumes_after_analysis_without_upstream_work(tmp_path: Path) -> None:
    delivery = tmp_path / "canonical_delivery.jsonl"
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        row["source_url"] = f"https://www.kugou.com/mixsong/{row['id']}.html"
    delivery.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )

    database = tmp_path / "master.sqlite"
    initialize_database(database)
    inventory = tmp_path / "data" / "song_inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text('{"schema_version":1,"songs":[]}\n', encoding="utf-8")
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    progress = tmp_path / "download_progress.json"
    progress.write_text("{}\n", encoding="utf-8")

    result = run_weekly_run(
        workspace=tmp_path,
        run_id="supplied-delivery-resume",
        rank_ids=(),
        chart_page=1,
        chart_size=100,
        chart_profile=None,
        database=database,
        inventory=inventory,
        audio_root=audio_root,
        legacy_progress=progress,
        operations_file=OPERATIONS,
        output_dir=tmp_path / "releases",
        release_name="fixture-release",
        peers_file=None,
        peer_names=(),
        publish=False,
        delivery=delivery,
        cnb_command=None,
        chart_database=None,
        state_file=tmp_path / "publish-state.json",
        expected_count=2,
        skip_peers=True,
    )

    state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    for name in (
        "cnb_storage_preflight",
        "chart_capture",
        "chart_dedupe",
        "historical_dedupe",
        "claude_download",
        "fallback_download",
        "cnb_input_materialization",
    ):
        assert state["atoms"][name]["outputs"]["status"] == "skipped"
    assert state["atoms"]["cnb_analysis"]["outputs"]["status"] == "supplied_delivery_validated"
    assert state["atoms"]["cnb_analysis"]["outputs"]["count"] == 2
    assert state["atoms"]["knowledge_import"]["status"] == "succeeded"
    assert state["atoms"]["snapshot"]["outputs"]["verification"]["valid"] is True
    assert state["atoms"]["local_snapshot_install"]["outputs"]["status"] == "skipped"
    assert state["atoms"]["peer_publish"]["outputs"]["reason"] == "peer sync explicitly skipped"
    assert not (tmp_path / "data" / "weekly_runs" / "supplied-delivery-resume" / "charts").exists()


def test_supplied_delivery_can_install_publisher_snapshot_explicitly_in_dry_run(tmp_path: Path) -> None:
    delivery = tmp_path / "canonical_delivery.jsonl"
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        row["source_url"] = f"https://www.kugou.com/mixsong/{row['id']}.html"
    delivery.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )

    database = tmp_path / "master.sqlite"
    initialize_database(database)
    inventory = tmp_path / "data" / "song_inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text('{"schema_version":1,"songs":[]}\n', encoding="utf-8")
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    progress = tmp_path / "download_progress.json"
    progress.write_text("{}\n", encoding="utf-8")
    local_target = tmp_path / "publisher-client"

    result = run_weekly_run(
        workspace=tmp_path,
        run_id="supplied-delivery-local-install",
        rank_ids=(),
        chart_page=1,
        chart_size=100,
        chart_profile=None,
        database=database,
        inventory=inventory,
        audio_root=audio_root,
        legacy_progress=progress,
        operations_file=OPERATIONS,
        output_dir=tmp_path / "releases",
        release_name="fixture-local-install",
        local_snapshot_dir=local_target,
        install_local=True,
        peers_file=None,
        peer_names=(),
        publish=False,
        delivery=delivery,
        cnb_command=None,
        chart_database=None,
        state_file=tmp_path / "publish-state.json",
        expected_count=2,
        skip_peers=True,
    )

    state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
    atom = state["atoms"]["local_snapshot_install"]
    assert atom["status"] == "succeeded"
    assert atom["outputs"]["installed"] is True
    assert atom["outputs"]["previous_current"] is None
    assert (local_target / "current.sqlite").is_symlink()
    assert (local_target / "current.sqlite").resolve().name == "fixture-local-install.sqlite"
