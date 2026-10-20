from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from music_kb import cli
from music_kb.errors import ReadOnlyError
from music_kb.repository import MusicKBRepository
from music_kb.schema import initialize_database


AUDIENCE_FIXTURES = Path(__file__).parent / "fixtures" / "audience"
SHANFENG = AUDIENCE_FIXTURES / "shanfeng.jsonl"
CHART_EVIDENCE = AUDIENCE_FIXTURES / "chart-evidence.jsonl"
SHANFENG_RECORDING_ID = "rec_shanfeng"
METER_RECORDING_ID = "rec_meter_fixture"


def seed_recording(
    repository: MusicKBRepository,
    *,
    recording_id: str,
    title: str,
    artist: str,
    tags: list[dict] | None = None,
) -> str:
    payload = {
        "recording": {
            "id": recording_id,
            "title": title,
            "audio_sha256": hashlib.sha256(recording_id.encode("utf-8")).hexdigest(),
        },
        "artists": [{"name": artist, "role": "primary"}],
        "analysis": {
            "raw_text": f"Synthetic canonical analysis for {title}.",
            "summary": "",
            "model_version": "fixture-audience-v1",
            "prompt_version": "fixture",
            "quality_state": "passed",
        },
        "source_tracks": [
            {
                "source": "fixture",
                "source_track_id": recording_id,
                "source_title": title,
                "source_artist_credit": artist,
            }
        ],
    }
    if tags:
        payload["tags"] = tags
    repository.import_analysis(payload)
    return recording_id


def fixture_row(path: Path, index: int = 0) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[index])


def chart_row(
    *,
    recording_id: str,
    researched_at: str,
    observations: list[tuple[str, str]],
    rank_id_start: int = 49223,
) -> dict:
    """Build one synthetic chart_evidence record for projection tests."""

    sources = []
    rows = []
    for index, (dimension, bucket) in enumerate(observations):
        rank_id = rank_id_start + index
        source_id = f"src_chart_{rank_id}_{researched_at.replace('-', '')}"
        sources.append(
            {
                "id": source_id,
                "source_platform": "kugou",
                "source_key": f"chart:{rank_id}:{researched_at}",
                "title": f"合成榜单 {rank_id}",
                "source_type": "chart_snapshot",
                "fetched_at": f"{researched_at}T03:10:00+08:00",
                "evidence_level": "1_platform_public",
                "coverage_note": "榜内热度≠人口构成",
                "local_path": f"data/weekly_runs/fixture/{rank_id}.json",
            }
        )
        rows.append(
            {
                "dimension": dimension,
                "bucket": bucket,
                "rank": index + 1,
                "evidence_level": "3_proxy",
                "is_proxy": 1,
                "source_id": source_id,
            }
        )
    return {
        "kind": "chart_evidence",
        "recording": {"recording_id": recording_id},
        "profile": {
            "target_platform": "unspecified",
            "time_window": "chart_snapshot",
            "method_id": "chart_signal_v1",
            "method_version": "chart-hint-v1",
            "status": "available",
            "confidence": "low",
            "is_proxy": 1,
            "researched_at": researched_at,
        },
        "sources": sources,
        "observations": rows,
    }


def research_row(
    *,
    recording_id: str,
    researched_at: str,
    observations: list[tuple[str, str]],
    time_window: str = "full_lifecycle",
    method_version: str = "v1.1",
) -> dict:
    """Build one synthetic research profile record with bare-bucket observations."""

    return {
        "kind": "profile",
        "recording": {"recording_id": recording_id},
        "profile": {
            "target_platform": "douyin",
            "time_window": time_window,
            "window_start": "2026-07-20",
            "window_end": "2026-10-19",
            "method_id": "version-matrix",
            "method_version": method_version,
            "status": "available",
            "confidence": "low",
            "is_proxy": 1,
            "researched_at": researched_at,
        },
        "observations": [
            {"dimension": dimension, "bucket": bucket, "value_low": 0.2, "value_high": 0.3,
             "evidence_level": "4_inferred"}
            for dimension, bucket in observations
        ],
    }


@pytest.fixture()
def tag_database(tmp_path: Path) -> Path:
    database = tmp_path / "music-master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        seed_recording(
            repository,
            recording_id=SHANFENG_RECORDING_ID,
            title="山风山风等等我",
            artist="万海东",
        )
        seed_recording(
            repository,
            recording_id="rec_other_song",
            title="另一首样本",
            artist="另一位歌手",
        )
        seed_recording(
            repository,
            recording_id=METER_RECORDING_ID,
            title="节拍样本",
            artist="测试艺人",
            tags=[{"namespace": "meter", "name": "4/4", "path": "meter/4-4", "status": "approved"}],
        )
    return database


def projected_tags(repository: MusicKBRepository, recording_id: str) -> list[dict]:
    rows = repository.connection.execute(
        """
        SELECT t.namespace, t.canonical_name, t.path, t.lifecycle_status, rt.role
        FROM recording_tag rt JOIN tag t ON t.id = rt.tag_id
        WHERE rt.recording_id = ?
          AND (rt.role IN ('audience', 'audience-hint')
               OR t.namespace IN ('audience', 'audience-hint'))
        ORDER BY t.namespace, t.canonical_name
        """,
        (recording_id,),
    )
    return [dict(row) for row in rows]


def tag_names(repository: MusicKBRepository, recording_id: str) -> set[tuple[str, str]]:
    return {
        (row["namespace"], row["canonical_name"])
        for row in projected_tags(repository, recording_id)
    }


def test_import_projects_alias_tags_and_search_filters(tag_database: Path) -> None:
    with MusicKBRepository(tag_database) as repository:
        stats = repository.import_audience([fixture_row(SHANFENG)])
        assert stats["errors"] == 0
        assert stats["tags"]["recordings"] == 1
        assert stats["tags"]["tags"] == 8
        assert stats["tags"]["namespaces"] == ["audience"]
        assert stats["tags"]["orphan_tags_deleted"] == 0

        expected = {
            ("audience", "age/18-23"),
            ("audience", "age/24-30"),
            ("audience", "age/31-40"),
            ("audience", "age/41-50"),
            ("audience", "age/50+"),
            ("audience", "gender/male"),
            ("audience", "gender/female"),
            ("audience", "city-tier/low"),
        }
        rows = projected_tags(repository, SHANFENG_RECORDING_ID)
        assert {(row["namespace"], row["canonical_name"]) for row in rows} == expected
        assert {row["role"] for row in rows} == {"audience"}
        for row in rows:
            assert row["path"] == row["canonical_name"]
            assert row["lifecycle_status"] == "candidate"
            aliases = [
                str(alias["alias"])
                for alias in repository.connection.execute(
                    """
                    SELECT ta.alias FROM tag_alias ta JOIN tag t ON t.id = ta.tag_id
                    WHERE t.namespace = ? AND t.canonical_name = ? ORDER BY ta.alias
                    """,
                    (row["namespace"], row["canonical_name"]),
                )
            ]
            assert aliases == [f"{row['namespace']}/{row['canonical_name']}"]

        # Projection is recording-level: it must not create analysis tags.
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM analysis_tag at JOIN analysis_revision ar "
            "ON ar.id = at.analysis_id WHERE ar.recording_id = ?",
            (SHANFENG_RECORDING_ID,),
        ).fetchone()[0] == 0

        # Existing --tag behavior is untouched and the public path alias hits.
        assert [item["recording_id"] for item in repository.search(tags=["audience/age/31-40"])] == [
            SHANFENG_RECORDING_ID
        ]
        assert [item["recording_id"] for item in repository.search(tags=["age/31-40"])] == [
            SHANFENG_RECORDING_ID
        ]
        assert [item["recording_id"] for item in repository.search(
            tags=["audience/age/31-40", "audience/gender/male"]
        )] == [SHANFENG_RECORDING_ID]
        assert repository.search(tags=["audience/age/31-40", "audience/gender/unknown"]) == []
        assert repository.search(tags=["audience/city-tier/low"])[0]["recording_id"] == (
            SHANFENG_RECORDING_ID
        )
        assert repository.discover(tags=["audience/age/31-40"])["match_count"] == 1
        recommended = repository.recommend(tags=["audience/age/31-40"], limit=5)
        assert recommended["match_count"] == 1
        assert recommended["results"][0]["recording_id"] == SHANFENG_RECORDING_ID

        # The meter tag still resolves exactly as before (#140 regression).
        assert [item["recording_id"] for item in repository.search(tags=["4/4"])] == [
            METER_RECORDING_ID
        ]
        assert repository.search(tags=["audience/age/31-40", "4/4"]) == []


def test_reimport_replaces_research_tags_without_residue(tag_database: Path) -> None:
    with MusicKBRepository(tag_database) as repository:
        repository.import_audience([fixture_row(SHANFENG)])
        replaced = research_row(
            recording_id=SHANFENG_RECORDING_ID,
            researched_at="2026-10-20",
            observations=[
                ("age", "31-40"),
                ("gender", "female"),
                ("city_tier", "一线/新一线"),
            ],
        )
        stats = repository.import_audience([replaced])
        assert stats["errors"] == 0
        assert tag_names(repository, SHANFENG_RECORDING_ID) == {
            ("audience", "age/31-40"),
            ("audience", "gender/female"),
            ("audience", "city-tier/high"),
        }
        # Old buckets are replaced, not stacked; orphan tag rows are cleaned up.
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM tag WHERE namespace = 'audience'"
        ).fetchone()[0] == 3
        assert repository.search(tags=["audience/age/18-23"]) == []
        assert [item["recording_id"] for item in repository.search(
            tags=["audience/city-tier/high"]
        )] == [SHANFENG_RECORDING_ID]

        # Re-running the same payload is idempotent.
        assert repository.import_audience([replaced])["tags"]["tags"] == 3
        assert len(projected_tags(repository, SHANFENG_RECORDING_ID)) == 3


def test_research_projection_uses_newest_profile_with_window_and_version_tie_breaks(
    tag_database: Path,
) -> None:
    with MusicKBRepository(tag_database) as repository:
        # Same researched_at: full_lifecycle wins over first_wave.
        repository.import_audience(
            [
                research_row(
                    recording_id=SHANFENG_RECORDING_ID,
                    researched_at="2026-10-20",
                    observations=[("gender", "male")],
                    time_window="first_wave",
                ),
                research_row(
                    recording_id=SHANFENG_RECORDING_ID,
                    researched_at="2026-10-20",
                    observations=[("gender", "female")],
                    time_window="full_lifecycle",
                ),
            ]
        )
        assert tag_names(repository, SHANFENG_RECORDING_ID) == {("audience", "gender/female")}

        # A newer researched_at wins even when it is a first_wave profile.
        repository.import_audience(
            [
                research_row(
                    recording_id=SHANFENG_RECORDING_ID,
                    researched_at="2026-10-21",
                    observations=[("gender", "male")],
                    time_window="first_wave",
                )
            ]
        )
        assert tag_names(repository, SHANFENG_RECORDING_ID) == {("audience", "gender/male")}

        # Same date/window: the higher method_version wins.
        repository.import_audience(
            [
                research_row(
                    recording_id=SHANFENG_RECORDING_ID,
                    researched_at="2026-10-22",
                    observations=[("gender", "female")],
                    method_version="v1.1",
                ),
                research_row(
                    recording_id=SHANFENG_RECORDING_ID,
                    researched_at="2026-10-22",
                    observations=[("gender", "male"), ("age", "41-50")],
                    method_version="v1.2",
                ),
            ]
        )
        assert tag_names(repository, SHANFENG_RECORDING_ID) == {
            ("audience", "gender/male"),
            ("audience", "age/41-50"),
        }


def test_chart_hints_only_come_from_the_newest_snapshot(tag_database: Path) -> None:
    older_song = research_row(
        recording_id="rec_other_song",
        researched_at="2026-10-20",
        observations=[("gender", "male")],
    )
    with MusicKBRepository(tag_database) as repository:
        repository.import_audience([older_song])
        # The other song gets a hint from the first snapshot period.
        repository.import_audience(
            [
                chart_row(
                    recording_id="rec_other_song",
                    researched_at="2026-10-20",
                    observations=[("generation_hint", "gen/90后")],
                )
            ],
            kind="chart_evidence",
        )
        assert tag_names(repository, "rec_other_song") == {
            ("audience", "gender/male"),
            ("audience-hint", "gen/90后"),
        }

        # A newer snapshot that does not hit "rec_other_song" clears its hint,
        # while its research tags are untouched.
        chart = chart_row(
            recording_id=SHANFENG_RECORDING_ID,
            researched_at="2026-10-27",
            observations=[
                ("generation_hint", "gen/00后"),
                ("scene", "scene/shortvideo"),
                ("tribe", "tribe/guofeng"),
            ],
        )
        stats = repository.import_audience([chart], kind="chart_evidence")
        assert stats["tags"]["chart_recordings"] == 1
        assert tag_names(repository, "rec_other_song") == {("audience", "gender/male")}
        assert tag_names(repository, SHANFENG_RECORDING_ID) == {
            ("audience-hint", "gen/00后"),
            ("audience-hint", "scene/shortvideo"),
            ("audience-hint", "tribe/guofeng"),
        }
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM tag WHERE namespace = 'audience-hint'"
        ).fetchone()[0] == 3
        assert [item["recording_id"] for item in repository.search(
            tags=["audience-hint/gen/00后"]
        )] == [SHANFENG_RECORDING_ID]
        assert repository.search(tags=["audience-hint/gen/90后"]) == []
        assert [item["recording_id"] for item in repository.search(
            tags=["audience-hint/scene/shortvideo"]
        )] == [SHANFENG_RECORDING_ID]

        # Re-importing the same snapshot does not stack tags.
        repository.import_audience([chart], kind="chart_evidence")
        assert len(projected_tags(repository, SHANFENG_RECORDING_ID)) == 3


def test_projection_normalizes_cross_generation_and_skips_v1_excluded_buckets(
    tag_database: Path,
) -> None:
    with MusicKBRepository(tag_database) as repository:
        repository.import_audience(
            [
                research_row(
                    recording_id=SHANFENG_RECORDING_ID,
                    researched_at="2026-10-20",
                    observations=[
                        ("city_tier", "二线"),
                        # Research-side extra dimensions are not in the v1
                        # projection table, so they stay evidence only.
                        ("life_stage", "学生"),
                        ("scene", "短视频"),
                    ],
                )
            ]
        )
        assert tag_names(repository, SHANFENG_RECORDING_ID) == {("audience", "city-tier/mid")}

        chart = chart_row(
            recording_id=SHANFENG_RECORDING_ID,
            researched_at="2026-10-27",
            observations=[
                ("generation_hint", "cross:00后+90后"),
                # v1 has no KTV chart and no "wide" generation bucket; both are
                # deliberately not projected (spec v2.1 §7).
                ("scene", "scene/ktv"),
                ("generation_hint", "gen/wide"),
                ("tribe", "tribe/opera"),
            ],
        )
        repository.import_audience([chart], kind="chart_evidence")
        assert tag_names(repository, SHANFENG_RECORDING_ID) == {
            ("audience", "city-tier/mid"),
            ("audience-hint", "gen/cross-00-90"),
            ("audience-hint", "tribe/opera"),
        }
        assert repository.search(tags=["audience-hint/gen/cross-00-90"]) != []
        assert repository.search(tags=["audience-hint/scene/ktv"]) == []


def test_get_canonical_analysis_includes_audience_summary(tag_database: Path) -> None:
    with MusicKBRepository(tag_database) as repository:
        repository.import_audience([fixture_row(SHANFENG)])
        repository.import_audience(
            [fixture_row(CHART_EVIDENCE, 1)], kind="chart_evidence"
        )
        canonical = repository.get_canonical_analysis(SHANFENG_RECORDING_ID)
        summary = canonical["audience"]
        assert summary["recorded"] is True
        assert [group["label"] for group in summary["groups"]] == ["调研画像", "榜单证据"]
        research, chart = summary["groups"]
        assert research["profile_count"] == 1
        assert research["observation_count"] == 8
        assert research["latest_researched_at"] == "2026-10-20"
        assert research["time_windows"] == ["full_lifecycle"]
        assert research["dimensions"] == ["age", "city_tier", "gender"]
        assert chart["profile_count"] == 1
        assert chart["latest_researched_at"] == "2026-10-27"
        assert chart["dimensions"] == ["generation_hint"]
        names = {(tag["namespace"], tag["canonical_name"]) for tag in summary["tags"]}
        assert ("audience", "age/31-40") in names
        assert ("audience-hint", "gen/00后") in names
        assert "不代表受众占比" in summary["chart_evidence_note"]

        # A recording with no audience records still returns a stable summary.
        empty = repository.get_canonical_analysis(METER_RECORDING_ID)["audience"]
        assert empty["recorded"] is False
        assert [group["profile_count"] for group in empty["groups"]] == [0, 0]
        assert empty["tags"] == []


def test_cli_search_recommend_discover_accept_audience_tags(
    tag_database: Path, capsys
) -> None:
    with MusicKBRepository(tag_database) as repository:
        repository.import_audience([fixture_row(SHANFENG)])
        repository.import_audience(
            [fixture_row(CHART_EVIDENCE, 1)], kind="chart_evidence"
        )

    def run(*arguments: str) -> dict:
        exit_code = cli.main(["--json", *arguments, "--db", str(tag_database)])
        assert exit_code == 0
        return json.loads(capsys.readouterr().out)["result"]

    search = run("search", "--tag", "audience/age/31-40")
    assert search["count"] == 1
    assert search["results"][0]["recording_id"] == SHANFENG_RECORDING_ID
    hint = run("search", "--tag", "audience-hint/gen/00后")
    assert hint["results"][0]["recording_id"] == SHANFENG_RECORDING_ID
    discover = run("discover", "--tag", "audience/age/31-40")
    assert discover["match_count"] == 1
    recommend = run("recommend", "--tag", "audience-hint/gen/00后")
    assert recommend["match_count"] == 1
    assert recommend["results"][0]["recording_id"] == SHANFENG_RECORDING_ID
    # `--tag 4/4` still resolves the pre-existing meter tag.
    meter = run("search", "--tag", "4/4")
    assert [item["recording_id"] for item in meter["results"]] == [METER_RECORDING_ID]
    untouched = run("search", "--tag", "audience/age/31-40", "--tag", "4/4")
    assert untouched["count"] == 0


def test_projection_requires_a_writable_repository(tag_database: Path) -> None:
    with MusicKBRepository(tag_database, read_only=True) as repository:
        with pytest.raises(ReadOnlyError):
            repository.rebuild_audience_tags()
