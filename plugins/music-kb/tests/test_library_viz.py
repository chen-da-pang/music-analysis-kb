from __future__ import annotations

import builtins
import importlib.util
import sqlite3
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "runners/library-viz/build_viz.py"
VIEWER = SCRIPT.parent / "viewer.html"


@pytest.fixture()
def viz(monkeypatch):
    original_import = builtins.__import__
    forbidden = {"torch", "torchaudio", "transformers", "muq", "merit", "mlx", "umap", "hdbscan"}

    def model_free_import(name, *args, **kwargs):
        if name.split(".")[0] in forbidden:
            raise AssertionError(f"pure-logic test attempted a heavy import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", model_free_import)
    spec = importlib.util.spec_from_file_location("library_viz_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_top_neighbors_orders_by_cosine_distance(viz):
    vectors = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [-1.0, 0.0]])
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    neighbors = viz.top_neighbors(vectors, 2)
    assert [index for index, _ in neighbors[0]] == [1, 2]
    assert neighbors[0][0][1] < neighbors[0][1][1]
    assert all(index != 0 for index, _ in neighbors[0])


def test_audio_target_name_preserves_extension(viz):
    assert viz.audio_target_name(7, ".FLAC") == "0007.flac"
    assert viz.audio_target_name(434, ".ogg") == "0434.ogg"


def test_normalize_2d_centers_and_scales(viz):
    points = np.array([[10.0, 20.0], [30.0, 40.0], [20.0, 30.0]])
    normalized = viz.normalize_2d(points)
    assert np.allclose(normalized.mean(axis=0), 0.0)
    assert np.isclose(np.abs(normalized).max(), 1.0)


def test_cluster_payload_orders_by_size_and_colors(viz):
    labels = np.array([5, 5, 5, 2, 2, -1])
    clusters = viz.cluster_payload(labels)
    assert clusters[0]["id"] == 5 and clusters[0]["size"] == 3
    assert clusters[1]["id"] == 2 and clusters[1]["size"] == 2
    assert clusters[2]["id"] == -1 and clusters[2]["name"] == "散点"
    assert len({cluster["color"] for cluster in clusters}) == 3


def _audience_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE audience_profile (
            id TEXT PRIMARY KEY, recording_id TEXT NOT NULL, target_platform TEXT NOT NULL,
            time_window TEXT NOT NULL, method_id TEXT NOT NULL, method_version TEXT NOT NULL,
            status TEXT NOT NULL, confidence TEXT NOT NULL, is_proxy INTEGER NOT NULL,
            researched_at TEXT NOT NULL, window_start TEXT, window_end TEXT,
            coverage_note TEXT
        );
        CREATE TABLE audience_observation (
            id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, dimension TEXT NOT NULL,
            bucket TEXT NOT NULL, value_low REAL, value_high REAL, value_mid REAL,
            rank INTEGER, evidence_level TEXT NOT NULL, source_id TEXT
        );
        CREATE TABLE audience_source (
            id TEXT PRIMARY KEY, source_platform TEXT NOT NULL, source_key TEXT NOT NULL,
            url TEXT, title TEXT, source_type TEXT NOT NULL, evidence_level TEXT NOT NULL,
            coverage_note TEXT, local_path TEXT
        );
        CREATE TABLE tag (
            id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, canonical_name TEXT NOT NULL,
            path TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE recording_tag (
            recording_id TEXT NOT NULL, tag_id INTEGER NOT NULL, role TEXT NOT NULL,
            PRIMARY KEY (recording_id, tag_id, role)
        );
        """
    )
    profiles = [
        ("p_a_research_old", "rec_a", "douyin", "full_lifecycle", "version-matrix", "v1.1",
         "available", "low", 1, "2026-10-19"),
        ("p_a_research_first", "rec_a", "douyin", "first_wave", "version-matrix", "v1.1",
         "available", "low", 1, "2026-10-21"),
        ("p_a_research_latest", "rec_a", "douyin", "full_lifecycle", "version-matrix", "v1.2",
         "available", "medium", 1, "2026-10-21"),
        ("p_a_chart_old", "rec_a", "unspecified", "chart_snapshot", "chart_signal_v1",
         "chart-hint-v1", "available", "low", 1, "2026-10-20"),
        ("p_a_chart_new", "rec_a", "unspecified", "chart_snapshot", "chart_signal_v1",
         "chart-hint-v1", "available", "medium", 1, "2026-10-27"),
        ("p_b_research", "rec_b", "douyin", "full_lifecycle", "version-matrix", "v1.1",
         "available", "low", 1, "2026-10-20"),
    ]
    connection.executemany(
        "INSERT INTO audience_profile VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
        [(*row, None if not row[0].startswith("p_b") else "no report; inferred only")
         for row in profiles],
    )
    connection.executemany(
        "INSERT INTO audience_source VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("s1", "kugou", "kugou-search:song:fixture:2026-10-19", "https://example.test/a",
             "酷狗同名检索", "kugou_search", "1_platform_public", "公开检索页", "/local/secret/a.json"),
            ("s2", "kugou", "chart:49224:2026-10-27", "https://example.test/chart",
             "酷狗 00后热歌榜", "chart_snapshot", "1_platform_public", "榜内热度≠人口构成",
             "/local/secret/chart.json"),
        ],
    )
    observations = [
        ("o1", "p_a_research_old", "age", "31-40", 0.3, 0.34, 0.32, None, "4_inferred", "s1"),
        ("o2", "p_a_research_first", "gender", "female", None, None, None, None, "4_inferred", "s1"),
        ("o3", "p_a_research_latest", "gender", "male", 0.54, 0.58, 0.56, None, "4_inferred", "s1"),
        ("o4", "p_a_chart_old", "generation_hint", "gen/90后", None, None, None, 3, "3_proxy", "s2"),
        ("o5", "p_a_chart_new", "generation_hint", "gen/00后", None, None, None, 8, "3_proxy", "s2"),
        ("o6", "p_b_research", "city_tier", "high", 0.4, 0.5, 0.45, None, "4_inferred", None),
    ]
    connection.executemany(
        "INSERT INTO audience_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", observations
    )
    connection.executemany(
        "INSERT INTO tag VALUES (?, ?, ?, ?)",
        [
            (1, "audience", "gender/male", "gender/male"),
            (2, "audience-hint", "gen/00后", "gen/00后"),
            (3, "audience", "city-tier/high", "city-tier/high"),
            (4, "title", "不应出现的标题标签", ""),
        ],
    )
    connection.executemany(
        "INSERT INTO recording_tag VALUES (?, ?, 'audience')",
        [("rec_a", 1), ("rec_a", 2), ("rec_b", 3), ("rec_a", 4)],
    )
    return connection


def test_load_audience_reads_newest_profiles_sources_and_tags(viz):
    connection = _audience_connection()
    try:
        audience = viz.load_audience(connection, {"rec_b", "rec_a", "rec_missing"})
    finally:
        connection.close()

    assert set(audience) == {"rec_a", "rec_b"}
    entry = audience["rec_a"]
    # Newest researched_at wins; full_lifecycle wins the same-date tie and the
    # higher method_version wins after that.
    assert entry["research"]["researched_at"] == "2026-10-21"
    assert entry["research"]["time_window"] == "full_lifecycle"
    assert entry["research"]["method_version"] == "v1.2"
    assert entry["research"]["items"] == [
        {
            "dimension": "gender",
            "label": "性别",
            "bucket": "male",
            "value_low": 0.54,
            "value_high": 0.58,
            "value_mid": 0.56,
            "rank": None,
            "evidence_level": "4_inferred",
        }
    ]
    assert entry["research"]["sources"] == [
        {
            "source_platform": "kugou",
            "source_key": "kugou-search:song:fixture:2026-10-19",
            "url": "https://example.test/a",
            "title": "酷狗同名检索",
            "source_type": "kugou_search",
            "evidence_level": "1_platform_public",
            "coverage_note": "公开检索页",
        }
    ]
    assert "local_path" not in entry["research"]["sources"][0]

    # The newest chart snapshot is selected; path-style buckets are displayed bare.
    assert entry["chart"]["researched_at"] == "2026-10-27"
    assert entry["chart"]["status"] == "available"
    assert [item["bucket"] for item in entry["chart"]["items"]] == ["00后"]
    assert [item["rank"] for item in entry["chart"]["items"]] == [8]
    assert entry["chart"]["sources"][0]["title"] == "酷狗 00后热歌榜"
    assert [tag["key"] for tag in entry["tags"]] == [
        "audience/gender/male",
        "audience-hint/gen/00后",
    ]
    assert "不代表受众占比" in entry["note"]

    # A research-only recording has no chart group; identity tags are not shipped.
    assert audience["rec_b"]["chart"] is None
    assert audience["rec_b"]["research"]["items"][0]["bucket"] == "high"
    assert [tag["key"] for tag in audience["rec_b"]["tags"]] == ["audience/city-tier/high"]
    assert viz.load_audience(connection=None, recording_ids=[]) == {}


def test_viewer_renders_audience_between_lyrics_and_neighbors():
    html = VIEWER.read_text(encoding="utf-8")
    assert "function audienceBlock(audience)" in html
    lyric_position = html.index("song.lyric ?")
    audience_position = html.index("audienceBlock(song.audience)")
    neighbor_position = html.index("最近 5 首</div>")
    assert lyric_position < audience_position < neighbor_position
    assert "不代表受众占比" in html
    assert "调研画像" in html and "榜单证据" in html
    assert "证据" in html or "evidence" in html
