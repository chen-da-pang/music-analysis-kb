from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from capture_kugou_chart import capture_chart, normalize_payload


def test_normalize_payload_deduplicates_platform_id_and_title_artist() -> None:
    payload = {
        "errcode": 0,
        "data": {
            "list": [
                {"song_name": "晴天", "artist_name": "周杰伦", "mix_song_id": "1", "play_link": "https://a"},
                {"song_name": "晴天", "artist_name": "周杰伦", "mix_song_id": "1", "play_link": "https://a"},
                {"song_name": " 稻香 ", "artist_name": "周杰伦", "play_link": "https://b"},
                {"song_name": "稻香", "artist_name": "周杰伦", "play_link": "https://b"},
            ]
        },
    }
    songs, counts = normalize_payload(payload)
    assert len(songs) == 2
    assert counts == {
        "source_records": 4,
        "source_unique_records": 2,
        "duplicate_source_records": 2,
        "invalid_source_records": 0,
    }
    assert songs[0]["identity_key"] == "kugou:1"
    assert len(songs[0]["chart_appearances"]) == 2


def test_normalize_payload_preserves_absolute_rank_for_later_pages() -> None:
    payload = {
        "errcode": 0,
        "data": {
            "list": [
                {"song_name": "第二页歌曲", "artist_name": "歌手", "mix_song_id": "2"},
            ]
        },
    }
    songs, counts = normalize_payload(payload, rank_start=101)
    assert counts["source_records"] == 1
    assert songs[0]["chart_appearances"] == [{"rank": 101}]


def test_capture_chart_writes_raw_normalized_and_manifest(tmp_path: Path) -> None:
    payload = {"errcode": 0, "data": {"list": [{"song_name": "晴天", "artist_name": "周杰伦", "mix_song_id": "1"}]}}

    def runner(command, **kwargs):
        assert command[:5] == ["kugou-cli", "--no-update-check", "music", "charts", "6666"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    result = capture_chart(
        run_id="test-run",
        rank_id="6666",
        page=1,
        size=1,
        output_dir=tmp_path,
        runner=runner,
    )
    assert result["source_unique_records"] == 1
    assert json.loads(Path(result["raw"]).read_text(encoding="utf-8"))["errcode"] == 0
    songs = json.loads(Path(result["songs"]).read_text(encoding="utf-8"))
    assert songs["songs"][0]["play_link"] is None
