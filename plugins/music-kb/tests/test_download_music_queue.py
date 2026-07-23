from __future__ import annotations

import base64
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


def _direct_kugou_responses(mix_song_id: str = "1") -> tuple[bytes, bytes, bytes]:
    page = (
        "<script>var dataFromSmarty = "
        + json.dumps(
            [
                {
                    "mixsongid": mix_song_id,
                    "hash": "DIRECT-HASH",
                    "timelength": 123000,
                    "audio_name": "黄霄雲、刘端端 - 空心 (Live)",
                    "song_name": "空心 (Live)",
                    "artist_name": "黄霄雲、刘端端",
                }
            ],
            ensure_ascii=False,
        )
        + ",// 当前页面歌曲信息</script>"
    ).encode("utf-8")
    search = json.dumps(
        {
            "status": 200,
            "candidates": [
                {
                    "id": "candidate-1",
                    "accesskey": "access-key",
                    "duration": 123000,
                    "song": "空心 (Live)",
                    "singer": "黄霄雲、刘端端",
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    lyric = base64.b64encode("[ti:空心]\r\n[00:01.00]精确页面歌词\r\n".encode("utf-8")).decode("ascii")
    download = json.dumps({"status": 200, "content": lyric}, ensure_ascii=False).encode("utf-8")
    return page, search, download


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


def test_download_worker_repairs_pending_musicdl_lyric_via_exact_page(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class FakeClient:
        def __init__(self, **kwargs):
            self.work_dir = Path(kwargs["init_music_clients_cfg"]["KugouMusicClient"]["work_dir"])
            self.exact = SimpleNamespace(
                source="KugouMusicClient",
                song_name="空心 (Live)",
                singers="黄霄雲、刘端端",
                identifier="musicdl-file-hash",
                lyric="<script>window.location='/error'</script>获取失败",
                raw_data={
                    "search": {"MixSongID": "1"},
                    "lyric": {"candidates": [{"id": "fixture"}]},
                },
            )

        def search(self, _query: str):
            return {"KugouMusicClient": [self.exact]}

        def download(self, _items):
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
    queue, inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/future-download.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row["play_link"] = source_url
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    page, search, download = _direct_kugou_responses()

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        if url == source_url:
            return page
        if url.startswith("https://lyrics.kugou.com/search?"):
            return search
        if url.startswith("https://lyrics.kugou.com/download?"):
            return download
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    summary = module.run_download(
        queue,
        inventory,
        work_dir,
        progress,
        log,
        "future-download-lyrics",
        None,
        False,
        0,
        0,
        10,
        5,
    )

    assert summary["downloaded"] == 1
    assert summary["lyrics_available"] == 1
    receipt = json.loads((tmp_path / "lyrics-receipts.jsonl").read_text(encoding="utf-8"))
    assert receipt["lyric_text"] == "精确页面歌词"
    assert receipt["evidence"]["query_method"] == module.DIRECT_LYRIC_QUERY_METHOD


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


def test_lyrics_only_worker_uses_exact_mixsong_page_without_musicdl(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    queue, _inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/exact.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row["source_url"] = source_url
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    page, search, download = _direct_kugou_responses()
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        calls.append(url)
        if url == source_url:
            return page
        if url.startswith("https://lyrics.kugou.com/search?"):
            return search
        if url.startswith("https://lyrics.kugou.com/download?"):
            return download
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    receipt_path = tmp_path / "backfill-lyrics.jsonl"
    summary = module.run_lyrics_only(
        queue,
        work_dir,
        progress,
        log,
        "lyrics-only-direct",
        None,
        False,
        0,
        0,
        10,
        5,
        receipt_path,
    )

    assert summary["available"] == 1
    assert len(calls) == 3
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "available"
    assert receipt["lyric_text"] == "精确页面歌词"
    assert receipt["evidence"]["query_method"] == module.DIRECT_LYRIC_QUERY_METHOD
    assert receipt["evidence"]["page_kugou_mix_song_id"] == "1"
    assert receipt["evidence"]["page_file_hash"] == "DIRECT-HASH"
    assert not (work_dir / "KugouMusicClient").exists()


def test_exact_mixsong_zero_lyric_candidates_is_platform_unavailable(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    queue, _inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/no-lyric.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row["source_url"] = source_url
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    page, _search, _download = _direct_kugou_responses()

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        if url == source_url:
            return page
        if url.startswith("https://lyrics.kugou.com/search?"):
            return b'{"status":200,"candidates":[]}'
        raise AssertionError(f"a zero-candidate response must not download lyrics: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    receipt_path = tmp_path / "backfill-lyrics.jsonl"
    summary = module.run_lyrics_only(
        queue,
        work_dir,
        progress,
        log,
        "lyrics-only-zero-candidates",
        None,
        False,
        0,
        0,
        10,
        5,
        receipt_path,
    )

    assert summary["platform_unavailable"] == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "platform_unavailable"
    assert receipt["evidence"]["response_kind"] == "platform_zero_lyric_candidates"
    assert receipt["evidence"]["query_method"] == module.DIRECT_LYRIC_QUERY_METHOD


def test_exact_source_url_can_prove_a_singleton_page_with_zero_mixsong_id(
    monkeypatch, tmp_path: Path
) -> None:
    module = _module()
    queue, _inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/zero-id.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row["source_url"] = source_url
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    page, search, download = _direct_kugou_responses("0")

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        if url == source_url:
            return page
        if url.startswith("https://lyrics.kugou.com/search?"):
            return search
        if url.startswith("https://lyrics.kugou.com/download?"):
            return download
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    receipt_path = tmp_path / "backfill-lyrics.jsonl"
    summary = module.run_lyrics_only(
        queue,
        work_dir,
        progress,
        log,
        "lyrics-only-zero-page-id",
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
    assert receipt["evidence"]["page_kugou_mix_song_id"] == "0"
    assert (
        receipt["evidence"]["identity_verification"]
        == "queue_exact_source_url_page_id_unavailable_v1"
    )


def test_nonzero_page_mixsong_id_mismatch_stays_pending(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    queue, _inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/wrong-id.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row["source_url"] = source_url
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    page, _search, _download = _direct_kugou_responses("999")

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        if url == source_url:
            return page
        raise AssertionError(f"a nonzero identity mismatch must stop before lyrics: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    receipt_path = tmp_path / "backfill-lyrics.jsonl"
    summary = module.run_lyrics_only(
        queue,
        work_dir,
        progress,
        log,
        "lyrics-only-wrong-page-id",
        None,
        False,
        0,
        0,
        10,
        5,
        receipt_path,
    )

    assert summary["pending"] == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["evidence"]["response_kind"] == "direct_identity_mismatch"
    assert receipt["evidence"]["page_returned_mix_song_ids"] == ["999"]


def test_lyrics_only_worker_rejects_html_failure_payload(monkeypatch, tmp_path: Path) -> None:
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
                        lyric="<script>window.location='/error'</script>获取失败",
                        raw_data={
                            "search": {"MixSongID": "1"},
                            "lyric": {"candidates": [{"id": "fixture"}]},
                        },
                    )
                ]
            }

    _install_fake_musicdl(monkeypatch, FakeClient)
    queue, _inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
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

    assert summary["pending"] == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "pending"
    assert receipt["evidence"]["response_kind"] == "invalid_lyric_payload"
    assert "lyric_text" not in receipt


def _empty_mixsong_page() -> bytes:
    return (
        '<script>var dataFromSmarty = [{"hash":null,"author_name":"","song_name":"",'
        '"audio_name":"","album_id":0,"timelength":0,"mixsongid":0}],'
        "// 当前页面歌曲信息</script>"
    ).encode("utf-8")


def test_lyrics_only_worker_uses_verified_archived_hash_after_empty_page(
    monkeypatch, tmp_path: Path
) -> None:
    module = _module()
    queue, _inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/archived-hash.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row.update(
        {
            "source_url": source_url,
            "archived_kugou_file_hash": "A" * 32,
            "archived_kugou_file_hash_provenance": {
                "method": "song_inventory_download_path_exact_identity_v1",
                "inventory_identity_key": "kugou:1",
                "download_status": "downloaded",
                "download_retention": "purged_after_analysis",
                "inventory_relative_audio_path": "old/fixture - " + "A" * 32 + ".mp3",
            },
        }
    )
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    _page, search, download = _direct_kugou_responses()

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        if url == source_url:
            return _empty_mixsong_page()
        if url.startswith("https://lyrics.kugou.com/search?"):
            assert "duration=-1" in url
            assert "hash=" + "A" * 32 in url
            return search
        if url.startswith("https://lyrics.kugou.com/download?"):
            return download
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    receipt_path = tmp_path / "backfill-lyrics.jsonl"
    summary = module.run_lyrics_only(
        queue,
        work_dir,
        progress,
        log,
        "lyrics-only-archived-hash",
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
    assert receipt["lyric_text"] == "精确页面歌词"
    assert receipt["evidence"]["query_method"] == module.DIRECT_HASH_LYRIC_QUERY_METHOD
    assert (
        receipt["evidence"]["identity_verification"]
        == "queue_exact_mixsong_inventory_download_hash_v1"
    )
    assert receipt["evidence"]["prior_direct_response_kind"] == "direct_missing_platform_hash"


def test_lyrics_only_worker_rejects_an_archived_hash_without_exact_inventory_identity(
    monkeypatch, tmp_path: Path
) -> None:
    module = _module()
    queue, _inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/bad-archived-hash.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row.update(
        {
            "source_url": source_url,
            "archived_kugou_file_hash": "A" * 32,
            "archived_kugou_file_hash_provenance": {
                "method": "song_inventory_download_path_exact_identity_v1",
                "inventory_identity_key": "kugou:999",
                "download_status": "downloaded",
            },
        }
    )
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        if url == source_url:
            return _empty_mixsong_page()
        raise AssertionError(f"unproven archived hash must not query lyrics: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    receipt_path = tmp_path / "backfill-lyrics.jsonl"
    summary = module.run_lyrics_only(
        queue,
        work_dir,
        progress,
        log,
        "lyrics-only-bad-archived-hash",
        None,
        False,
        0,
        0,
        10,
        5,
        receipt_path,
    )

    assert summary["pending"] == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["evidence"]["response_kind"] == "direct_missing_platform_hash"


def test_download_worker_uses_exact_musicdl_hash_when_page_is_empty(monkeypatch, tmp_path: Path) -> None:
    module = _module()

    class FakeClient:
        def __init__(self, **kwargs):
            self.work_dir = Path(kwargs["init_music_clients_cfg"]["KugouMusicClient"]["work_dir"])
            self.exact = SimpleNamespace(
                source="KugouMusicClient",
                song_name="空心 (Live)",
                singers="黄霄雲、刘端端",
                identifier="B" * 32,
                lyric="<html>missing</html>",
                raw_data={"search": {"MixSongID": "1"}, "lyric": {"candidates": []}},
            )

        def search(self, _query: str):
            return {"KugouMusicClient": [self.exact]}

        def download(self, _items):
            path = self.work_dir / "KugouMusicClient" / "exact.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"exact")
            return [
                SimpleNamespace(
                    save_path=str(path),
                    source=self.exact.source,
                    # A downloaded object with a different ID cannot prove its
                    # hash belongs to the selected MixSongID. The fallback must
                    # retain the exact-ID validated search result's hash.
                    identifier="C" * 32,
                    lyric=self.exact.lyric,
                    raw_data={"search": {"MixSongID": "different"}, "lyric": {"candidates": []}},
                )
            ]

    _install_fake_musicdl(monkeypatch, FakeClient)
    queue, inventory, work_dir, progress, log = _queue(
        tmp_path, title="空心 (Live)", artist="黄霄雲、刘端端"
    )
    source_url = "https://www.kugou.com/mixsong/agent_gateway/future-empty.html"
    row = json.loads(queue.read_text(encoding="utf-8"))
    row["source_url"] = source_url
    queue.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    _page, search, download = _direct_kugou_responses()

    def fake_fetch(url: str, *, timeout: float) -> bytes:
        assert timeout == 5
        if url == source_url:
            return _empty_mixsong_page()
        if url.startswith("https://lyrics.kugou.com/search?"):
            assert module.signal.getitimer(module.signal.ITIMER_REAL)[0] > 0
            assert "hash=" + "B" * 32 in url
            assert "hash=" + "C" * 32 not in url
            return search
        if url.startswith("https://lyrics.kugou.com/download?"):
            return download
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(module, "_fetch_url", fake_fetch)
    summary = module.run_download(
        queue,
        inventory,
        work_dir,
        progress,
        log,
        "future-download-hash-lyrics",
        None,
        False,
        0,
        0,
        10,
        5,
    )

    assert summary["downloaded"] == 1
    assert summary["lyrics_available"] == 1
    receipt = json.loads((tmp_path / "lyrics-receipts.jsonl").read_text(encoding="utf-8"))
    assert receipt["lyric_text"] == "精确页面歌词"
    assert receipt["evidence"]["query_method"] == module.DIRECT_HASH_LYRIC_QUERY_METHOD
    assert receipt["evidence"]["identity_verification"] == "musicdl_exact_mixsong_id_file_hash_v1"
    assert receipt["evidence"]["musicdl_hash_identity_source"] == "musicdl_search_result_exact_mixsong_id_v1"
