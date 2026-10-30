from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fallback_parallel.py"


def _module():
    spec = importlib.util.spec_from_file_location("fallback_parallel", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _song(identity: str, status: str, path: str | None = None) -> dict:
    payload = {"identity_key": identity, "title": identity, "artist": "Artist"}
    if path is None:
        payload["download"] = {"status": status}
    else:
        payload["download"] = {"status": status, "retention": "retained", "path": path,
                               "file_present": True, "exists": True, "size_bytes": 3,
                               "duration_seconds": 200.0, "source": "NeteaseDirect"}
    return payload


def _shard(tmp_path: Path, index: int, identities: list[str], inventory: dict, work_dir: Path) -> dict:
    shard_dir = tmp_path / "run" / "fallback-shards" / f"shard-{index:02d}"
    progress = {
        "schema_version": 1,
        "run_id": f"run-shard-{index:02d}",
        "started_at": "2026-10-28T00:00:00Z",
        "finished_at": "2026-10-28T00:01:00Z",
        "results": {identity: {"status": "downloaded", "attempt_status": "downloaded"} for identity in identities},
        "summary": {"downloaded": len(identities), "skipped_existing": 0, "failed": 0,
                    "no_results": 0, "abandoned": 0},
    }
    progress_path = shard_dir / "progress.json"
    _write_json(progress_path, progress)
    inventory_path = shard_dir / "inventory.json"
    _write_json(inventory_path, inventory)
    return {
        "index": index,
        "rows": [{"identity_key": identity} for identity in identities],
        "inventory": inventory_path,
        "progress": progress_path,
        "work_dir": work_dir,
    }


def _inventory(tmp_path: Path, songs: list[dict]) -> Path:
    path = tmp_path / "data" / "song_inventory.json"
    _write_json(path, {"schema_version": 1, "songs": songs})
    return path


def test_merge_moves_flat_root_file_into_existing_audio_root(tmp_path: Path) -> None:
    module = _module()
    audio_root = tmp_path / "audio_root"
    audio_root.mkdir()
    (audio_root / "existing.mp3").write_bytes(b"existing")
    shard_work = tmp_path / "shard-01" / "music_downloads"
    shard_work.mkdir(parents=True)
    source = shard_work / "netease-111.mp3"
    source.write_bytes(b"new-audio")
    real_inventory = _inventory(tmp_path, [_song("kugou:1", "missing")])
    shard_inventory = {"songs": [_song("kugou:1", "downloaded", str(source))]}
    shard = _shard(tmp_path, 1, ["kugou:1"], shard_inventory, shard_work)

    import hashlib
    expected = hashlib.sha256(real_inventory.read_bytes()).hexdigest()
    progress = module.merge_isolated_shards(
        shards=[shard],
        inventory_path=real_inventory,
        work_dir=audio_root,
        progress_path=tmp_path / "fallback-progress.json",
        run_id="run",
        started_at="2026-10-28T00:00:00Z",
        expected_inventory_sha256=expected,
    )

    target = audio_root / "netease-111.mp3"
    assert target.is_file() and target.read_bytes() == b"new-audio"
    assert (audio_root / "existing.mp3").read_bytes() == b"existing"
    inventory = json.loads(real_inventory.read_text(encoding="utf-8"))
    song = next(item for item in inventory["songs"] if item["identity_key"] == "kugou:1")
    assert song["download"]["status"] == "downloaded"
    assert song["download"]["path"] == str(target.resolve())
    assert progress["merge"]["root_files_moved"] == 1
    assert not source.exists()


def test_merge_moves_song_directory_files_into_existing_target_directory(tmp_path: Path) -> None:
    module = _module()
    audio_root = tmp_path / "audio_root"
    target_dir = audio_root / "QQMusicClient" / "2026-10-28-22-00 Song"
    target_dir.mkdir(parents=True)
    (target_dir / "keep.txt").write_bytes(b"keep")
    (target_dir / "Song - 123.lrc").write_bytes(b"old-lyric")
    shard_work = tmp_path / "shard-02" / "music_downloads"
    shard_dir = shard_work / "QQMusicClient" / "2026-10-28-22-00 Song"
    shard_dir.mkdir(parents=True)
    source_audio = shard_dir / "Song - 123.mp3"
    source_audio.write_bytes(b"new-audio")
    (shard_dir / "Song - 123.lrc").write_bytes(b"new-lyric")
    real_inventory = _inventory(tmp_path, [_song("kugou:2", "missing")])
    shard_inventory = {"songs": [_song("kugou:2", "downloaded", str(source_audio))]}
    shard = _shard(tmp_path, 2, ["kugou:2"], shard_inventory, shard_work)

    import hashlib
    expected = hashlib.sha256(real_inventory.read_bytes()).hexdigest()
    progress = module.merge_isolated_shards(
        shards=[shard],
        inventory_path=real_inventory,
        work_dir=audio_root,
        progress_path=tmp_path / "fallback-progress.json",
        run_id="run",
        started_at="2026-10-28T00:00:00Z",
        expected_inventory_sha256=expected,
    )

    assert (target_dir / "Song - 123.mp3").read_bytes() == b"new-audio"
    assert (target_dir / "keep.txt").read_bytes() == b"keep"
    assert (target_dir / "Song - 123.lrc").read_bytes() == b"old-lyric"
    assert not (shard_dir / "Song - 123.mp3").exists()
    inventory = json.loads(real_inventory.read_text(encoding="utf-8"))
    song = next(item for item in inventory["songs"] if item["identity_key"] == "kugou:2")
    assert song["download"]["path"] == str((target_dir / "Song - 123.mp3").resolve())
    assert progress["merge"]["directory_merges"] == 1


def test_merge_preserves_existing_media_without_overwrite(tmp_path: Path) -> None:
    module = _module()
    audio_root = tmp_path / "audio_root"
    audio_root.mkdir()
    target = audio_root / "netease-333.mp3"
    target.write_bytes(b"already-there")
    shard_work = tmp_path / "shard-03" / "music_downloads"
    shard_work.mkdir(parents=True)
    source = shard_work / "netease-333.mp3"
    source.write_bytes(b"shard-version")
    real_inventory = _inventory(tmp_path, [_song("kugou:3", "missing")])
    shard_inventory = {"songs": [_song("kugou:3", "downloaded", str(source))]}
    shard = _shard(tmp_path, 3, ["kugou:3"], shard_inventory, shard_work)

    import hashlib
    expected = hashlib.sha256(real_inventory.read_bytes()).hexdigest()
    progress = module.merge_isolated_shards(
        shards=[shard],
        inventory_path=real_inventory,
        work_dir=audio_root,
        progress_path=tmp_path / "fallback-progress.json",
        run_id="run",
        started_at="2026-10-28T00:00:00Z",
        expected_inventory_sha256=expected,
    )

    assert target.read_bytes() == b"already-there"
    assert source.is_file()
    inventory = json.loads(real_inventory.read_text(encoding="utf-8"))
    song = next(item for item in inventory["songs"] if item["identity_key"] == "kugou:3")
    assert song["download"]["status"] == "downloaded"
    assert song["download"]["preserved_existing_media"] is True
    assert song["download"]["path"] == str(target.resolve())
    assert progress["merge"]["preserved_existing_media"] == 1


def test_merge_dry_run_plans_without_touching_files_or_inventory(tmp_path: Path) -> None:
    module = _module()
    audio_root = tmp_path / "audio_root"
    audio_root.mkdir()
    shard_work = tmp_path / "shard-01" / "music_downloads"
    shard_work.mkdir(parents=True)
    source = shard_work / "netease-111.mp3"
    source.write_bytes(b"new-audio")
    real_inventory = _inventory(tmp_path, [_song("kugou:1", "missing")])
    original_inventory = real_inventory.read_text(encoding="utf-8")
    shard_inventory = {"songs": [_song("kugou:1", "downloaded", str(source))]}
    shard = _shard(tmp_path, 1, ["kugou:1"], shard_inventory, shard_work)

    progress_path = tmp_path / "fallback-progress.json"
    import hashlib
    expected = hashlib.sha256(real_inventory.read_bytes()).hexdigest()
    progress = module.merge_isolated_shards(
        shards=[shard],
        inventory_path=real_inventory,
        work_dir=audio_root,
        progress_path=progress_path,
        run_id="run",
        started_at="2026-10-28T00:00:00Z",
        expected_inventory_sha256=expected,
        dry_run=True,
    )

    assert progress["merge"]["dry_run"] is True
    assert progress["merge"]["downloaded_updates"] == 1
    assert source.is_file()
    assert not (audio_root / "netease-111.mp3").exists()
    assert real_inventory.read_text(encoding="utf-8") == original_inventory
    assert not progress_path.exists()
