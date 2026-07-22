from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "download_music_queue.py"


def _module():
    spec = importlib.util.spec_from_file_location("download_music_queue", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_fake_musicdl(monkeypatch, client_type: type) -> None:
    package = types.ModuleType("musicdl")
    module = types.ModuleType("musicdl.musicdl")
    module.MusicClient = client_type
    package.musicdl = module
    monkeypatch.setitem(sys.modules, "musicdl", package)
    monkeypatch.setitem(sys.modules, "musicdl.musicdl", module)


def _queue(tmp_path: Path, *, title: str, artist: str) -> tuple[Path, Path, Path, Path, Path]:
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps(
            {
                "identity_key": "kugou:1",
                "title_artist_key": f"kugou:{title}\u0000{artist}",
                "platform": "kugou",
                "platform_track_key": "1",
                "title": title,
                "artist": artist,
                "play_link": "https://www.kugou.com/mixsong/example.html",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"songs": []}), encoding="utf-8")
    return queue, inventory, tmp_path / "music", tmp_path / "progress.json", tmp_path / "download.log"


def test_choose_match_rejects_wrong_versions_and_artist() -> None:
    module = _module()
    wrong_live = SimpleNamespace(song_name="空心 (DJ版)", singers="黄霄雲、刘端端")
    wrong_arrangement = SimpleNamespace(song_name="心似烟火 (DJ Leo朔版)", singers="陈壹千")
    wrong_artist = SimpleNamespace(song_name="空心 (Live)", singers="其他歌手")

    assert module.choose_match([wrong_live], "空心 (Live)", "黄霄雲、刘端端") is None
    assert module.choose_match([wrong_arrangement], "心似烟火", "陈壹千") is None
    assert module.choose_match([wrong_artist], "空心 (Live)", "黄霄雲、刘端端") is None


def test_choose_match_accepts_nfkc_spacing_and_artist_separators() -> None:
    module = _module()
    exact = SimpleNamespace(song_name="空心 ( Live )", singers="黄霄雲, 刘端端")

    assert module.choose_match([exact], "空心（Live）", "黄霄雲、刘端端") is exact
    assert module.choose_match([SimpleNamespace(song_name="空心 (Live)", singers="NULL")], "空心 (Live)", "NULL") is None


def test_incompatible_search_results_become_auditable_no_results(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def search(self, _query: str):
            return {
                "KugouMusicClient": [
                    SimpleNamespace(source="KugouMusicClient", song_name="空心 (DJ版)", singers="黄霄雲、刘端端")
                ]
            }

        def download(self, _items):
            raise AssertionError("incompatible result must not be downloaded")

    _install_fake_musicdl(monkeypatch, FakeClient)
    queue, inventory, work_dir, progress, log = _queue(tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端")
    summary = module.run_download(queue, inventory, work_dir, progress, log, "test-run", None, False, 0, 0, 10, 5)

    assert summary["downloaded"] == 0
    assert summary["no_results"] == 1
    saved_inventory = json.loads(inventory.read_text(encoding="utf-8"))
    download = saved_inventory["songs"][0]["download"]
    assert download["status"] == "no_results"
    assert download["reason"] == "no_exact_platform_identity_title_artist_match"
    assert download["match_policy"] == module.MATCH_POLICY
    assert download["rejected_candidates"] == [{"title": "空心 (DJ版)", "artist": "黄霄雲、刘端端"}]


def test_exact_match_records_selected_result_metadata(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class FakeClient:
        def __init__(self, **kwargs):
            self.work_dir = Path(kwargs["init_music_clients_cfg"]["KugouMusicClient"]["work_dir"])
            self.exact = SimpleNamespace(
                source="KugouMusicClient",
                song_name="空心 (Live)",
                singers="黄霄雲, 刘端端",
                identifier="exact-file-hash",
                lyric="[00:01.00]第一句\n[00:02.00]第二句\n",
                raw_data={
                    "search": {"MixSongID": "1"},
                    "lyric": {"candidates": [{"id": "fixture"}]},
                },
            )

        def search(self, _query: str):
            return {
                "KugouMusicClient": [
                    SimpleNamespace(source="KugouMusicClient", song_name="空心 (DJ版)", singers="黄霄雲、刘端端"),
                    self.exact,
                ]
            }

        def download(self, items):
            assert items == [self.exact]
            path = self.work_dir / "KugouMusicClient" / "exact.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"exact")
            return [
                SimpleNamespace(
                    save_path=str(path),
                    source=self.exact.source,
                    identifier=self.exact.identifier,
                    lyric=self.exact.lyric,
                    raw_data=self.exact.raw_data,
                )
            ]

    _install_fake_musicdl(monkeypatch, FakeClient)
    queue, inventory, work_dir, progress, log = _queue(tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端")
    summary = module.run_download(queue, inventory, work_dir, progress, log, "test-run", None, False, 0, 0, 10, 5)

    assert summary["downloaded"] == 1
    saved_inventory = json.loads(inventory.read_text(encoding="utf-8"))
    download = saved_inventory["songs"][0]["download"]
    assert download["status"] == "downloaded"
    assert download["matched_title"] == "空心 (Live)"
    assert download["matched_artist"] == "黄霄雲, 刘端端"
    assert download["matched_kugou_track_keys"] == ["1"]
    assert download["match_policy"] == module.MATCH_POLICY
    receipt = json.loads((tmp_path / "lyrics-receipts.jsonl").read_text(encoding="utf-8"))
    assert receipt["status"] == "available"
    assert receipt["source_track_id"] == "kugou-1"
    assert receipt["lyric_text"] == "第一句\n第二句"


def test_exact_title_artist_without_matching_mix_song_id_is_rejected(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def search(self, _query: str):
            return {
                "KugouMusicClient": [
                    SimpleNamespace(
                        source="KugouMusicClient",
                        song_name="空心 (Live)",
                        singers="黄霄雲、刘端端",
                        raw_data={"search": {"MixSongID": "different-track"}},
                    )
                ]
            }

        def download(self, _items):
            raise AssertionError("a different mix-song ID must never be downloaded")

    _install_fake_musicdl(monkeypatch, FakeClient)
    queue, inventory, work_dir, progress, log = _queue(tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端")
    summary = module.run_download(queue, inventory, work_dir, progress, log, "test-run", None, False, 0, 0, 10, 5)

    assert summary["no_results"] == 1
    saved = json.loads(inventory.read_text(encoding="utf-8"))
    assert saved["songs"][0]["download"]["reason"] == "no_exact_platform_identity_title_artist_match"


def test_lyrics_only_worker_writes_pending_or_available_receipt_without_audio(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def search(self, _query: str):
            return {
                "KugouMusicClient": [
                    SimpleNamespace(
                        source="KugouMusicClient",
                        song_name="空心 (Live)",
                        singers="黄霄雲、刘端端",
                        identifier="fixture-hash",
                        lyric="[ti:空心]\n[00:01.00]只取歌词\n",
                        raw_data={
                            "search": {"MixSongID": "1"},
                            "lyric": {"candidates": [{"id": "fixture"}]},
                        },
                    )
                ]
            }

    _install_fake_musicdl(monkeypatch, FakeClient)
    queue, _inventory, work_dir, progress, log = _queue(tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端")
    receipt_path = tmp_path / "backfill-lyrics.jsonl"
    summary = module.run_lyrics_only(
        queue,
        work_dir,
        progress,
        log,
        "lyrics-only",
        None,
        False,
        0,
        0,
        10,
        5,
        receipt_path,
    )

    assert summary["available"] == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "available"
    assert receipt["lyric_text"] == "只取歌词"
    assert not (work_dir / "KugouMusicClient").exists()
