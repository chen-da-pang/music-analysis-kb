from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "prune_audio_library.py"
SPEC = importlib.util.spec_from_file_location("prune_audio_library", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_knowledge_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE campaign_delivery_provenance (id INTEGER PRIMARY KEY);
            INSERT INTO campaign_delivery_provenance DEFAULT VALUES;
            CREATE TABLE source_track (
                id TEXT PRIMARY KEY,
                recording_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_track_id TEXT NOT NULL,
                source_url TEXT
            );
            INSERT INTO source_track VALUES (
                'source-1', 'recording-1', 'kugou', 'kugou-1', 'https://www.kugou.com/mixsong/1.html'
            );
            CREATE TABLE recording (
                id TEXT PRIMARY KEY,
                canonical_analysis_id TEXT
            );
            INSERT INTO recording VALUES ('recording-1', 'analysis-1');
            CREATE TABLE recording_lyric (
                recording_id TEXT PRIMARY KEY,
                source_track_row_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO recording_lyric VALUES ('recording-1', 'source-1', 'available');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_prune_deletes_only_analyzed_audio_and_preserves_pending_download(tmp_path: Path) -> None:
    root = tmp_path / "audio"
    analyzed = root / "analyzed" / "track.mp3"
    analyzed.parent.mkdir(parents=True)
    analyzed.write_bytes(b"a" * 1024)
    (analyzed.parent / "track.lrc").write_text("lyrics\n", encoding="utf-8")
    pending = root / "pending" / "track.mp3"
    pending.parent.mkdir(parents=True)
    pending.write_bytes(b"b" * 2048)

    inventory_path = tmp_path / "song_inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "songs": [
                    {
                        "identity_key": "kugou:1",
                        "platform": "kugou",
                        "platform_track_key": "1",
                        "download": {
                            "status": "downloaded",
                            "path": "analyzed/track.mp3",
                            "file_present": True,
                            "exists": True,
                        },
                    },
                    {
                        "identity_key": "kugou:2",
                        "platform": "kugou",
                        "platform_track_key": "2",
                        "download": {
                            "status": "downloaded",
                            "path": "pending/track.mp3",
                            "file_present": True,
                            "exists": True,
                        },
                    },
                    {
                        "identity_key": "kugou:3",
                        "platform": "kugou",
                        "platform_track_key": "3",
                        "download": {
                            "status": "downloaded",
                            "path": "old/track.mp3",
                            "retention": "purged_after_analysis",
                            "file_present": False,
                            "exists": False,
                        },
                    },
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    database = tmp_path / "knowledge.sqlite"
    write_knowledge_db(database)

    dry_run = MODULE.prune(inventory_path, root, database, expected_count=3, confirm=False)
    assert dry_run["eligible_song_count"] == 1
    assert dry_run["retained_song_count"] == 1
    assert dry_run["already_purged_song_count"] == 1
    assert dry_run["selected_directory_count"] == 1
    assert dry_run["file_count"] == 2
    assert analyzed.is_file()
    assert pending.is_file()

    result = MODULE.prune(inventory_path, root, database, expected_count=3, confirm=True)
    assert result["remaining_present_song_count"] == 1
    assert not analyzed.parent.exists()
    assert pending.is_file()

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert len(inventory["songs"]) == 3
    assert inventory["songs"][0]["download"]["retention"] == "purged_after_analysis"
    assert inventory["songs"][0]["download"]["file_present"] is False
    assert inventory["songs"][1]["download"]["file_present"] is True
    assert inventory["audio_retention"] == "partial_purge_after_analysis"


def test_prune_refuses_when_any_canonical_lyric_is_missing_or_pending(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.sqlite"
    write_knowledge_db(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM recording_lyric")
        connection.commit()

    with pytest.raises(RuntimeError, match="歌词覆盖不完整"):
        MODULE.validate_release(database)
