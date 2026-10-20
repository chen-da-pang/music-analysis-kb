from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from music_kb.errors import ValidationError
from music_kb.operation_context import load_validated_operations
from music_kb.repository import MusicKBRepository
from music_kb.schema import initialize_database

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from capture_kugou_audience_charts import capture_audience_charts

from music_kb.audience_charts import (
    CHART_BUCKETS,
    build_chart_evidence_records,
    chart_evidence_confidence,
    import_audience_inputs,
    load_audience_chart_profile,
    load_chart_capture,
    match_chart_entries,
    precaptured_audience_chart_summary,
)


FIXTURE_ANALYSIS = Path(__file__).parent / "fixtures" / "analysis.json"
REFERENCE_PROFILE = Path(__file__).parents[1] / "references" / "kugou-audience-charts-v1.json"
OPERATIONS = Path(__file__).parents[1] / "references" / "validated-operations.json"

# Verbatim copy of the #141 "24 榜官方简介" table; any edit here means the
# versioned policy no longer matches the ticket and the test must fail.
EXPECTED_CHARTS: dict[str, dict[str, str]] = {
    "49224": {
        "chart_name": "00后热歌榜",
        "family": "generation",
        "update_frequency": "每天",
        "official_intro": "排序方式：按年龄段的喜爱热度高低排序 更新频率：每天",
        "capture": "own",
        "dimension": "generation_hint",
        "bucket": "gen/00后",
    },
    "49223": {
        "chart_name": "90后热歌榜",
        "family": "generation",
        "update_frequency": "每天",
        "official_intro": "排序方式：按年龄段的喜爱热度高低排序 更新频率：每天",
        "capture": "own",
        "dimension": "generation_hint",
        "bucket": "gen/90后",
    },
    "49225": {
        "chart_name": "80后热歌榜",
        "family": "generation",
        "update_frequency": "每天",
        "official_intro": "排序方式：按年龄段的喜爱热度高低排序 更新频率：每天",
        "capture": "own",
        "dimension": "generation_hint",
        "bucket": "gen/80后",
    },
    "52144": {
        "chart_name": "短视频热歌榜",
        "family": "scene",
        "update_frequency": "周一",
        "official_intro": "数据来源：短视频热门歌曲 排序方式：按照歌曲在酷狗的播放热度排序 更新频率：每周一",
        "capture": "reuse",
        "dimension": "scene",
        "bucket": "scene/shortvideo",
    },
    "52767": {
        "chart_name": "短视频收藏人气榜",
        "family": "scene",
        "update_frequency": "每天",
        "official_intro": "数据来源：短视频平台歌曲 排序方式：按照歌曲在酷狗的喜爱用户数排序 更新频率：每天",
        "capture": "own",
        "dimension": "scene",
        "bucket": "scene/shortvideo",
    },
    "24971": {
        "chart_name": "DJ热歌榜",
        "family": "scene",
        "update_frequency": "周四",
        "official_intro": "数据来源：DJ类歌曲 排序方式：按歌曲喜爱用户数的总量排序 更新频率：每周四",
        "capture": "own",
        "dimension": "scene",
        "bucket": "scene/dj",
    },
    "46910": {
        "chart_name": "综艺新歌榜",
        "family": "scene",
        "update_frequency": "周三",
        "official_intro": "数据来源：60天内发行的综艺新歌 排序方式：按歌曲热度、歌手等级和发行时间综合评分排序 更新频率：每周三",
        "capture": "own",
        "dimension": "scene",
        "bucket": "scene/variety",
    },
    "33163": {
        "chart_name": "影视金曲榜",
        "family": "scene",
        "update_frequency": "周三",
        "official_intro": "数据来源：影视歌曲 排序方式：影视歌曲偏好用户中，按歌曲日均喜爱用户数排序 更新频率：每周三",
        "capture": "own",
        "dimension": "scene",
        "bucket": "scene/soundtrack",
    },
    "37361": {
        "chart_name": "酷狗识曲榜",
        "family": "scene",
        "update_frequency": "工作日",
        "official_intro": "数据来源：酷狗音乐听歌识曲 排序方式：按近一周识曲用户数排序 更新频率：每周一至周五",
        "capture": "own",
        "dimension": "scene",
        "bucket": "scene/recognition",
    },
    "100530": {
        "chart_name": "视频号热歌酷狗榜",
        "family": "scene",
        "update_frequency": "每天",
        "official_intro": "数据来源：全曲库歌曲 排序方式：按酷狗、视频号双平台热度排序 更新频率：每天",
        "capture": "own",
        "dimension": "scene",
        "bucket": "scene/channels",
    },
    "33162": {
        "chart_name": "ACG新歌榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：180天内发行的ACG新歌 排序方式：按歌曲热度、歌手等级和发行时间综合评分排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/acg",
    },
    "33160": {
        "chart_name": "电音榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：电音歌曲 排序方式：电音歌曲偏好用户中，按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/electronic",
    },
    "44412": {
        "chart_name": "说唱先锋榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：说唱歌曲 排序方式：说唱歌曲偏好用户中，按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/rap",
    },
    "51341": {
        "chart_name": "民谣榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：民谣歌曲 排序方式：民谣歌曲偏好用户中，按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/folk",
    },
    "59896": {
        "chart_name": "摇滚榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：摇滚歌曲 排序方式：摇滚歌曲偏好用户中，按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/rock",
    },
    "59895": {
        "chart_name": "R&B榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：R&B歌曲 排序方式：R&B歌曲偏好用户中，按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/rnb",
    },
    "59897": {
        "chart_name": "爵士榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：爵士歌曲 排序方式：按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/jazz",
    },
    "59898": {
        "chart_name": "乡村音乐榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：乡村音乐歌曲 排序方式：按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/country",
    },
    "59899": {
        "chart_name": "古典榜",
        "family": "tribe",
        "update_frequency": "周四",
        "official_intro": "数据来源：古典歌曲 排序方式：按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/classical",
    },
    "85897": {
        "chart_name": "国潮音乐榜",
        "family": "tribe",
        "update_frequency": "周五凌晨更新周榜",
        "official_intro": "酷狗音乐结合平台特色，通过播放、传播、互动等维度，结合歌曲自身播放热度等综合评估推出的全新音乐榜单",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/guofeng",
    },
    "80025": {
        "chart_name": "国乐榜",
        "family": "tribe",
        "update_frequency": "周四更新",
        "official_intro": "数据来源：国乐歌曲 排序方式：按歌曲日均喜爱用户数排序 更新频率：每周四",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/guofeng",
    },
    "108332": {
        "chart_name": "粤剧榜",
        "family": "tribe",
        "update_frequency": "每周二更新",
        "official_intro": "数据来源：粤剧音乐 排序方式：按歌曲播放量排序 更新频率：每周二",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/opera",
    },
    "108333": {
        "chart_name": "京剧榜",
        "family": "tribe",
        "update_frequency": "每周二更新",
        "official_intro": "数据来源：京剧音乐 排序方式：按歌曲播放量排序 更新频率：每周二",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/opera",
    },
    "108335": {
        "chart_name": "南音榜",
        "family": "tribe",
        "update_frequency": "每周二更新",
        "official_intro": "数据来源：南音音乐 排序方式：按歌曲播放量排序 更新频率：每周二",
        "capture": "own",
        "dimension": "tribe",
        "bucket": "tribe/opera",
    },
}


def _analysis_payload(
    *, recording_id: str, title: str, artist: str, source_track_key: str
) -> dict:
    payload = json.loads(FIXTURE_ANALYSIS.read_text(encoding="utf-8"))
    payload["recording"] = {
        "id": recording_id,
        "title": title,
        "version_label": "",
        "audio_sha256": hashlib.sha256(recording_id.encode("utf-8")).hexdigest(),
    }
    payload["artists"] = [{"name": artist, "aliases": [], "role": "primary"}]
    payload["title_aliases"] = []
    payload["analysis"]["raw_text"] = f"Synthetic analysis for {title} / {artist} ({recording_id})."
    payload["source_tracks"] = [
        {
            "source": "kugou",
            "source_track_id": f"kugou-{source_track_key}",
            "source_title": title,
            "source_artist_credit": artist,
            "source_url": f"https://www.kugou.com/mixsong/{source_track_key}.html",
        }
    ]
    return payload


def _seed_library(database: Path) -> Path:
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_analysis(json.loads(FIXTURE_ANALYSIS.read_text(encoding="utf-8")))
        repository.import_analysis(
            _analysis_payload(
                recording_id="rec_mix_match",
                title="原名甲",
                artist="歌手甲",
                source_track_key="111",
            )
        )
        repository.import_analysis(
            _analysis_payload(
                recording_id="rec_exact_title",
                title="晴天",
                artist="周杰伦",
                source_track_key="222",
            )
        )
        repository.import_analysis(
            _analysis_payload(
                recording_id="rec_base_title",
                title="慢品人间烟火气",
                artist="涂一乐",
                source_track_key="333",
            )
        )
    return database


def _write_chart_capture(
    directory: Path,
    rank_id: str,
    songs: list[dict],
    *,
    captured_at: str = "2026-10-20T10:40:21+08:00",
    page: int = 1,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"chart-{rank_id}-p{page}-songs.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platform": "kugou",
                "run_id": "fixture-run",
                "summary": {
                    "run_id": "fixture-run",
                    "rank_id": rank_id,
                    "page": page,
                    "size": 100,
                    "rank_start": (page - 1) * 100 + 1,
                    "source_records": len(songs),
                    "captured_at": captured_at,
                },
                "songs": songs,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _chart_song(
    title: str,
    artist: str,
    *,
    track_key: str | None,
    rank: int,
) -> dict:
    return {
        "identity_key": f"kugou:{track_key}" if track_key else title,
        "platform": "kugou",
        "platform_track_key": track_key,
        "title": title,
        "artist": artist,
        "title_artist_key": f"kugou:{title}\x00{artist}",
        "play_link": None,
        "chart_appearances": [{"rank": rank}],
    }


def _profile_jsonl(path: Path) -> Path:
    record = {
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
                "source_key": "https://example.test/report",
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
    }
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# -- versioned policy -------------------------------------------------------


def test_versioned_policy_has_24_charts_with_verbatim_ticket_data() -> None:
    profile = load_audience_chart_profile(REFERENCE_PROFILE)
    assert len(profile["charts"]) == 24
    assert profile["family_counts"] == {"generation": 3, "scene": 7, "tribe": 14}
    charts = {chart["rank_id"]: chart for chart in profile["charts"]}
    assert set(charts) == set(EXPECTED_CHARTS)
    for rank_id, expected in EXPECTED_CHARTS.items():
        chart = charts[rank_id]
        for field, value in expected.items():
            assert chart[field] == value, (rank_id, field)
    assert charts["52144"]["capture"] == "reuse"
    assert sum(1 for chart in charts.values() if chart["capture"] == "reuse") == 1
    assert set(CHART_BUCKETS) == set(EXPECTED_CHARTS)


def test_policy_rejects_family_count_and_bucket_drift(tmp_path: Path) -> None:
    original = json.loads(REFERENCE_PROFILE.read_text(encoding="utf-8"))
    drifted = deepcopy(original)
    drifted["charts"][0]["family"] = "scene"
    drifted["charts"][0]["dimension"] = "scene"
    path = tmp_path / "drifted.json"
    path.write_text(json.dumps(drifted), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_audience_chart_profile(path)

    missing = deepcopy(original)
    missing["charts"] = missing["charts"][:-1]
    missing["chart_count"] = len(missing["charts"])
    path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_audience_chart_profile(path)

    bad_bucket = deepcopy(original)
    bad_bucket["charts"][0]["bucket"] = "gen/10后"
    path.write_text(json.dumps(bad_bucket), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_audience_chart_profile(path)


# -- matching ---------------------------------------------------------------


def test_match_prefers_mix_song_id_then_exact_then_base_title(tmp_path: Path) -> None:
    database = _seed_library(tmp_path / "master.sqlite")
    entries = [
        {
            "rank_id": "49224",
            "chart_name": "00后热歌榜",
            "rank": 1,
            "title": "完全不同标题",
            "artist": "不同歌手",
            "platform_track_key": "111",
        },
        {
            "rank_id": "49224",
            "chart_name": "00后热歌榜",
            "rank": 2,
            "title": "晴天",
            "artist": "周杰伦",
            "platform_track_key": "9002",
        },
        {
            "rank_id": "49224",
            "chart_name": "00后热歌榜",
            "rank": 3,
            "title": "慢品人间烟火气 (微醺版)",
            "artist": "涂一乐",
            "platform_track_key": "9003",
        },
        {
            "rank_id": "49224",
            "chart_name": "00后热歌榜",
            "rank": 4,
            "title": "晴天",
            "artist": "别的歌手",
            "platform_track_key": "9444",
        },
        {
            "rank_id": "49224",
            "chart_name": "00后热歌榜",
            "rank": 5,
            "title": "完全不存在",
            "artist": "某人",
            "platform_track_key": "9555",
        },
    ]
    with MusicKBRepository(database, read_only=True) as repository:
        matched = match_chart_entries(repository.connection, entries)
    stats = matched["stats"]
    assert stats == {
        "entries": 5,
        "matched_entries": 3,
        "matched_recordings": 3,
        "ambiguous": 1,
        "unmatched": 1,
        "methods": {
            "base_title+artist": 1,
            "exact_title+artist": 1,
            "mix_song_id": 1,
        },
    }
    methods = {(match["title"], match["recording_ids"][0]) for match in matched["matches"]}
    assert ("完全不同标题", "rec_mix_match") in methods
    assert ("晴天", "rec_exact_title") in methods
    assert ("慢品人间烟火气 (微醺版)", "rec_base_title") in methods
    assert matched["ambiguous"][0]["title"] == "晴天"
    assert matched["ambiguous"][0]["candidate_recordings"] == ["rec_exact_title"]
    assert matched["ambiguous"][0]["reason"] == "title matched but artist did not"
    assert matched["unmatched"][0]["title"] == "完全不存在"


# -- chart evidence records -------------------------------------------------


def _chart_meta(rank_id: str) -> dict:
    profile = load_audience_chart_profile(REFERENCE_PROFILE)
    chart = next(chart for chart in profile["charts"] if chart["rank_id"] == rank_id)
    return {
        **chart,
        "files": [f"/tmp/chart-{rank_id}-p1-songs.json"],
        "captured_at": "2026-10-20T10:40:21+08:00",
    }


def test_build_chart_evidence_keeps_best_rank_and_confidence_rule() -> None:
    charts = {
        "49224": _chart_meta("49224"),
        "49223": _chart_meta("49223"),
        "52144": _chart_meta("52144"),
    }
    matches = [
        {"rank_id": "49224", "rank": 9, "recording_ids": ["rec_a"]},
        {"rank_id": "49224", "rank": 4, "recording_ids": ["rec_a"]},
        {"rank_id": "49223", "rank": 12, "recording_ids": ["rec_a"]},
    ]
    records = build_chart_evidence_records(matches, charts=charts, snapshot_date="2026-10-20")
    assert len(records) == 1
    record = records[0]
    assert record["recording"] == {"recording_id": "rec_a"}
    assert record["profile"]["method_id"] == "chart_signal_v1"
    assert record["profile"]["method_version"] == "chart-hint-v1"
    assert record["profile"]["time_window"] == "chart_snapshot"
    assert record["profile"]["target_platform"] == "unspecified"
    assert record["profile"]["researched_at"] == "2026-10-20"
    assert record["profile"]["confidence"] == "medium"
    assert record["profile"]["is_proxy"] == 1
    # A chart hit is evidence, not a coverage gap: the hit count lives in
    # basis_note, and coverage_note is reserved for genuinely missing evidence.
    assert "coverage_note" not in record["profile"]
    assert record["profile"]["basis_note"] == "单期快照命中 2 榜（榜单证据，非受众占比）"
    assert {source["source_key"] for source in record["sources"]} == {
        "chart:49223:2026-10-20",
        "chart:49224:2026-10-20",
    }
    assert all(source["source_type"] == "chart_snapshot" for source in record["sources"])
    assert all(source["evidence_level"] == "1_platform_public" for source in record["sources"])
    assert all(source["coverage_note"] == "榜内热度≠人口构成" for source in record["sources"])
    observations = record["observations"]
    assert observations[0]["bucket"] == "gen/90后"
    assert observations[0]["rank"] == 12
    assert observations[1]["bucket"] == "gen/00后"
    assert observations[1]["rank"] == 4
    assert all(item["evidence_level"] == "3_proxy" for item in observations)


def test_chart_evidence_confidence_spec_v1() -> None:
    generation = {"family": "generation"}
    scene = {"family": "scene"}
    assert chart_evidence_confidence([generation]) == "low"
    assert chart_evidence_confidence([generation, generation]) == "medium"
    assert chart_evidence_confidence([scene, scene, generation]) == "medium"
    assert chart_evidence_confidence([generation, scene]) == "medium"
    assert chart_evidence_confidence([generation, generation, scene]) == "high"
    assert chart_evidence_confidence([scene, scene, generation, generation]) == "high"


# -- capture ----------------------------------------------------------------


def test_load_chart_capture_reuses_52144_from_charts_directory(tmp_path: Path) -> None:
    audience_dir = tmp_path / "audience-charts"
    charts_dir = tmp_path / "charts"
    _write_chart_capture(
        audience_dir,
        "49224",
        [_chart_song("晴天", "周杰伦", track_key="111", rank=1)],
    )
    _write_chart_capture(
        charts_dir,
        "52144",
        [_chart_song("晴天", "周杰伦", track_key="111", rank=7)],
    )
    capture = load_chart_capture(
        chart_directory=audience_dir,
        reuse_directory=charts_dir,
        rank_ids=["49224", "52144"],
    )
    assert capture["missing"] == []
    assert capture["reused"] == ["52144"]
    assert capture["charts"]["52144"]["reused"] is True
    ranks = {(entry["rank_id"], entry["rank"]) for entry in capture["entries"]}
    assert ranks == {("49224", 1), ("52144", 7)}
    assert capture["entries"][0]["dimension"] == "generation_hint"
    assert capture["entries"][0]["bucket"] == "gen/00后"


def _capture_payload(pairs: list[tuple[str, str, str]]) -> str:
    return json.dumps(
        {
            "errcode": 0,
            "data": {
                "list": [
                    {"song_name": title, "artist_name": artist, "mix_song_id": track}
                    for title, artist, track in pairs
                ]
            },
        },
        ensure_ascii=False,
    )


def test_capture_audience_charts_paginates_and_writes_manifest(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        page = int(command[command.index("--page") + 1])
        if page == 1:
            payload = _capture_payload(
                [("晴天", "周杰伦", "111"), ("稻香", "周杰伦", "222")]
            )
        else:
            payload = _capture_payload([("七里香", "周杰伦", "333")])
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    output_dir = tmp_path / "audience-charts"
    result = capture_audience_charts(
        run_id="fixture-run",
        output_dir=output_dir,
        rank_ids=["49224"],
        page_size=2,
        max_pages=3,
        snapshot_date="2026-10-20",
        runner=runner,
    )
    assert result["chart_count"] == 1
    assert result["captured_charts"] == 1
    assert result["total_source_records"] == 3
    assert result["snapshot_date"] == "2026-10-20"
    assert [int(command[command.index("--page") + 1]) for command in calls] == [1, 2]
    assert (output_dir / "chart-49224-p1-songs.json").is_file()
    assert (output_dir / "chart-49224-p2-songs.json").is_file()
    assert not list(output_dir.glob("download_queue*"))
    assert not list(output_dir.glob("chart-songs.json"))
    manifest = json.loads((output_dir / "audience-charts-manifest.json").read_text(encoding="utf-8"))
    assert manifest["chart_count"] == 1
    assert manifest["charts"][0]["page_count"] == 2
    assert manifest["charts"][0]["source_records"] == 3


def test_capture_audience_charts_reuses_existing_52144_without_capture(tmp_path: Path) -> None:
    charts_dir = tmp_path / "charts"
    _write_chart_capture(
        charts_dir,
        "52144",
        [_chart_song("晴天", "周杰伦", track_key="111", rank=7)],
    )

    def runner(command, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError(f"reused chart must not be captured: {command}")

    result = capture_audience_charts(
        run_id="fixture-run",
        output_dir=tmp_path / "audience-charts",
        reuse_directory=charts_dir,
        rank_ids=["52144"],
        snapshot_date="2026-10-20",
        runner=runner,
    )
    assert result["captured_charts"] == 0
    assert result["reused_charts"] == ["52144"]
    assert result["total_source_records"] == 1


# -- import orchestration ---------------------------------------------------


def test_import_audience_inputs_imports_chart_evidence_and_profiles(tmp_path: Path) -> None:
    database = _seed_library(tmp_path / "master.sqlite")
    chart_dir = tmp_path / "audience-charts"
    _write_chart_capture(
        chart_dir,
        "49224",
        [
            _chart_song("霓虹夜航", "示例乐队", track_key="900", rank=3),
            _chart_song("霓虹夜航", "未知人", track_key="901", rank=9),
            _chart_song("完全不存在", "某人", track_key="902", rank=5),
        ],
    )
    _write_chart_capture(
        chart_dir,
        "49223",
        [_chart_song("霓虹夜航", "示例乐队", track_key="900", rank=2)],
    )
    profiles = _profile_jsonl(tmp_path / "profiles.jsonl")

    result = import_audience_inputs(
        database=database,
        chart_directory=chart_dir,
        audience_profiles=profiles,
    )
    assert result["status"] == "succeeded"
    chart_evidence = result["chart_evidence"]
    assert chart_evidence["status"] == "imported"
    assert chart_evidence["charts"] == 2
    assert chart_evidence["entries"] == 4
    assert chart_evidence["matched_entries"] == 2
    assert chart_evidence["matched_recordings"] == 1
    assert chart_evidence["ambiguous"] == 1
    assert chart_evidence["unmatched"] == 1
    assert chart_evidence["snapshot_date"] == "2026-10-20"
    assert chart_evidence["import"]["profiles"] == 1
    assert chart_evidence["import"]["observations"] == 2
    assert chart_evidence["import"]["sources"] == 2
    assert result["profiles"]["import"]["profiles"] == 1
    review_queue = json.loads(
        Path(chart_evidence["review_queue"]).read_text(encoding="utf-8")
    )
    assert review_queue["counts"]["ambiguous"] == 1
    assert review_queue["ambiguous"][0]["title"] == "霓虹夜航"

    with MusicKBRepository(database, read_only=True) as repository:
        audience = repository.get_audience("rec_neon_night_studio")
        groups = {group["group"]: group for group in audience["groups"]}
        chart_group = groups["chart_evidence"]
        research_group = groups["research"]
        assert len(chart_group["profiles"]) == 1
        assert chart_group["profiles"][0]["method_id"] == "chart_signal_v1"
        assert chart_group["profiles"][0]["confidence"] == "medium"
        # Matched chart evidence must not surface as a coverage gap; the hit
        # count is visible in basis_note instead (review finding #2).
        assert chart_group["coverage_gaps"] == []
        assert "命中 2 榜" in chart_group["profiles"][0]["basis_note"]
        assert len(chart_group["observations"]) == 2
        assert {observation["bucket"] for observation in chart_group["observations"]} == {
            "gen/00后",
            "gen/90后",
        }
        assert len(chart_group["sources"]) == 2
        assert len(research_group["profiles"]) == 1
        assert len(research_group["observations"]) == 1

    second = import_audience_inputs(
        database=database,
        chart_directory=chart_dir,
        audience_profiles=profiles,
    )
    assert second["chart_evidence"]["import"]["duplicates"] >= 1
    with MusicKBRepository(database, read_only=True) as repository:
        sources = repository.connection.execute(
            "SELECT COUNT(*) FROM audience_source"
        ).fetchone()[0]
        observations = repository.connection.execute(
            "SELECT COUNT(*) FROM audience_observation"
        ).fetchone()[0]
    assert sources == 3
    assert observations == 3


def test_import_audience_inputs_records_missing_inputs_as_coverage_gaps(tmp_path: Path) -> None:
    database = _seed_library(tmp_path / "master.sqlite")
    result = import_audience_inputs(
        database=database,
        chart_directory=tmp_path / "missing-audience-charts",
        audience_profiles=tmp_path / "missing-profiles.jsonl",
    )
    assert result["status"] == "skipped"
    assert result["chart_evidence"]["status"] == "skipped"
    assert result["profiles"]["status"] == "skipped"
    reasons = {gap["source"]: gap["reason"] for gap in result["coverage_gaps"]}
    assert "no audience chart capture directory" in reasons["chart_evidence"]
    assert "does not exist" in reasons["research_profiles"]
    with MusicKBRepository(database, read_only=True) as repository:
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM audience_profile"
        ).fetchone()[0] == 0


def test_import_audience_inputs_reports_partial_capture_as_coverage_gap(tmp_path: Path) -> None:
    database = _seed_library(tmp_path / "master.sqlite")
    chart_dir = tmp_path / "audience-charts"
    _write_chart_capture(
        chart_dir,
        "49224",
        [_chart_song("霓虹夜航", "示例乐队", track_key="900", rank=3)],
    )
    _write_chart_capture(
        chart_dir,
        "49223",
        [_chart_song("霓虹夜航", "示例乐队", track_key="900", rank=8)],
    )
    result = import_audience_inputs(database=database, chart_directory=chart_dir)
    # The two present charts still import; the other 22 are an explicit,
    # auditable coverage gap rather than a silent omission.
    assert result["chart_evidence"]["status"] == "imported"
    gap = next(gap for gap in result["coverage_gaps"] if gap["source"] == "chart_evidence")
    assert "partial" in gap["reason"]
    assert len(gap["missing_rank_ids"]) == 22
    assert "49225" in gap["missing_rank_ids"]
    assert "49224" not in gap["missing_rank_ids"]


def test_import_audience_inputs_delivery_resume_imports_profiles_only(tmp_path: Path) -> None:
    database = _seed_library(tmp_path / "master.sqlite")
    chart_dir = tmp_path / "audience-charts"
    _write_chart_capture(
        chart_dir,
        "49224",
        [_chart_song("霓虹夜航", "示例乐队", track_key="900", rank=3)],
    )
    profiles = _profile_jsonl(tmp_path / "profiles.jsonl")
    result = import_audience_inputs(
        database=database,
        chart_directory=chart_dir,
        audience_profiles=profiles,
        delivery_resume=True,
    )
    assert result["status"] == "succeeded"
    assert result["chart_evidence"]["status"] == "skipped"
    assert "delivery resume" in result["chart_evidence"]["reason"]
    assert result["profiles"]["import"]["profiles"] == 1
    with MusicKBRepository(database, read_only=True) as repository:
        chart_profiles = repository.connection.execute(
            "SELECT COUNT(*) FROM audience_profile WHERE method_id='chart_signal_v1'"
        ).fetchone()[0]
    assert chart_profiles == 0


# -- pre-captured reuse ------------------------------------------------------


def test_precaptured_summary_reuses_manifest_directory(tmp_path: Path) -> None:
    chart_dir = tmp_path / "audience-charts"
    chart_dir.mkdir()
    manifest = chart_dir / "audience-charts-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_date": "2026-10-20",
                "chart_count": 24,
                "captured_charts": 23,
                "reused_charts": ["52144"],
                "total_source_records": 2403,
                "charts": [],
            }
        ),
        encoding="utf-8",
    )
    summary = precaptured_audience_chart_summary(
        chart_dir, chart_profile=REFERENCE_PROFILE
    )
    assert summary is not None
    assert summary["status"] == "reused"
    assert "manifest reused" in summary["reason"]
    assert summary["manifest"] == str(manifest)
    assert summary["snapshot_date"] == "2026-10-20"
    assert summary["chart_count"] == 24
    assert summary["reused_charts"] == ["52144"]
    assert summary["total_source_records"] == 2403


def test_precaptured_summary_reuses_complete_page_directory_without_manifest(
    tmp_path: Path,
) -> None:
    chart_dir = tmp_path / "audience-charts"
    profile = load_audience_chart_profile(REFERENCE_PROFILE)
    for chart in profile["charts"]:
        _write_chart_capture(
            chart_dir,
            str(chart["rank_id"]),
            [_chart_song(f"歌{chart['rank_id']}", "歌手", track_key=None, rank=1)],
        )
    summary = precaptured_audience_chart_summary(
        chart_dir, chart_profile=REFERENCE_PROFILE
    )
    assert summary is not None
    assert summary["status"] == "reused"
    assert summary["manifest"] is None
    assert summary["chart_count"] == len(profile["charts"])
    assert summary["snapshot_date"] == "2026-10-20"
    assert summary["total_source_records"] == len(profile["charts"])


def test_precaptured_summary_ignores_missing_partial_and_unreadable_directories(
    tmp_path: Path,
) -> None:
    assert precaptured_audience_chart_summary(None) is None
    assert precaptured_audience_chart_summary(tmp_path / "absent") is None

    partial = tmp_path / "partial"
    _write_chart_capture(
        partial,
        "49224",
        [_chart_song("晴天", "周杰伦", track_key="111", rank=1)],
    )
    assert precaptured_audience_chart_summary(partial, chart_profile=REFERENCE_PROFILE) is None

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "audience-charts-manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="pre-captured audience chart manifest"):
        precaptured_audience_chart_summary(malformed, chart_profile=REFERENCE_PROFILE)


# -- operation registry -----------------------------------------------------


def test_validated_operations_register_both_audience_atoms() -> None:
    registry = json.loads(OPERATIONS.read_text(encoding="utf-8"))
    for atom in ("audience_chart_capture", "audience_import"):
        assert atom in registry["operations"]
        assert registry["operations"][atom]["effective_method"].strip()
        load_validated_operations(OPERATIONS, required_atom=atom)
    rules = "\n".join(registry["rules"])
    assert "run_dir/audience-charts" in rules
    assert "不输出任何受众占比" in rules
