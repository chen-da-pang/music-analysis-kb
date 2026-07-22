from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from music_kb.errors import ValidationError
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
