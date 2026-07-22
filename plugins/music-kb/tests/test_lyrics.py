from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from music_kb.errors import ValidationError
from music_kb.lyrics_backfill import materialize_lyric_backfill_queue
from music_kb.lyrics import LYRIC_NORMALIZER_VERSION, normalize_lyric_text
from music_kb.repository import MusicKBRepository
from music_kb.schema import SCHEMA_VERSION, initialize_database
from music_kb.snapshot import create_snapshot, verify_snapshot


def _source_track_row_id(repository: MusicKBRepository, recording_id: str) -> str:
    row = repository.connection.execute(
        "SELECT id FROM source_track WHERE recording_id = ?", (recording_id,)
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _lyric_payload(repository: MusicKBRepository, *, status: str = "available") -> dict[str, object]:
    recording_id = "rec_neon_night_studio"
    source_track_row_id = _source_track_row_id(repository, recording_id)
    evidence = {
        "source_name": "fixture",
        "source_track_id": "fixture-001",
        "reason": "fixture lyric response",
        "query_method": "exact_source_track",
    }
    payload: dict[str, object] = {
        "recording_id": recording_id,
        "source_track_row_id": source_track_row_id,
        "status": status,
        "evidence": evidence,
    }
    if status == "available":
        payload["lyric_text"] = "[ti:霓虹夜航]\n[00:01.20]第一句\n[00:03.00][00:04.00]副歌\n[Chorus]\n第二句\n"
    return payload


def test_normalize_lyric_text_removes_only_lrc_transport_data() -> None:
    assert normalize_lyric_text(
        "\ufeff[ar:示例乐队]\r\n[00:01.20]第一句\r\n[00:02.00][00:03.00]副歌\r\n[Chorus]\r\n"
    ) == "第一句\n副歌\n[Chorus]"


def test_imported_lyrics_are_identity_bound_readable_and_idempotent(unresolved_master_database) -> None:
    with MusicKBRepository(unresolved_master_database) as repository:
        payload = _lyric_payload(repository)
        result = repository.import_lyric(payload)
        assert result["status"] == "available"
        assert result["idempotent"] is False
        repeated = repository.import_lyric(payload)
        assert repeated["idempotent"] is True
        lyrics = repository.get_lyrics("rec_neon_night_studio")
        assert lyrics["status"] == "available"
        assert lyrics["lyric_text"] == "第一句\n副歌\n[Chorus]\n第二句"
        assert lyrics["normalizer_version"] == LYRIC_NORMALIZER_VERSION
        assert lyrics["source"]["track_id"] == "fixture-001"
        assert repository.lyric_coverage() == {
            "canonical_recordings": 1,
            "available": 1,
            "instrumental": 0,
            "platform_unavailable": 0,
            "pending": 0,
            "missing": 0,
            "unresolved": 0,
        }
        assert repository.validate(require_lyrics=True)["valid"] is True


def test_lyric_coverage_blocks_snapshot_until_terminal_result(unresolved_master_database, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Master database failed validation"):
        create_snapshot(unresolved_master_database, tmp_path / "releases", release_name="blocked-lyrics")
    with MusicKBRepository(unresolved_master_database) as repository:
        repository.import_lyric(_lyric_payload(repository, status="platform_unavailable"))
        assert repository.status()["counts"]["lyrics_platform_unavailable"] == 1
    release = create_snapshot(unresolved_master_database, tmp_path / "releases", release_name="ready-lyrics")
    assert verify_snapshot(release["manifest"])["valid"] is True


def test_lyric_import_rejects_identity_mismatch_and_preserves_terminal_result(unresolved_master_database) -> None:
    with MusicKBRepository(unresolved_master_database) as repository:
        payload = _lyric_payload(repository)
        repository.import_lyric(payload)
        pending = _lyric_payload(repository, status="pending")
        pending["evidence"] = {
            "source_name": "fixture",
            "source_track_id": "fixture-001",
            "reason": "temporary network failure",
        }
        preserved = repository.import_lyric(pending)
        assert preserved["preserved_terminal"] is True

        invalid = _lyric_payload(repository)
        invalid["evidence"] = {
            "source_name": "wrong-source",
            "source_track_id": "fixture-001",
            "reason": "wrong identity",
        }
        with pytest.raises(ValidationError, match="source_name"):
            repository.import_lyric(invalid)


def test_cc_receipt_file_binds_by_source_identity_not_title_artist(
    unresolved_master_database, tmp_path: Path
) -> None:
    with MusicKBRepository(unresolved_master_database) as repository:
        source_track_row_id = _source_track_row_id(repository, "rec_neon_night_studio")
        source = repository.connection.execute(
            "SELECT source_name, source_track_id FROM source_track WHERE id = ?",
            (source_track_row_id,),
        ).fetchone()
        assert source is not None
        receipt = {
            "schema_version": 1,
            "source_name": str(source["source_name"]),
            "source_track_id": str(source["source_track_id"]),
            "status": "available",
            "lyric_text": "[00:01.00]来自 CC 的第一句\n[00:02.00]第二句\n",
            "evidence": {
                "source_name": str(source["source_name"]),
                "source_track_id": str(source["source_track_id"]),
                "reason": "exact platform source identity returned lyrics",
                "query_method": "musicdl_kugou_exact_mix_song_id_v1",
            },
        }
        path = tmp_path / "lyrics.jsonl"
        path.write_text(json.dumps(receipt, ensure_ascii=False) + "\n", encoding="utf-8")
        imported = repository.import_lyric_receipt_file(path)
        assert imported["count"] == 1
        assert repository.get_lyrics("rec_neon_night_studio")["lyric_text"] == "来自 CC 的第一句\n第二句"

        wrong_recording = dict(receipt, recording_id="rec_other_version")
        with pytest.raises(ValidationError, match="recording_id"):
            repository.import_lyric_receipt(wrong_recording)


def _make_fixture_source_exact_kugou(repository: MusicKBRepository) -> None:
    with repository.connection:
        repository.connection.execute(
            """
            UPDATE source_track
            SET source_name = 'kugou', source_track_id = 'kugou-123456'
            WHERE recording_id = 'rec_neon_night_studio'
            """
        )


def _write_kugou_chart_database(
    path: Path,
    rows: list[tuple[str, str]],
) -> Path:
    """Create the minimal authoritative URL -> MixSongID bridge fixture."""

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE platform_tracks(
                platform TEXT NOT NULL,
                platform_track_key TEXT NOT NULL,
                play_link TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO platform_tracks(platform, platform_track_key, play_link) VALUES ('kugou', ?, ?)",
            rows,
        )
    return path


def _insert_exact_audio_provenance(
    repository: MusicKBRepository,
    *,
    delivery_id: str,
    manifest_index: int,
) -> None:
    recording = repository.connection.execute(
        """
        SELECT id, canonical_analysis_id, audio_sha256
        FROM recording
        WHERE id = 'rec_neon_night_studio'
        """
    ).fetchone()
    assert recording is not None
    with repository.connection:
        repository.connection.execute(
            """
            INSERT INTO campaign_delivery_provenance(
                id, delivery_schema_version, campaign_id, delivery_id, analysis_id,
                manifest_index, source_title, source_artist, relative_audio_path,
                source_sha256, source_bytes, output_text_sha256,
                generated_token_count, max_new_tokens, contract, attempt_id,
                canonical_source, provenance_json
            ) VALUES (?, 1, 'fixture-campaign', ?, ?, ?, '霓虹夜航', '示例乐队', ?, ?,
                      1, ?, 1, 1, 'fixture-contract', 'fixture-attempt', 'fixture-source', '{}')
            """,
            (
                f"prov_{delivery_id}",
                delivery_id,
                str(recording["canonical_analysis_id"]),
                manifest_index,
                f"audio/{delivery_id}.mp3",
                str(recording["audio_sha256"]),
                "b" * 64,
            ),
        )


def test_lyrics_backfill_queue_uses_only_unresolved_exact_kugou_source(unresolved_master_database) -> None:
    with MusicKBRepository(unresolved_master_database) as repository:
        _make_fixture_source_exact_kugou(repository)
        plan = repository.prepare_lyric_backfill_queue()

    assert plan["coverage"]["unresolved"] == 1
    assert plan["queue_count"] == 1
    assert plan["rows"] == [
        {
            "schema_version": 1,
            "recording_id": "rec_neon_night_studio",
            "source_track_row_id": plan["rows"][0]["source_track_row_id"],
            "source_name": "kugou",
            "source_track_id": "kugou-123456",
            "identity_key": "kugou:123456",
            "platform": "kugou",
            "platform_track_key": "123456",
            "title": "霓虹夜航",
            "artist": "示例乐队",
            "source_url": "https://music.example/listen/fixture-001",
            "existing_lyric_status": "missing",
            "identity_resolution": "source_track_id_kugou_prefix_v1",
            "source_identity_alias_count": 1,
        }
    ]


def test_lyrics_backfill_queue_refuses_ambiguous_kugou_source(unresolved_master_database) -> None:
    with MusicKBRepository(unresolved_master_database) as repository:
        _make_fixture_source_exact_kugou(repository)
        with repository.connection:
            repository.connection.execute(
                """
                INSERT INTO source_track(
                    id, recording_id, source_name, source_track_id,
                    source_title, source_artist_credit, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "src_extra_kugou",
                    "rec_neon_night_studio",
                    "kugou",
                    "kugou-654321",
                    "霓虹夜航",
                    "示例乐队",
                    "https://music.example/listen/fixture-001-alt",
                ),
            )
        with pytest.raises(ValidationError, match="lack a safe exact Kugou source identity"):
            repository.prepare_lyric_backfill_queue()


def test_lyrics_backfill_queue_maps_legacy_source_only_through_exact_chart_play_link(
    unresolved_master_database, tmp_path: Path
) -> None:
    legacy_url = "https://www.kugou.com/mixsong/agent_gateway/fixture-hash.html"
    chart_database = _write_kugou_chart_database(
        tmp_path / "music_trends.sqlite",
        [("987654", legacy_url)],
    )
    with MusicKBRepository(unresolved_master_database) as repository:
        with repository.connection:
            repository.connection.execute(
                """
                UPDATE source_track
                SET source_name = 'kugou', source_track_id = 'legacy-delivery-key',
                    source_url = ?
                WHERE recording_id = 'rec_neon_night_studio'
                """,
                (legacy_url,),
            )
        with pytest.raises(ValidationError, match="require --chart-db"):
            repository.prepare_lyric_backfill_queue()
        plan = repository.prepare_lyric_backfill_queue(chart_database=chart_database)

    assert plan["queue_count"] == 1
    assert plan["chart_database"] == str(chart_database.resolve())
    assert plan["identity_resolution_counts"] == {"chart_play_link_exact_v1": 1}
    assert plan["rows"][0]["source_track_id"] == "legacy-delivery-key"
    assert plan["rows"][0]["platform_track_key"] == "987654"
    assert plan["rows"][0]["identity_resolution"] == "chart_play_link_exact_v1"


def test_lyrics_backfill_queue_rejects_a_chart_link_with_multiple_platform_ids(
    unresolved_master_database, tmp_path: Path
) -> None:
    legacy_url = "https://www.kugou.com/mixsong/agent_gateway/ambiguous-fixture.html"
    chart_database = _write_kugou_chart_database(
        tmp_path / "ambiguous-music_trends.sqlite",
        [("111", legacy_url), ("222", legacy_url)],
    )
    with MusicKBRepository(unresolved_master_database) as repository:
        with repository.connection:
            repository.connection.execute(
                """
                UPDATE source_track
                SET source_name = 'kugou', source_track_id = 'legacy-delivery-key',
                    source_url = ?
                WHERE recording_id = 'rec_neon_night_studio'
                """,
                (legacy_url,),
            )
        with pytest.raises(ValidationError, match="multiple platform IDs"):
            repository.prepare_lyric_backfill_queue(chart_database=chart_database)


def test_lyrics_backfill_queue_deterministically_selects_proven_byte_identical_aliases(
    unresolved_master_database,
) -> None:
    with MusicKBRepository(unresolved_master_database) as repository:
        _make_fixture_source_exact_kugou(repository)
        _insert_exact_audio_provenance(
            repository,
            delivery_id="kugou-123456",
            manifest_index=0,
        )
        with repository.connection:
            repository.connection.execute(
                """
                INSERT INTO source_track(
                    id, recording_id, source_name, source_track_id,
                    source_title, source_artist_credit, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "src_exact_alias",
                    "rec_neon_night_studio",
                    "kugou",
                    "kugou-654321",
                    "霓虹夜航",
                    "示例乐队",
                    "https://music.example/listen/fixture-001-alias",
                ),
            )
        _insert_exact_audio_provenance(
            repository,
            delivery_id="kugou-654321",
            manifest_index=1,
        )
        plan = repository.prepare_lyric_backfill_queue()

    assert plan["queue_count"] == 1
    assert plan["rows"][0]["source_track_id"] == "kugou-123456"
    assert plan["rows"][0]["source_identity_alias_count"] == 2


def test_materialized_lyrics_backfill_queue_is_operational_jsonl(unresolved_master_database, tmp_path: Path) -> None:
    with MusicKBRepository(unresolved_master_database) as repository:
        _make_fixture_source_exact_kugou(repository)

    output = tmp_path / "operations" / "lyrics-backfill.jsonl"
    result = materialize_lyric_backfill_queue(unresolved_master_database, output)

    assert result["queue"] == str(output)
    assert result["queue_count"] == 1
    assert len(result["queue_sha256"]) == 64
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["recording_id"] == "rec_neon_night_studio"
    assert row["source_track_id"] == "kugou-123456"


def test_initialize_migrates_v6_database_without_fabricating_lyrics(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE recording_lyric")
        connection.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        assert repository.status()["schema_version"] == SCHEMA_VERSION == 7
        tables = {
            str(row["name"])
            for row in repository.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "recording_lyric" in tables
        assert repository.lyric_coverage()["canonical_recordings"] == 0
