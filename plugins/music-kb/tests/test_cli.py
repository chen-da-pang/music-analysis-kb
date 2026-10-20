from __future__ import annotations

import json
import base64
import struct
import os
import sqlite3
import pytest
import subprocess
import sys
from pathlib import Path

import music_kb.cli as cli
from music_kb.repository import MusicKBRepository
from music_kb.mcp_server import ReadOnlyMusicKB


FIXTURE = Path(__file__).parent / "fixtures" / "analysis.json"


def test_weekly_run_has_no_embedding_bypass():
    help_result = run_cli("weekly-run", "--help")
    assert help_result.returncode == 0
    assert "--skip-audio-embedding" not in help_result.stdout
    rejected = run_cli("weekly-run", "--skip-audio-embedding")
    assert rejected.returncode != 0


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "music_kb.cli", *arguments], text=True, capture_output=True, check=False
    )


@pytest.mark.parametrize("channel", ["overall", "rhythm", "timbre"])
def test_cli_similar_reads_installed_snapshot(embedded_snapshot, channel):
    database = embedded_snapshot["current_database"]
    result = run_cli("--json", "similar", "--db", database,
                     "--recording-id", "rec_neon_night_studio", "--channel", channel, "--limit", "1")
    assert result.returncode == 0, result.stderr
    expected = ReadOnlyMusicKB(database).similar(
        recording_id="rec_neon_night_studio", channel=channel, limit=1,
    )
    assert json.loads(result.stdout)["result"] == expected
    assert expected["results"][0]["recording_id"] == "neighbor"


def test_cli_similar_rejects_v7_snapshot(embedded_snapshot):
    database = embedded_snapshot["current_database"]
    os.chmod(database, 0o644)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE recording_embedding")
        connection.execute("DROP TABLE embedding_model")
        connection.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    result = run_cli("--json", "similar", "--db", database,
                     "--recording-id", "rec_neon_night_studio")
    assert result.returncode == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "database_not_initialized"
    assert error["message"] == "Unsupported schema version 7; expected 8."


def test_cli_embedding_import_similar_and_doctor(master_database, tmp_path):
    source = tmp_path / "vectors.jsonl"
    source.write_text(json.dumps({
        "recording_id": "rec_neon_night_studio", "audio_sha256": "a" * 64,
        "vector_b64": base64.b64encode(struct.pack("<ff", 1, 0)).decode(),
    }) + "\n", encoding="utf-8")
    arguments = ["--json", "import-embeddings", "--db", str(master_database),
                 "--input", str(source), "--model-id", "muq-v3", "--model-name", "MuQ-MuLan",
                 "--recipe", "v3", "--dim", "2", "--metric", "cosine", "--license", "CC-BY-NC-4.0"]
    for _ in range(2):
        imported = run_cli(*arguments)
        assert imported.returncode == 0, imported.stderr
        assert json.loads(imported.stdout)["result"]["imported"] == 1
    similar = run_cli("similar", "--json", "--db", str(master_database),
                      "--recording-id", "rec_neon_night_studio", "--channel", "overall")
    assert similar.returncode == 0, similar.stderr
    assert json.loads(similar.stdout)["result"]["results"] == []
    doctor = run_cli("--json", "doctor", "--db", str(master_database))
    result = json.loads(doctor.stdout)["result"]
    assert result["counts"]["embedding_models"] == 1
    assert result["counts"]["recordings_with_embeddings"] == 1
    assert result["embedding_coverage"]["ratio"] == 1.0


def test_cli_similar_missing_vector_and_bad_channel_are_structured(master_database):
    for channel, code in [("overall", "not_found"), ("melody", "validation_error")]:
        result = run_cli("--json", "similar", "--db", str(master_database),
                         "--recording-id", "rec_neon_night_studio", "--channel", channel)
        assert result.returncode == 2
        error = json.loads(result.stderr)
        assert error["ok"] is False
        assert error["error"]["code"] == code
        assert error["error"]["details"]["channel"] == channel


def test_cli_similar_human_distance_has_three_decimals(master_database, fixture_payload):
    from copy import deepcopy

    with MusicKBRepository(master_database) as repository:
        payload = deepcopy(fixture_payload)
        payload["recording"].update(id="other", title="Other", audio_sha256="b" * 64)
        payload["source_tracks"] = []
        repository.import_analysis(payload)
        repository.import_embeddings([
            dict(recording_id="rec_neon_night_studio", audio_sha256="a" * 64, vector=struct.pack("<ff", 1, 0)),
            dict(recording_id="other", audio_sha256="b" * 64, vector=struct.pack("<ff", 3, 4)),
        ], model_id="muq-v3", model_name="MuQ-MuLan", recipe="v3", dim=2,
            metric="cosine", weight_license="CC-BY-NC-4.0")
    result = run_cli("similar", "--db", str(master_database), "--recording-id", "rec_neon_night_studio")
    assert result.returncode == 0, result.stderr
    assert "0.400" in result.stdout
    assert "Other" in result.stdout


@pytest.mark.parametrize("suffix", [b"\r\n", b"", b"\n\n", b"\n{bad}\n", None])
def test_cli_embedding_jsonl_failure_rolls_back(master_database, tmp_path, suffix):
    source = tmp_path / "bad.jsonl"
    line = json.dumps({"recording_id": "rec_neon_night_studio", "audio_sha256": "a" * 64,
                       "vector_b64": base64.b64encode(struct.pack("<ff", 1, 0)).decode()}).encode()
    source.write_bytes(line + suffix if suffix is not None else b"")
    result = run_cli("--json", "import-embeddings", "--db", str(master_database),
                     "--input", str(source), "--model-id", "muq-v3", "--model-name", "MuQ-MuLan",
                     "--recipe", "v3", "--dim", "2", "--metric", "cosine", "--license", "CC-BY-NC-4.0")
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "validation_error"
    with MusicKBRepository(master_database, read_only=True) as repository:
        assert repository.status()["counts"]["embedding_models"] == 0
        assert repository.status()["counts"]["recordings_with_embeddings"] == 0


def test_cli_json_lifecycle(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialized = run_cli("--json", "init", "--db", str(database))
    assert initialized.returncode == 0, initialized.stderr
    imported = run_cli("--json", "import-analysis", "--db", str(database), "--input", str(FIXTURE))
    assert imported.returncode == 0, imported.stderr
    searched = run_cli("--json", "search", "--db", str(database), "--tag", "颗粒人声切片")
    assert searched.returncode == 0, searched.stderr
    search_result = json.loads(searched.stdout)["result"]
    assert search_result["count"] == 1
    assert search_result["facet_scope"]["kind"] == "returned_results"
    assert search_result["facet_scope"]["recording_count"] == 1
    assert {item["name"] for item in search_result["facet_counts"]} == {
        "electronic pop",
        "granular vocal chop",
        "syncopated rimshot",
    }
    discovered = run_cli(
        "--json",
        "discover",
        "--db",
        str(database),
        "--tag",
        "颗粒人声切片",
    )
    assert discovered.returncode == 0, discovered.stderr
    discovery_result = json.loads(discovered.stdout)["result"]
    assert discovery_result["match_count"] == 1
    assert discovery_result["facet_scope"]["kind"] == "all_matches"
    assert "results" not in discovery_result

    recommended = run_cli(
        "--json",
        "recommend",
        "--db",
        str(database),
        "--tag",
        "颗粒人声切片",
        "--limit",
        "1",
    )
    assert recommended.returncode == 0, recommended.stderr
    recommendation_result = json.loads(recommended.stdout)["result"]
    assert recommendation_result["count"] == 1
    assert recommendation_result["results"][0]["recording_id"] == "rec_neon_night_studio"
    assert "source_links" not in recommendation_result["results"][0]
    valid = run_cli("--json", "validate", "--db", str(database))
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["result"]["valid"] is True


def _generic_payload(recording_id: str, *, title: str) -> dict[str, object]:
    return {
        "recording": {"id": recording_id, "title": title},
        "artists": [{"name": "批量导入测试艺人"}],
        "analysis": {
            "raw_text": "批量 JSONL 导入保留 U+2028 分隔符：第一段\u2028第二段。",
            "summary": "streaming CLI regression fixture",
            "quality_state": "passed",
        },
        "tags": [{"namespace": "production", "name": "streaming test tag"}],
    }


def test_cli_streams_jsonl_and_reports_bounded_summary(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialized = run_cli("--json", "init", "--db", str(database))
    assert initialized.returncode == 0, initialized.stderr

    source = tmp_path / "generic.ndjson"
    source.write_text(
        "\n".join(
            json.dumps(_generic_payload(f"rec_stream_{index}", title=f"stream {index}"), ensure_ascii=False)
            for index in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    imported = run_cli(
        "--json",
        "import-analysis",
        "--db",
        str(database),
        "--input",
        str(source),
        "--batch-size",
        "1",
    )
    assert imported.returncode == 0, imported.stderr
    result = json.loads(imported.stdout)["result"]
    assert {key: result[key] for key in result if key not in {"imports", "imports_returned", "imports_truncated"}} == {
        "count": 3,
        "created_count": 3,
        "idempotent_count": 0,
        "canonical_count": 3,
        "batch_size": 1,
        "batch_count": 3,
        "search_projection_rebuilt": True,
    }
    assert len(result["imports"]) == 3
    assert result["imports_returned"] == 3
    assert result["imports_truncated"] is False
    searched = run_cli("--json", "search", "--db", str(database), "--tag", "streaming test tag")
    assert searched.returncode == 0, searched.stderr
    assert json.loads(searched.stdout)["result"]["count"] == 3


def test_cli_rebuild_search_recovers_after_interrupted_jsonl_batch(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    assert run_cli("--json", "init", "--db", str(database)).returncode == 0
    source = tmp_path / "broken.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(_generic_payload("rec_recover", title="recover"), ensure_ascii=False),
                json.dumps({"recording": {"id": "rec_invalid"}, "artists": [{"name": "bad"}]}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    interrupted = run_cli(
        "--json",
        "import-analysis",
        "--db",
        str(database),
        "--input",
        str(source),
        "--batch-size",
        "1",
    )
    assert interrupted.returncode == 2
    assert "recording.title" in json.loads(interrupted.stderr)["error"]["message"]

    invalid = run_cli("--json", "validate", "--db", str(database))
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout)["result"]["valid"] is False
    assert any(
        issue["code"] == "search_projection_dirty"
        for issue in json.loads(invalid.stdout)["result"]["issues"]
    )

    rebuilt = run_cli("--json", "rebuild-search", "--db", str(database))
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert json.loads(rebuilt.stdout)["result"]["recording_count"] == 1
    valid = run_cli("--json", "validate", "--db", str(database))
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["result"]["valid"] is True


def test_cli_prepares_identity_bound_lyrics_backfill_queue(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    assert run_cli("--json", "init", "--db", str(database)).returncode == 0
    assert run_cli("--json", "import-analysis", "--db", str(database), "--input", str(FIXTURE)).returncode == 0
    with MusicKBRepository(database) as repository:
        with repository.connection:
            repository.connection.execute(
                "UPDATE source_track SET source_name = 'kugou', source_track_id = 'kugou-999'"
            )

    queue = tmp_path / "operational" / "lyrics-backfill.jsonl"
    result = run_cli(
        "--json",
        "prepare-lyrics-backfill",
        "--db",
        str(database),
        "--output",
        str(queue),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)["result"]
    assert payload["queue_count"] == 1
    assert payload["queue"] == str(queue)
    assert json.loads(queue.read_text(encoding="utf-8"))["source_track_id"] == "kugou-999"


def test_doctor_reports_missing_default_database(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("MUSIC_KB_DB", str(missing))
    result = run_cli("--json", "doctor")
    assert result.returncode == 1
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is False
    assert parsed["result"]["ready"] is False


def test_cli_publish_push_dry_run_has_a_machine_readable_plan(tmp_path: Path, lyric_seed) -> None:
    database = tmp_path / "master.sqlite"
    assert run_cli("--json", "init", "--db", str(database)).returncode == 0
    assert run_cli("--json", "import-analysis", "--db", str(database), "--input", str(FIXTURE)).returncode == 0
    with MusicKBRepository(database) as repository:
        lyric_seed(repository)
    created = run_cli(
        "--json",
        "snapshot",
        "create",
        "--db",
        str(database),
        "--output-dir",
        str(tmp_path / "releases"),
        "--name",
        "cli-distribution-fixture",
    )
    assert created.returncode == 0, created.stderr
    release_dir = json.loads(created.stdout)["result"]["release_dir"]

    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    peers.write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[defaults]",
                f'identity_file = "{key}"',
                "",
                "[[peers]]",
                'name = "cli-peer"',
                'host = "cli.example.test"',
                'user = "music-user"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    pushed = run_cli(
        "--json",
        "publish",
        "push",
        "--release-dir",
        release_dir,
        "--peers-file",
        str(peers),
        "--dry-run",
    )

    assert pushed.returncode == 0, pushed.stderr
    parsed = json.loads(pushed.stdout)
    assert parsed["ok"] is True
    assert parsed["result"]["release_name"] == "cli-distribution-fixture"
    assert parsed["result"]["peers"][0]["status"] == "planned"


def test_peer_inventory_defaults_to_private_local_path(monkeypatch) -> None:
    monkeypatch.delenv("MUSIC_KB_PEERS_FILE", raising=False)
    parsed = cli.build_parser().parse_args(
        [
            "weekly-update",
            "--db",
            "/tmp/master.sqlite",
            "--input",
            "/tmp/input.jsonl",
            "--output-dir",
            "/tmp/releases",
        ]
    )
    assert parsed.peers_file == Path("~/.config/music-kb/peers.toml").expanduser()


def test_weekly_commands_expose_local_snapshot_install_controls() -> None:
    parser = cli.build_parser()
    weekly = parser.parse_args(
        [
            "weekly-run",
            "--run-id",
            "fixture-run",
            "--local-snapshot-dir",
            "/tmp/publisher-client",
            "--install-local",
            "--confirm-delete-cnb-repositories",
        ]
    )
    assert weekly.local_snapshot_dir == Path("/tmp/publisher-client")
    assert weekly.install_local is True
    assert weekly.confirm_delete_cnb_repositories is True

    update = parser.parse_args(
        [
            "weekly-update",
            "--db",
            "/tmp/master.sqlite",
            "--input",
            "/tmp/input.jsonl",
            "--output-dir",
            "/tmp/releases",
            "--no-install-local",
        ]
    )
    assert update.install_local is False


def test_weekly_run_exposes_disposable_campaign_controls() -> None:
    parser = cli.build_parser()
    parsed = parser.parse_args(
        [
            "weekly-run",
            "--run-id",
            "fixture-run",
            "--cnb-campaign-dry-run",
            "--cnb-campaign-poll-seconds",
            "0.5",
            "--cnb-campaign-timeout-seconds",
            "120",
            "--cnb-github-commit",
            "a" * 40,
            "--cnb-campaign-work-dir",
            "/tmp/campaign-work",
            "--retained-campaign-receipt",
            "/tmp/retained-campaign-receipt.json",
        ]
    )
    assert parsed.cnb_campaign_dry_run is True
    assert parsed.cnb_campaign_poll_seconds == 0.5
    assert parsed.cnb_campaign_timeout_seconds == 120
    assert parsed.cnb_github_commit == "a" * 40
    assert parsed.cnb_campaign_work_dir == Path("/tmp/campaign-work")
    assert parsed.retained_campaign_receipt == Path("/tmp/retained-campaign-receipt.json")


def test_weekly_run_exposes_confirm_cnb_analysis_flag() -> None:
    parser = cli.build_parser()
    parsed = parser.parse_args(
        ["weekly-run", "--run-id", "fixture-run", "--confirm-cnb-analysis"]
    )
    assert parsed.confirm_cnb_analysis is True
    default = parser.parse_args(["weekly-run", "--run-id", "fixture-run"])
    assert default.confirm_cnb_analysis is False


def test_peer_inventory_environment_override(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "custom-peers.toml"
    monkeypatch.setenv("MUSIC_KB_PEERS_FILE", str(override))
    parsed = cli.build_parser().parse_args(
        ["publish", "push", "--release-dir", "/tmp/release"]
    )
    assert parsed.peers_file == override


def test_cli_publish_partial_failure_exits_one_with_json_result(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "publish_snapshot",
        lambda *args, **kwargs: {
            "release_name": "fixture-release",
            "release_dir": "/private/release",
            "dry_run": False,
            "peer_count": 2,
            "succeeded_count": 1,
            "failed_count": 1,
            "peers": [],
        },
    )

    exit_code = cli.main(
        [
            "--json",
            "publish",
            "push",
            "--release-dir",
            "/private/release",
            "--peers-file",
            "/private/peers.toml",
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "result": {
            "dry_run": False,
            "failed_count": 1,
            "peer_count": 2,
            "peers": [],
            "release_dir": "/private/release",
            "release_name": "fixture-release",
            "succeeded_count": 1,
        },
    }
