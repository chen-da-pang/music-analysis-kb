from __future__ import annotations

import importlib.util
import hashlib
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


def write_delivery(path: Path, *, audio: bytes) -> dict[str, object]:
    source_sha256 = hashlib.sha256(audio).hexdigest()
    output_text = "Synthetic canonical analysis."
    row: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": "fixture-campaign",
        "id": "kugou-1",
        "manifest_index": 0,
        "title": "Fixture song",
        "artist": "Fixture artist",
        "relative_audio_path": "audio/track.mp3",
        "source_sha256": source_sha256,
        "source_bytes": len(audio),
        "source_url": "https://www.kugou.com/mixsong/fixture.html",
        "output_text": output_text,
        "output_text_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        "generated_token_count": 1,
        "max_new_tokens": 1,
        "contract": "fixture-contract",
        "attempt_id": "fixture-attempt",
        "canonical_source": "fixture",
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    return row


def write_campaign_staging(staging: Path, *, delivery: dict[str, object], source: Path) -> Path:
    audio_path = staging / "audio" / "track.mp3"
    audio_path.parent.mkdir(parents=True)
    audio_path.hardlink_to(source)
    manifest = {
        "campaign_id": delivery["campaign_id"],
        "id": delivery["id"],
        "relative_audio_path": delivery["relative_audio_path"],
        "sha256": delivery["source_sha256"],
        "source_bytes": delivery["source_bytes"],
        "source_url": delivery["source_url"],
        "title": delivery["title"],
        "artist": delivery["artist"],
    }
    (staging / "manifest.jsonl").write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    return audio_path


def write_one_song_inventory(path: Path) -> None:
    path.write_text(
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
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


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


def test_prune_removes_exact_delivery_bound_staging_after_release(tmp_path: Path) -> None:
    root = tmp_path / "audio"
    source = root / "analyzed" / "track.mp3"
    source.parent.mkdir(parents=True)
    audio = b"fixture-audio" * 128
    source.write_bytes(audio)
    inventory_path = tmp_path / "song_inventory.json"
    write_one_song_inventory(inventory_path)
    database = tmp_path / "knowledge.sqlite"
    write_knowledge_db(database)
    delivery_path = tmp_path / "canonical.jsonl"
    delivery = write_delivery(delivery_path, audio=audio)
    input_staging = tmp_path / "run" / "cnb-input"
    campaign_staging = tmp_path / "run" / "campaign-repository" / "data" / "input" / "fixture-campaign"
    input_audio = write_campaign_staging(input_staging, delivery=delivery, source=source)
    campaign_audio = write_campaign_staging(campaign_staging, delivery=delivery, source=source)
    (input_staging / "ledger.jsonl").write_text("evidence\n", encoding="utf-8")

    dry_run = MODULE.prune(
        inventory_path,
        root,
        database,
        expected_count=1,
        confirm=False,
        delivery_path=delivery_path,
        campaign_staging_paths=[input_staging, campaign_staging],
    )
    assert dry_run["delivery_staging"]["status"] == "ready"
    assert dry_run["delivery_staging"]["staging_directory_count"] == 2
    assert dry_run["delivery_staging"]["file_count"] == 2
    assert source.is_file()
    assert input_audio.is_file()
    assert campaign_audio.is_file()

    result = MODULE.prune(
        inventory_path,
        root,
        database,
        expected_count=1,
        confirm=True,
        delivery_path=delivery_path,
        campaign_staging_paths=[input_staging, campaign_staging],
    )
    assert result["delivery_staging"]["status"] == "removed"
    assert result["delivery_staging"]["removed_file_count"] == 2
    assert not source.exists()
    assert not (input_staging / "audio").exists()
    assert not (campaign_staging / "audio").exists()
    assert (input_staging / "manifest.jsonl").is_file()
    assert (input_staging / "ledger.jsonl").is_file()
    assert (campaign_staging / "manifest.jsonl").is_file()

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert inventory["songs"][0]["download"]["retention"] == "purged_after_analysis"


def test_prune_refuses_mismatched_delivery_bound_staging_before_deleting_audio(tmp_path: Path) -> None:
    root = tmp_path / "audio"
    source = root / "analyzed" / "track.mp3"
    source.parent.mkdir(parents=True)
    audio = b"fixture-audio" * 128
    source.write_bytes(audio)
    inventory_path = tmp_path / "song_inventory.json"
    write_one_song_inventory(inventory_path)
    database = tmp_path / "knowledge.sqlite"
    write_knowledge_db(database)
    delivery_path = tmp_path / "canonical.jsonl"
    delivery = write_delivery(delivery_path, audio=audio)
    staging = tmp_path / "run" / "cnb-input"
    staged_audio = write_campaign_staging(staging, delivery=delivery, source=source)
    manifest = json.loads((staging / "manifest.jsonl").read_text(encoding="utf-8"))
    manifest["source_url"] = "https://www.kugou.com/mixsong/unexpected.html"
    (staging / "manifest.jsonl").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest与canonical delivery不一致"):
        MODULE.prune(
            inventory_path,
            root,
            database,
            expected_count=1,
            confirm=True,
            delivery_path=delivery_path,
            campaign_staging_paths=[staging],
        )

    assert source.is_file()
    assert staged_audio.is_file()
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert inventory["songs"][0]["download"].get("retention") is None


def test_prune_refuses_delivery_without_source_url_before_deleting_audio(tmp_path: Path) -> None:
    root = tmp_path / "audio"
    source = root / "analyzed" / "track.mp3"
    source.parent.mkdir(parents=True)
    audio = b"fixture-audio" * 128
    source.write_bytes(audio)
    inventory_path = tmp_path / "song_inventory.json"
    write_one_song_inventory(inventory_path)
    database = tmp_path / "knowledge.sqlite"
    write_knowledge_db(database)
    delivery_path = tmp_path / "canonical.jsonl"
    delivery = write_delivery(delivery_path, audio=audio)
    delivery["source_url"] = ""
    delivery_path.write_text(json.dumps(delivery, ensure_ascii=False) + "\n", encoding="utf-8")
    staging = tmp_path / "run" / "cnb-input"
    staged_audio = write_campaign_staging(staging, delivery=delivery, source=source)

    with pytest.raises(RuntimeError, match="canonical delivery line 1.source_url"):
        MODULE.prune(
            inventory_path,
            root,
            database,
            expected_count=1,
            confirm=True,
            delivery_path=delivery_path,
            campaign_staging_paths=[staging],
        )

    assert source.is_file()
    assert staged_audio.is_file()
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert inventory["songs"][0]["download"].get("retention") is None


def test_prune_refuses_when_any_canonical_lyric_is_missing_or_pending(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.sqlite"
    write_knowledge_db(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM recording_lyric")
        connection.commit()

    with pytest.raises(RuntimeError, match="歌词覆盖不完整"):
        MODULE.validate_release(database)
