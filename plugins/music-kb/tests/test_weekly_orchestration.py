from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

from music_kb.schema import initialize_database
from music_kb.weekly_orchestration import (
    _cleanup_gate_satisfied,
    _cnb_cleanup_receipt_is_acceptable,
    _inventory_database_path,
    _resolve_campaign_repository_root,
    run_weekly_run,
)


FIXTURE = Path(__file__).parent / "fixtures" / "kugou_canonical_delivery.jsonl"
OPERATIONS = Path(__file__).parents[1] / "references" / "validated-operations.json"


def _delivery_lyrics(lyric_receipt_writer, name: str) -> Path:
    return lyric_receipt_writer(
        name,
        [("kugou", "kg-fixture-0001"), ("kugou", "kg-fixture-0002")],
    )


def test_resolve_campaign_repository_root_from_nested_data_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "publisher-workspace"
    repository = workspace / "music-analysis-kb"
    repository.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "remote", "add", "origin", "https://github.com/chen-da-pang/music-analysis-kb"],
        check=True,
    )
    source_root = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(
        "music_kb.weekly_orchestration._git_worktree_clean",
        lambda path: path != source_root,
    )

    assert _resolve_campaign_repository_root(workspace) == repository.resolve()


def test_inventory_database_path_honors_explicit_chart_database(tmp_path: Path) -> None:
    explicit = tmp_path / "authoritative" / "music_trends.sqlite"

    assert _inventory_database_path(tmp_path, explicit) == explicit
    assert _inventory_database_path(tmp_path, None) == tmp_path / "data" / "music_trends.sqlite"


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
    assert not _cnb_cleanup_receipt_is_acceptable(
        {
            "visible_cleanup_complete": True,
            "failures": [],
            "clean": False,
            "server_gc_pending": True,
            "repository_cleanup_required": True,
            "destructive_repository_cleanup_complete": False,
        }
    )
    assert _cnb_cleanup_receipt_is_acceptable(
        {
            "visible_cleanup_complete": True,
            "failures": [],
            "clean": False,
            "server_gc_pending": True,
            "repository_cleanup_required": True,
            "destructive_repository_cleanup_complete": True,
        }
    )


def test_weekly_run_rejects_publish_opt_out(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be combined with --publish"):
        run_weekly_run(
            workspace=tmp_path,
            run_id="publish-opt-out",
            rank_ids=(),
            chart_page=1,
            chart_size=100,
            chart_profile=None,
            database=tmp_path / "master.sqlite",
            inventory=tmp_path / "data" / "song_inventory.json",
            audio_root=tmp_path / "audio",
            legacy_progress=tmp_path / "download_progress.json",
            operations_file=OPERATIONS,
            output_dir=tmp_path / "releases",
            release_name="publish-opt-out",
            local_snapshot_dir=tmp_path / "publisher",
            install_local=False,
            peers_file=None,
            peer_names=(),
            publish=True,
            delivery=None,
            cnb_command=None,
            chart_database=None,
            state_file=tmp_path / "publish-state.json",
        )


def test_supplied_delivery_resumes_after_analysis_without_upstream_work(tmp_path: Path, lyric_receipt_writer) -> None:
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
    lyrics = _delivery_lyrics(lyric_receipt_writer, "supplied-delivery")

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
        lyric_receipt_paths=[lyrics],
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


def test_supplied_delivery_can_install_publisher_snapshot_explicitly_in_dry_run(
    tmp_path: Path, lyric_receipt_writer
) -> None:
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
    lyrics = _delivery_lyrics(lyric_receipt_writer, "supplied-local-install")

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
        lyric_receipt_paths=[lyrics],
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
    assert atom["outputs"]["release_sha256"] == state["atoms"]["snapshot"]["outputs"]["verification"]["sha256"]
    assert atom["outputs"]["verification"]["valid"] is True
    assert (local_target / "current.sqlite").is_symlink()
    assert (local_target / "current.sqlite").resolve().name == "fixture-local-install.sqlite"


def test_real_publish_defaults_to_local_snapshot_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lyric_receipt_writer
) -> None:
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
    inventory.write_text('{"schema_version":1,"songs":[{}]}\n', encoding="utf-8")
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    progress = tmp_path / "download_progress.json"
    progress.write_text("{}\n", encoding="utf-8")
    lyrics = _delivery_lyrics(lyric_receipt_writer, "supplied-real-publish")

    def fake_json_command(command, *, cwd, timeout_seconds, env=None):
        return {"status": "succeeded"}, subprocess.CompletedProcess(command, 0, "", "")

    def fake_subprocess_run(command, **kwargs):
        payload = {
            "visible_cleanup_complete": True,
            "failures": [],
            "clean": True,
            "server_gc_pending": False,
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("music_kb.weekly_orchestration._json_command", fake_json_command)
    monkeypatch.setattr("music_kb.weekly_orchestration.subprocess.run", fake_subprocess_run)

    result = run_weekly_run(
        workspace=tmp_path,
        run_id="supplied-delivery-default-local-install",
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
        release_name="default-local-install",
        peers_file=None,
        peer_names=(),
        publish=True,
        delivery=delivery,
        lyric_receipt_paths=[lyrics],
        cnb_command=None,
        chart_database=None,
        state_file=tmp_path / "publish-state.json",
        expected_count=2,
        confirm_delete_audio=True,
        confirm_delete_cnb_storage=True,
        confirm_delete_cnb_repositories=True,
        skip_peers=True,
    )

    state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
    atom = state["atoms"]["local_snapshot_install"]
    assert atom["inputs"]["enabled"] is True
    assert atom["outputs"]["status"] == "succeeded"
    assert (tmp_path / "current.sqlite").is_symlink()
    assert (tmp_path / "current.sqlite").resolve().name == "default-local-install.sqlite"


def test_fresh_run_materializes_queue_and_records_disposable_campaign_atoms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    inventory = tmp_path / "data" / "song_inventory.json"
    inventory.parent.mkdir(parents=True)
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    audio_file = audio_root / "song.flac"
    audio_file.write_bytes(b"audio-bytes")
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "songs": [
                    {
                        "identity_key": "kugou:1",
                        "platform_track_key": "1",
                        "title": "Song",
                        "artist": "Artist",
                        "play_link": "https://www.kugou.com/mixsong/1.html",
                        "download": {"status": "downloaded", "path": "song.flac"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    progress = tmp_path / "download_progress.json"
    progress.write_text("{}\n", encoding="utf-8")
    chart_path = tmp_path / "chart.json"
    queue_path = tmp_path / "queue.jsonl"

    monkeypatch.setattr(
        "music_kb.weekly_orchestration.run_preflight",
        lambda **kwargs: {"valid": True, "failed_required": [], "commands": {}},
    )
    monkeypatch.setattr(
        "music_kb.weekly_orchestration._json_command_allow_failure",
        lambda *args, **kwargs: (
            {
                "action": "campaign-preflight",
                "clean": True,
                "checks": {},
            },
            subprocess.CompletedProcess(args[0], 0, "", ""),
        ),
    )

    def fake_json_command(command, *, cwd, timeout_seconds, env=None):
        name = Path(command[1]).name if len(command) > 1 else ""
        if name == "capture_kugou_chart.py":
            chart_path.write_text(
                json.dumps(
                    {
                        "songs": [
                            {
                                "identity_key": "kugou:1",
                                "platform_track_key": "1",
                                "title": "Song",
                                "artist": "Artist",
                                "play_link": "https://www.kugou.com/mixsong/1.html",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return {"songs": str(chart_path), "source_records": 1}, subprocess.CompletedProcess(command, 0, "", "")
        if name == "build_song_inventory.py":
            return {"songs": 1}, subprocess.CompletedProcess(command, 0, "", "")
        if name == "prepare_download_queue.py":
            queue_path.write_text(json.dumps({"identity_key": "kugou:1"}) + "\n", encoding="utf-8")
            return {
                "queue": str(queue_path),
                "queued": 1,
                "skipped_existing_download": 0,
            }, subprocess.CompletedProcess(command, 0, "", "")
        if name == "run_claude_download.py":
            return {
                "queue_manifest": {"queue": str(queue_path), "queued": 1},
                "worker_progress": {"downloaded": 1},
            }, subprocess.CompletedProcess(command, 0, "", "")
        if name == "run_claude_fallback.py":
            return {"queue_manifest": {"queued": 0}}, subprocess.CompletedProcess(command, 0, "", "")
        if name == "cnb_campaign_repository.py" and "prepare" in command:
            receipt = Path(command[command.index("--receipt") + 1])
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "planned",
                        "run_id": "fresh-campaign",
                        "repository": "wuyoumusic/music-flamingo-campaign-fresh-campaign",
                        "runtime_image": "docker.cnb.cool/wuyoumusic/moss-music-runner@sha256:a04cdbc02ef0f0958282e7bbf8c3a15b3a3105f4d17c95db88c98d1fc5f3657b",
                    }
                ),
                encoding="utf-8",
            )
            return {"status": "planned", "receipt": str(receipt)}, subprocess.CompletedProcess(command, 0, "", "")
        if name == "cnb_campaign_repository.py" and "submit" in command:
            return {"status": "submit_planned"}, subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    monkeypatch.setattr("music_kb.weekly_orchestration._json_command", fake_json_command)
    result = run_weekly_run(
        workspace=tmp_path,
        run_id="fresh-campaign",
        rank_ids=["1"],
        chart_page=1,
        chart_size=100,
        chart_profile=None,
        database=database,
        inventory=inventory,
        audio_root=audio_root,
        legacy_progress=progress,
        operations_file=OPERATIONS,
        output_dir=tmp_path / "releases",
        release_name="fresh-campaign-release",
        peers_file=None,
        peer_names=(),
        publish=False,
        delivery=None,
        cnb_command=None,
        chart_database=None,
        state_file=tmp_path / "publish-state.json",
        cnb_campaign_dry_run=True,
        cnb_github_commit="a" * 40,
        skip_peers=True,
    )
    state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    assert state["atoms"]["cnb_input_materialization"]["outputs"]["item_count"] == 1
    assert state["atoms"]["cnb_campaign_repository"]["outputs"]["status"] == "planned"
    assert state["atoms"]["cnb_campaign_submit"]["outputs"]["status"] == "submit_planned"
    assert state["atoms"]["cnb_analysis"]["outputs"]["status"] == "skipped"
    assert (tmp_path / "data" / "weekly_runs" / "fresh-campaign" / "cnb-input" / "manifest.jsonl").is_file()


@pytest.mark.parametrize("receipt_completed", [False, True])
def test_weekly_run_resumes_disk_campaign_receipt_without_recapturing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_completed: bool, lyric_receipt_writer
) -> None:
    run_id = "resume-campaign"
    run_dir = tmp_path / "data" / "weekly_runs" / run_id
    staging = run_dir / "cnb-input"
    (staging / "audio").mkdir(parents=True)
    rows = [
        {
            "id": "kg-1",
            "relative_audio_path": "audio/one.mp3",
            "source_bytes": 4,
            "sha256": hashlib.sha256(b"one!").hexdigest(),
            "title": "One",
            "artist": "Artist",
            "source_url": "https://www.kugou.com/mixsong/1.html",
            "campaign_id": run_id,
        },
        {
            "id": "kg-2",
            "relative_audio_path": "audio/two.mp3",
            "source_bytes": 4,
            "sha256": hashlib.sha256(b"two!").hexdigest(),
            "title": "Two",
            "artist": "Artist",
            "source_url": "https://www.kugou.com/mixsong/2.html",
            "campaign_id": run_id,
        },
    ]
    (staging / "audio" / "one.mp3").write_bytes(b"one!")
    (staging / "audio" / "two.mp3").write_bytes(b"two!")
    (staging / "manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    operations_hash = hashlib.sha256(OPERATIONS.read_bytes()).hexdigest()
    (run_dir / "run-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "failed",
                "started_at": "2026-07-21T00:00:00Z",
                "finished_at": "2026-07-21T00:01:00Z",
                "operations_file": str(OPERATIONS),
                "operations_sha256": operations_hash,
                "atoms": {},
                "errors": [{"atom": "cnb_campaign_submit", "message": "simulated"}],
            }
        ),
        encoding="utf-8",
    )
    delivery = tmp_path / "canonical.jsonl"
    delivery_rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]
    for index, row in enumerate(delivery_rows, 1):
        row["source_url"] = f"https://www.kugou.com/mixsong/{index}.html"
    delivery.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in delivery_rows),
        encoding="utf-8",
    )
    delivery_binding = {
        "path": str(delivery),
        "sha256": hashlib.sha256(delivery.read_bytes()).hexdigest(),
        "count": 2,
        "ledger_branch": f"campaign-results/{run_id}",
    }
    receipt = {
        "schema_version": 1,
        "atom": "cnb_campaign_repository",
        "status": "completed" if receipt_completed else "failed",
        "run_id": run_id,
        "repository": f"wuyoumusic/music-flamingo-campaign-{run_id}",
        "repository_prefix": "music-flamingo-campaign-",
        "organization": "wuyoumusic",
        "github_repository": "chen-da-pang/music-analysis-kb",
        "github_commit": "a" * 40,
        "runtime_image": "fixture",
        "runtime_digest": "fixture",
        "transport": "lfs",
        "manifest": {
            "path": str(staging / "manifest.jsonl"),
            "sha256": hashlib.sha256((staging / "manifest.jsonl").read_bytes()).hexdigest(),
            "item_count": 2,
            "source_bytes": 8,
            "source_links": 2,
            "campaign_id": run_id,
        },
        "workspace": str(run_dir / "cnb" / "campaign-repository"),
        "checkout": str(run_dir / "cnb" / "campaign-repository" / "repo"),
        "campaign_repository_config": str(run_dir / "cnb" / "campaign-repository" / "repo" / ".cnb.yml"),
        "repository_created": True,
        "repository_pushed": True,
        "builds": [],
        "delivery": delivery_binding if receipt_completed else None,
    }
    receipt_path = run_dir / "cnb" / "campaign-receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    class FakeCampaignAdapter:
        @staticmethod
        def load_campaign_policy(_path):
            return {}

        @staticmethod
        def _policy_with_transport(policy, _transport):
            return policy

        @staticmethod
        def _read_manifest(_path, expected_count=None):
            return {
                "path": str(staging / "manifest.jsonl"),
                "sha256": hashlib.sha256((staging / "manifest.jsonl").read_bytes()).hexdigest(),
                "item_count": 2,
                "source_bytes": 8,
                "source_links": 2,
                "campaign_id": run_id,
            }

        @staticmethod
        def validate_campaign_receipt_binding(*_args, **_kwargs):
            return {"valid": True, "errors": []}

    monkeypatch.setattr(
        "music_kb.weekly_orchestration._load_script_module",
        lambda _path, module_name: FakeCampaignAdapter if module_name == "music_kb_campaign_resume" else (_ for _ in ()).throw(AssertionError(module_name)),
    )
    preflight_commands: list[tuple[str, ...]] = []

    def fake_preflight(**kwargs):
        preflight_commands.append(tuple(kwargs["required_commands"]))
        return {"valid": True, "failed_required": [], "commands": {}}

    monkeypatch.setattr("music_kb.weekly_orchestration.run_preflight", fake_preflight)
    calls: list[str] = []
    campaign_calls: list[list[str]] = []

    def fake_allow(command, *, cwd, timeout_seconds, env=None):
        calls.append(command[2] if len(command) > 2 else "")
        if "cleanup" in command:
            return {"status": "dry_run", "clean": False}, subprocess.CompletedProcess(command, 0, "", "")
        return {"action": "campaign-preflight", "clean": True}, subprocess.CompletedProcess(command, 0, "", "")

    def fake_json(command, *, cwd, timeout_seconds, env=None):
        campaign_calls.append(list(command))
        if "capture_kugou_chart.py" in command or "run_claude_download.py" in command:
            raise AssertionError("resume must not recapture or download")
        script_name = Path(command[1]).name if len(command) > 1 else ""
        if script_name == "cnb_campaign_repository.py" and "prepare" in command:
            return {"status": "created_and_pushed"}, subprocess.CompletedProcess(command, 0, "", "")
        if script_name == "cnb_campaign_repository.py" and "submit" in command:
            return {
                "status": "completed",
                "delivery": {"path": str(delivery), "count": 2, "sha256": hashlib.sha256(delivery.read_bytes()).hexdigest()},
            }, subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    monkeypatch.setattr("music_kb.weekly_orchestration._json_command_allow_failure", fake_allow)
    monkeypatch.setattr("music_kb.weekly_orchestration._json_command", fake_json)
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"schema_version":1,"songs":[]}\n', encoding="utf-8")
    progress = tmp_path / "progress.json"
    progress.write_text("{}\n", encoding="utf-8")
    lyrics = _delivery_lyrics(lyric_receipt_writer, "campaign-resume")

    result = run_weekly_run(
        workspace=tmp_path,
        run_id=run_id,
        rank_ids=["1"],
        chart_page=1,
        chart_size=100,
        chart_profile=None,
        database=database,
        inventory=inventory,
        audio_root=tmp_path / "audio",
        legacy_progress=progress,
        operations_file=OPERATIONS,
        output_dir=tmp_path / "releases",
        release_name="resume-release",
        peers_file=None,
        peer_names=(),
        publish=False,
        delivery=None,
        lyric_receipt_paths=[lyrics],
        cnb_command=None,
        chart_database=None,
        state_file=tmp_path / "publish-state.json",
        cnb_github_commit="a" * 40,
        skip_peers=True,
    )
    state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    assert state["resume_count"] == 1
    assert preflight_commands == [()]
    assert state["atoms"]["chart_capture"]["outputs"]["status"] == "skipped"
    if receipt_completed:
        assert state["atoms"]["cnb_campaign_repository"]["outputs"]["status"] == "receipt_delivery_reused"
        assert state["atoms"]["cnb_campaign_submit"]["outputs"]["status"] == "receipt_delivery_reused"
        assert not any("submit" in command for command in campaign_calls)
    else:
        assert state["atoms"]["cnb_campaign_repository"]["outputs"]["status"] == "created_and_pushed"
        assert state["atoms"]["cnb_campaign_submit"]["outputs"]["status"] == "completed"
        assert any("submit" in command for command in campaign_calls)
    assert "preflight" in calls and "cleanup" in calls
