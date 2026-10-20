from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from music_kb.publish_state import load_publish_state
from music_kb.repository import MusicKBRepository
from music_kb.schema import initialize_database
from music_kb.workflow import run_weekly_update


FIXTURE = Path(__file__).parent / "fixtures" / "analysis.json"
CAMPAIGN_FIXTURE = Path(__file__).parent / "fixtures" / "kugou_canonical_delivery.jsonl"


def _peers(path: Path, key: Path) -> None:
    path.write_text(
        f'''version = 2
[defaults]
identity_file = "{key}"
[[peers]]
name = "fixture-peer"
enabled = true
host = "fixture.example"
user = "music"
''',
        encoding="utf-8",
    )


class NoTransport:
    def __call__(self, command: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"transport must not run during prepare mode: {command} {timeout_seconds}")


class SuccessfulTransport:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, stdout="ok", stderr="")


def test_weekly_update_prepares_release_and_only_dry_runs_publish(tmp_path: Path, lyric_receipt_writer) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)
    state = tmp_path / "publish-state.json"
    lyrics = lyric_receipt_writer("generic", [("fixture", "fixture-001")])

    result = run_weekly_update(
        database=database,
        input_path=FIXTURE,
        input_kind="generic",
        expected_count=None,
        batch_size=1,
        output_dir=tmp_path / "releases",
        release_name="music-kb-atom3",
        lyric_receipt_paths=[lyrics],
        peers_file=peers,
        publish=False,
        state_file=state,
        runner=NoTransport(),
    )

    assert result["workflow"] == "weekly-update"
    assert result["import"]["count"] == 1
    assert result["tags"]["skipped"] is True
    assert result["validation"]["valid"] is True
    assert result["release_verification"]["valid"] is True
    assert result["local_install"]["status"] == "skipped"
    assert result["publish"]["dry_run"] is True
    assert result["publish"]["peers"][0]["status"] == "planned"
    assert not state.exists()
    assert result["audience"]["status"] == "skipped"
    assert {gap["source"] for gap in result["audience"]["coverage_gaps"]} == {
        "chart_evidence",
        "research_profiles",
    }


def test_weekly_update_publish_records_state_after_verified_fanout(tmp_path: Path, lyric_receipt_writer) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)
    state = tmp_path / "publish-state.json"
    runner = SuccessfulTransport()
    lyrics = lyric_receipt_writer("generic-publish", [("fixture", "fixture-001")])

    result = run_weekly_update(
        database=database,
        input_path=FIXTURE,
        input_kind="generic",
        expected_count=None,
        batch_size=1,
        output_dir=tmp_path / "releases",
        release_name="music-kb-atom3-publish",
        lyric_receipt_paths=[lyrics],
        peers_file=peers,
        publish=True,
        state_file=state,
        runner=runner,
    )

    assert result["publish"]["dry_run"] is False
    assert result["publish"]["succeeded_count"] == 1
    assert result["local_install"]["status"] == "succeeded"
    assert (tmp_path / "current.sqlite").is_symlink()
    assert (tmp_path / "current.sqlite").resolve().name == "music-kb-atom3-publish.sqlite"
    assert len(runner.commands) == 6
    saved = load_publish_state(state)
    assert saved["last_publish"]["release_name"] == "music-kb-atom3-publish"
    assert saved["peers"]["fixture-peer"]["last_success"]["status"] == "succeeded"
    assert saved["peers"]["fixture-peer"]["last_success"]["release_sha256"] == result["release_verification"]["sha256"]
    assert result["local_install"]["release_sha256"] == result["release_verification"]["sha256"]
    assert result["local_install"]["verification"]["valid"] is True


def test_weekly_update_rejects_publish_opt_out(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)

    with pytest.raises(ValueError, match="cannot be combined with --publish"):
        run_weekly_update(
            database=database,
            input_path=FIXTURE,
            input_kind="generic",
            expected_count=None,
            batch_size=1,
            output_dir=tmp_path / "releases",
            release_name="publish-opt-out",
            local_snapshot_dir=tmp_path / "publisher",
            install_local=False,
            peers_file=peers,
            publish=True,
            state_file=tmp_path / "publish-state.json",
            runner=NoTransport(),
        )


def test_campaign_weekly_update_rejects_missing_source_links(tmp_path: Path, lyric_receipt_writer) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)
    lyrics = lyric_receipt_writer(
        "campaign-links",
        [("kugou", "kg-fixture-0001"), ("kugou", "kg-fixture-0002")],
    )

    with pytest.raises(ValueError, match="Source-link completeness gate failed"):
        run_weekly_update(
            database=database,
            input_path=CAMPAIGN_FIXTURE,
            input_kind="campaign",
            expected_count=2,
            batch_size=1,
            output_dir=tmp_path / "releases",
            release_name="campaign-link-gate",
            lyric_receipt_paths=[lyrics],
            peers_file=peers,
            publish=False,
            state_file=tmp_path / "publish-state.json",
            runner=NoTransport(),
        )


def test_weekly_update_imports_audience_inputs_before_validate(
    tmp_path: Path, lyric_receipt_writer
) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)
    lyrics = lyric_receipt_writer("generic-audience", [("fixture", "fixture-001")])

    chart_dir = tmp_path / "audience-charts"
    chart_dir.mkdir()
    (chart_dir / "chart-49224-p1-songs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platform": "kugou",
                "run_id": "weekly-update-audience",
                "summary": {
                    "run_id": "weekly-update-audience",
                    "rank_id": "49224",
                    "page": 1,
                    "size": 100,
                    "rank_start": 1,
                    "source_records": 1,
                    "captured_at": "2026-10-20T10:40:21+08:00",
                },
                "songs": [
                    {
                        "identity_key": "kugou:900",
                        "platform": "kugou",
                        "platform_track_key": "900",
                        "title": "霓虹夜航",
                        "artist": "示例乐队",
                        "chart_appearances": [{"rank": 3}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profiles = tmp_path / "audience-profiles.jsonl"
    profiles.write_text(
        json.dumps(
            {
                "kind": "profile",
                "recording": {"title": "霓虹夜航", "primary_artist": "示例乐队"},
                "profile": {
                    "target_platform": "douyin",
                    "time_window": "full_lifecycle",
                    "method_id": "version-matrix",
                    "method_version": "v1.1",
                    "status": "available",
                    "confidence": "low",
                    "is_proxy": 1,
                    "researched_at": "2026-10-20",
                },
                "sources": [
                    {
                        "source_platform": "web",
                        "source_key": "https://example.test/weekly-update",
                        "source_type": "report",
                        "fetched_at": "2026-10-20",
                        "evidence_level": "2_media",
                    }
                ],
                "observations": [
                    {
                        "dimension": "age",
                        "bucket": "31-40",
                        "value_low": 0.3,
                        "value_high": 0.4,
                        "value_mid": 0.35,
                        "evidence_level": "4_inferred",
                        "is_proxy": 1,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_weekly_update(
        database=database,
        input_path=FIXTURE,
        input_kind="generic",
        expected_count=None,
        batch_size=1,
        output_dir=tmp_path / "releases",
        release_name="weekly-update-audience",
        lyric_receipt_paths=[lyrics],
        peers_file=peers,
        publish=False,
        state_file=tmp_path / "publish-state.json",
        runner=NoTransport(),
        audience_input=profiles,
        audience_charts=chart_dir,
    )

    assert result["validation"]["valid"] is True
    audience = result["audience"]
    assert audience["status"] == "succeeded"
    chart_evidence = audience["chart_evidence"]
    assert chart_evidence["status"] == "imported"
    assert chart_evidence["matched_entries"] == 1
    assert chart_evidence["matched_recordings"] == 1
    assert chart_evidence["ambiguous"] == 0
    assert chart_evidence["unmatched"] == 0
    assert chart_evidence["snapshot_date"] == "2026-10-20"
    assert audience["profiles"]["import"]["profiles"] == 1
    assert isinstance(audience["tags"], int)
    assert audience["coverage_gaps"] == []
    with MusicKBRepository(database, read_only=True) as repository:
        audience_row = repository.get_audience("rec_neon_night_studio")
    groups = {group["group"]: group for group in audience_row["groups"]}
    assert groups["chart_evidence"]["profiles"][0]["confidence"] == "low"
    assert groups["chart_evidence"]["observations"][0]["bucket"] == "gen/00后"
    assert groups["research"]["profiles"][0]["method_id"] == "version-matrix"
