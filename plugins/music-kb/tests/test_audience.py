from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from music_kb import cli
from music_kb.errors import NotFoundError, ValidationError
from music_kb.repository import MusicKBRepository, iter_audience_file
from music_kb.schema import SCHEMA_VERSION, initialize_database
from music_kb.snapshot import create_snapshot, verify_snapshot


AUDIENCE_FIXTURES = Path(__file__).parent / "fixtures" / "audience"
SHANFENG = AUDIENCE_FIXTURES / "shanfeng.jsonl"
CHART_EVIDENCE = AUDIENCE_FIXTURES / "chart-evidence.jsonl"
SHANFENG_RECORDING_ID = "rec_shanfeng"
SHANFENG_TITLE = "山风山风等等我"
SHANFENG_ARTIST = "万海东"
SHANFENG_OBSERVATION_COUNT = 8
SHANFENG_ENTRY_COUNT = 2
AUDIENCE_TABLES = {
    "audience_source",
    "audience_profile",
    "audience_observation",
    "audience_entry",
}


def seed_recording(
    repository: MusicKBRepository,
    *,
    recording_id: str = SHANFENG_RECORDING_ID,
    title: str = SHANFENG_TITLE,
    artist: str = SHANFENG_ARTIST,
) -> str:
    """Create one canonical recording that the audience fixtures can resolve."""

    audio_sha256 = hashlib.sha256(recording_id.encode("utf-8")).hexdigest()
    repository.import_analysis(
        {
            "recording": {
                "id": recording_id,
                "title": title,
                "audio_sha256": audio_sha256,
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
    )
    return recording_id


def load_fixture_line(path: Path, index: int = 0) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[index])


def write_jsonl(tmp_path: Path, rows: list[dict[str, Any]], name: str = "audience.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def audience_database(tmp_path: Path) -> Path:
    database = tmp_path / "music-master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        seed_recording(repository)
    return database


def test_schema_v9_adds_audience_tables_and_v8_migration_is_repeatable(
    audience_database: Path,
) -> None:
    assert SCHEMA_VERSION == 9
    with MusicKBRepository(audience_database) as repository:
        tables = {
            str(row[0])
            for row in repository.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert AUDIENCE_TABLES.issubset(tables)
        assert repository.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "9"
        recording_count = repository.connection.execute(
            "SELECT COUNT(*) FROM recording"
        ).fetchone()[0]
        # Simulate a v8 publisher database: the additive audience tables are
        # absent and the stored version still says 8.
        with repository.connection:
            for table in (
                "audience_observation",
                "audience_profile",
                "audience_entry",
                "audience_source",
            ):
                repository.connection.execute(f"DROP TABLE {table}")
            repository.connection.execute(
                "UPDATE meta SET value = '8' WHERE key = 'schema_version'"
            )

    initialize_database(audience_database)
    initialize_database(audience_database)

    with MusicKBRepository(audience_database) as repository:
        tables = {
            str(row[0])
            for row in repository.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert AUDIENCE_TABLES.issubset(tables)
        assert repository.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "9"
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM recording"
        ).fetchone()[0] == recording_count
        source_columns = {
            str(row["name"])
            for row in repository.connection.execute("PRAGMA table_info(audience_source)")
        }
        assert {"source_key", "local_path"} <= source_columns
        with pytest.raises(sqlite3.IntegrityError):
            with repository.connection:
                repository.connection.execute(
                    "INSERT INTO audience_source(id, source_platform, source_key, source_type, "
                    "fetched_at, evidence_level) VALUES ('src_a', 'kugou', 'same-key', "
                    "'chart_snapshot', '2026-10-20', '1_platform_public')"
                )
                repository.connection.execute(
                    "INSERT INTO audience_source(id, source_platform, source_key, source_type, "
                    "fetched_at, evidence_level) VALUES ('src_b', 'kugou', 'same-key', "
                    "'chart_snapshot', '2026-10-20', '1_platform_public')"
                )


def test_import_audience_profile_fixture_roundtrip(audience_database: Path) -> None:
    with MusicKBRepository(audience_database) as repository:
        stats = repository.import_audience(iter_audience_file(SHANFENG))
        assert stats["errors"] == 0
        assert stats["error_details"] == []
        assert stats["kind"] == "profile"
        assert stats["batch_count"] == 1
        assert stats["profiles"] == 1
        assert stats["observations"] == SHANFENG_OBSERVATION_COUNT
        assert stats["sources"] == 1
        assert stats["entries"] == SHANFENG_ENTRY_COUNT
        assert stats["duplicates"] == 0

        counts = repository.status()["counts"]
        assert counts["audience_sources"] == 1
        assert counts["audience_profiles"] == 1
        assert counts["audience_observations"] == SHANFENG_OBSERVATION_COUNT
        assert counts["audience_entries"] == SHANFENG_ENTRY_COUNT

        profile_row = repository.connection.execute(
            "SELECT * FROM audience_profile"
        ).fetchone()
        assert profile_row["target_platform"] == "douyin"
        assert profile_row["time_window"] == "full_lifecycle"
        assert profile_row["method_id"] == "version-matrix"
        assert profile_row["method_version"] == "v1.1"
        assert profile_row["confidence"] == "low"
        assert profile_row["is_proxy"] == 1
        assert profile_row["researched_at"] == "2026-10-20"

        audience = repository.get_audience(SHANFENG_RECORDING_ID)
        assert [group["group"] for group in audience["groups"]] == [
            "research",
            "chart_evidence",
        ]
        research, chart = audience["groups"]
        assert research["label"] == "调研画像"
        assert len(research["profiles"]) == 1
        assert len(research["observations"]) == SHANFENG_OBSERVATION_COUNT
        assert len(research["sources"]) == 1
        assert len(research["entries"]) == SHANFENG_ENTRY_COUNT
        assert research["coverage_gaps"]
        assert {row["dimension"] for row in research["observations"]} == {
            "age",
            "gender",
            "city_tier",
        }
        assert {row["evidence_level"] for row in research["observations"]} == {
            "4_inferred"
        }
        assert chart["profiles"] == []
        assert chart["observations"] == []
        # The successful import path projects the research buckets into
        # recording-level tags (#140); values are never projected, only buckets.
        assert {
            (tag["namespace"], tag["canonical_name"]) for tag in audience["tags"]
        } == {
            ("audience", "age/18-23"),
            ("audience", "age/24-30"),
            ("audience", "age/31-40"),
            ("audience", "age/41-50"),
            ("audience", "age/50+"),
            ("audience", "gender/male"),
            ("audience", "gender/female"),
            ("audience", "city-tier/low"),
        }
        assert {tag["role"] for tag in audience["tags"]} == {"audience"}
        assert audience["entries"] == research["entries"]
        assert audience["counts"]["profiles"] == 1
        cover = next(
            entry for entry in audience["entries"] if entry["entry_type"] == "cover"
        )
        assert cover["performer"] == ""
        assert cover["released_at"] == ""
        original = next(
            entry for entry in audience["entries"] if entry["entry_type"] == "original"
        )
        assert original["performer"] == SHANFENG_ARTIST
        assert original["released_at"] == "2026-07-20"
        assert original["source_id"] == "src_shanfeng_kugou_search"


def test_audience_reimport_is_idempotent_and_replaces_observations(
    audience_database: Path, tmp_path: Path
) -> None:
    with MusicKBRepository(audience_database) as repository:
        repository.import_audience(iter_audience_file(SHANFENG))
        second = repository.import_audience(iter_audience_file(SHANFENG))
        assert second["profiles"] == 1
        assert second["observations"] == SHANFENG_OBSERVATION_COUNT
        # One source natural key already existed and was updated in place.
        assert second["duplicates"] == second["sources"] == 1
        counts = repository.status()["counts"]
        assert counts["audience_sources"] == 1
        assert counts["audience_profiles"] == 1
        assert counts["audience_observations"] == SHANFENG_OBSERVATION_COUNT
        assert counts["audience_entries"] == SHANFENG_ENTRY_COUNT

        changed = copy.deepcopy(load_fixture_line(SHANFENG))
        changed["profile"].pop("id")
        changed["observations"] = changed["observations"][:2]
        changed["observations"][0]["value_mid"] = 0.06
        changed.pop("entries")
        changed_path = write_jsonl(tmp_path, [changed], name="changed.jsonl")
        third = repository.import_audience(iter_audience_file(changed_path))
        assert third["observations"] == 2
        assert third["entries"] == 0
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM audience_observation"
        ).fetchone()[0] == 2
        updated = repository.connection.execute(
            "SELECT value_mid FROM audience_observation WHERE bucket = '18-23'"
        ).fetchone()
        assert updated[0] == pytest.approx(0.06)
        # A record without an entries key never touches the recording's entries.
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM audience_entry"
        ).fetchone()[0] == SHANFENG_ENTRY_COUNT

        emptied = copy.deepcopy(load_fixture_line(SHANFENG))
        emptied["profile"].pop("id")
        emptied["entries"] = []
        emptied_path = write_jsonl(tmp_path, [emptied], name="emptied.jsonl")
        fourth = repository.import_audience(iter_audience_file(emptied_path))
        assert fourth["entries"] == 0
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM audience_entry"
        ).fetchone()[0] == 0


def test_audience_sources_upsert_by_platform_and_source_key(
    audience_database: Path, tmp_path: Path
) -> None:
    rows = []
    for window in ("first_wave", "full_lifecycle"):
        row = copy.deepcopy(load_fixture_line(SHANFENG))
        row["profile"].pop("id")
        row["profile"]["time_window"] = window
        row["profile"]["window_start"] = "2026-07-20"
        row["profile"]["window_end"] = (
            "2026-08-10" if window == "first_wave" else "2026-10-19"
        )
        row["profile"]["researched_at"] = "2026-10-20"
        row.pop("entries")
        rows.append(row)
    path = write_jsonl(tmp_path, rows, name="two-windows.jsonl")
    with MusicKBRepository(audience_database) as repository:
        stats = repository.import_audience(iter_audience_file(path))
        assert stats["errors"] == 0
        assert stats["profiles"] == 2
        assert stats["sources"] == 2
        assert stats["duplicates"] == 1
        assert repository.status()["counts"]["audience_sources"] == 1
        profiles = repository.connection.execute(
            "SELECT time_window FROM audience_profile ORDER BY time_window"
        ).fetchall()
        assert [row[0] for row in profiles] == ["first_wave", "full_lifecycle"]


def test_audience_import_reports_bad_lines_and_keeps_valid_rows(
    audience_database: Path, tmp_path: Path
) -> None:
    base = load_fixture_line(SHANFENG)

    missing_window = copy.deepcopy(base)
    missing_window["profile"].pop("time_window")

    bad_share = copy.deepcopy(base)
    bad_share["profile"].pop("id")
    bad_share["observations"][0]["value_low"] = 0.9

    unknown_source = copy.deepcopy(base)
    unknown_source["profile"].pop("id")
    unknown_source["observations"][0]["source_id"] = "src_does_not_exist"

    missing_recording = copy.deepcopy(base)
    missing_recording["recording"] = {
        "title": "不存在的歌",
        "primary_artist": "不存在的歌手",
    }

    wrong_target = copy.deepcopy(base)
    wrong_target["profile"]["target_platform"] = "kugou"

    reverse_window = copy.deepcopy(base)
    reverse_window["profile"]["window_start"] = "2026-10-19"
    reverse_window["profile"]["window_end"] = "2026-07-20"

    kind_mismatch = copy.deepcopy(base)
    kind_mismatch["profile"].pop("id")
    kind_mismatch["kind"] = "chart_evidence"

    bad_entry = copy.deepcopy(base)
    bad_entry["profile"].pop("id")
    bad_entry["entries"][0]["entry_type"] = "mashup"

    bad_source_platform = copy.deepcopy(base)
    bad_source_platform["profile"].pop("id")
    bad_source_platform["sources"][0]["source_platform"] = "myspace"

    bad_rows = [
        missing_window,
        bad_share,
        unknown_source,
        missing_recording,
        wrong_target,
        reverse_window,
        kind_mismatch,
        bad_entry,
        bad_source_platform,
    ]
    path = write_jsonl(tmp_path, [base, *bad_rows], name="mixed.jsonl")
    with MusicKBRepository(audience_database) as repository:
        stats = repository.import_audience(iter_audience_file(path))
        assert stats["profiles"] == 1
        assert stats["errors"] == len(bad_rows)
        assert [error["line"] for error in stats["error_details"]] == list(
            range(2, 2 + len(bad_rows))
        )
        messages = " | ".join(
            error["message"] for error in stats["error_details"]
        )
        assert "profile.time_window" in messages
        assert "value_low <= value_mid <= value_high" in messages
        assert "source_id does not reference" in messages
        assert "not found by title/artist" in messages
        assert "target_platform" in messages
        assert "window_end must not be earlier" in messages
        assert "does not match the import kind" in messages
        assert "entry_type must be one of" in messages
        assert "source_platform must be one of" in messages
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM audience_profile"
        ).fetchone()[0] == 1


def test_import_chart_evidence_contract_and_get_filters(audience_database: Path) -> None:
    with MusicKBRepository(audience_database) as repository:
        stats = repository.import_audience(
            iter_audience_file(CHART_EVIDENCE), kind="chart_evidence"
        )
        assert stats["errors"] == 0
        assert stats["kind"] == "chart_evidence"
        assert stats["profiles"] == 2
        assert stats["observations"] == 3
        assert stats["sources"] == 3
        assert stats["entries"] == 0

        profiles = repository.connection.execute(
            "SELECT * FROM audience_profile ORDER BY researched_at"
        ).fetchall()
        assert [row["researched_at"] for row in profiles] == ["2026-10-20", "2026-10-27"]
        for row in profiles:
            assert row["target_platform"] == "unspecified"
            assert row["time_window"] == "chart_snapshot"
            assert row["method_id"] == "chart_signal_v1"
            assert row["method_version"] == "chart-hint-v1"
            assert row["status"] == "available"
        assert [row["confidence"] for row in profiles] == ["medium", "low"]

        sources = repository.connection.execute(
            "SELECT * FROM audience_source ORDER BY source_key"
        ).fetchall()
        assert [row["source_key"] for row in sources] == [
            "chart:49223:2026-10-20",
            "chart:49224:2026-10-27",
            "chart:52144:2026-10-20",
        ]
        for row in sources:
            assert row["source_platform"] == "kugou"
            assert row["source_type"] == "chart_snapshot"
            assert row["evidence_level"] == "1_platform_public"
            assert str(row["local_path"]).startswith("data/weekly_runs/")

        observations = repository.connection.execute(
            "SELECT * FROM audience_observation ORDER BY rank"
        ).fetchall()
        assert [row["dimension"] for row in observations] == [
            "scene",
            "generation_hint",
            "generation_hint",
        ]
        for row in observations:
            assert row["evidence_level"] == "3_proxy"
            assert row["is_proxy"] == 1
            assert row["rank"] >= 1
            assert row["source_id"] is not None

        audience = repository.get_audience(SHANFENG_RECORDING_ID)
        research, chart = audience["groups"]
        assert research["profiles"] == []
        assert len(chart["profiles"]) == 2
        assert chart["profiles"][0]["researched_at"] == "2026-10-27"
        assert chart["profiles"][1]["researched_at"] == "2026-10-20"
        assert len(chart["observations"]) == 3
        assert len(chart["sources"]) == 3
        assert audience["chart_evidence_note"]
        # Only the newest chart snapshot period projects hints; the older
        # 2026-10-20 generation/scene memberships stay queryable as evidence but
        # do not leave stale recording tags behind.
        assert [
            (tag["namespace"], tag["canonical_name"]) for tag in audience["tags"]
        ] == [("audience-hint", "gen/00后")]
        assert audience["tags"][0]["role"] == "audience"

        snapshot_window = repository.get_audience(
            SHANFENG_RECORDING_ID, window="chart_snapshot"
        )
        assert snapshot_window["counts"]["profiles"] == 2
        assert snapshot_window["groups"][0]["profiles"] == []
        research_only = repository.get_audience(
            SHANFENG_RECORDING_ID, method="version-matrix"
        )
        assert research_only["counts"]["profiles"] == 0
        chart_only = repository.get_audience(
            SHANFENG_RECORDING_ID, method="chart_signal_v1"
        )
        assert chart_only["counts"]["profiles"] == 2
        assert chart_only["filters"] == {
            "window": None,
            "method": "chart_signal_v1",
        }
        with pytest.raises(ValidationError):
            repository.get_audience(SHANFENG_RECORDING_ID, window="weekly")
        with pytest.raises(NotFoundError):
            repository.get_audience("rec_missing")


def test_chart_evidence_import_rejects_profile_kind_and_broken_contract(
    audience_database: Path, tmp_path: Path
) -> None:
    chart_line = load_fixture_line(CHART_EVIDENCE)
    with MusicKBRepository(audience_database) as repository:
        profile_kind = write_jsonl(tmp_path, [chart_line], name="chart-as-profile.jsonl")
        stats = repository.import_audience(iter_audience_file(profile_kind))
        assert stats["errors"] == 1
        assert "does not match the import kind" in stats["error_details"][0]["message"]

        undeclared = copy.deepcopy(chart_line)
        undeclared.pop("kind")
        undeclared_path = write_jsonl(
            tmp_path, [undeclared], name="chart-undeclared.jsonl"
        )
        stats = repository.import_audience(iter_audience_file(undeclared_path))
        assert stats["errors"] == 1
        assert "kind=chart_evidence" in stats["error_details"][0]["message"]

        no_rank = copy.deepcopy(chart_line)
        for observation in no_rank["observations"]:
            observation.pop("rank")
        no_rank_path = write_jsonl(
            tmp_path, [no_rank], name="chart-no-rank.jsonl"
        )
        stats = repository.import_audience(
            iter_audience_file(no_rank_path), kind="chart_evidence"
        )
        assert stats["errors"] == 1
        assert "require rank" in stats["error_details"][0]["message"]

        bad_source_key = copy.deepcopy(chart_line)
        bad_source_key["sources"][0]["source_key"] = "chart:49223"
        bad_source_key["observations"][0]["source_id"] = "src_chart_49223_20261020"
        bad_source_key_path = write_jsonl(
            tmp_path, [bad_source_key], name="chart-bad-key.jsonl"
        )
        stats = repository.import_audience(
            iter_audience_file(bad_source_key_path), kind="chart_evidence"
        )
        assert stats["errors"] == 1
        assert "chart:<rank_id>:<YYYY-MM-DD>" in stats["error_details"][0]["message"]


def test_validate_flags_audience_invariants_without_a_coverage_gate(
    audience_database: Path,
) -> None:
    with MusicKBRepository(audience_database) as repository:
        # An empty audience surface is valid: coverage is never a release gate.
        assert repository.validate()["valid"] is True
        repository.import_audience(iter_audience_file(SHANFENG))
        assert repository.validate()["valid"] is True

        with repository.connection:
            repository.connection.execute(
                "INSERT INTO audience_profile(id, recording_id, target_platform, "
                "time_window, method_id, method_version, status, confidence, "
                "is_proxy, researched_at) VALUES ('prof_bad', ?, 'kugou', "
                "'full_lifecycle', 'version-matrix', 'v1.1', 'available', 'low', 1, "
                "'2026-10-20')",
                (SHANFENG_RECORDING_ID,),
            )
            repository.connection.execute(
                "INSERT INTO audience_observation(id, profile_id, dimension, bucket, "
                "value_low, evidence_level, is_proxy) VALUES ('obs_bad', 'prof_bad', "
                "'age', '', 1.5, '4_inferred', 1)"
            )
            repository.connection.execute(
                "INSERT INTO audience_entry(id, recording_id, entry_type, performer, "
                "released_at) VALUES ('entry_bad', ?, 'cover', NULL, '')",
                (SHANFENG_RECORDING_ID,),
            )
        validation = repository.validate()
        codes = {issue["code"] for issue in validation["issues"]}
        assert "audience_profile_invalid_enum" in codes
        assert "audience_profile_target_platform_rule" in codes
        assert "audience_observation_empty_bucket" in codes
        assert "audience_observation_invalid_share" in codes
        assert "audience_entry_null_identity" in codes

    # Still no coverage gate: removing every audience row keeps validation valid.
    with MusicKBRepository(audience_database) as repository:
        with repository.connection:
            repository.connection.execute("DELETE FROM audience_observation")
            repository.connection.execute("DELETE FROM audience_profile")
            repository.connection.execute("DELETE FROM audience_entry")
            repository.connection.execute("DELETE FROM audience_source")
        assert repository.validate()["valid"] is True


def test_snapshot_carries_latest_audience_profiles_and_counts(
    audience_database: Path, tmp_path: Path, lyric_seed
) -> None:
    extra_research = copy.deepcopy(load_fixture_line(SHANFENG))
    extra_research["profile"].pop("id")
    extra_research["profile"]["method_version"] = "v1.2"
    extra_research["profile"]["coverage_note"] = "v1.2 复核版"
    extra_research["observations"] = extra_research["observations"][:2]
    extra_research.pop("entries")
    extra_path = write_jsonl(tmp_path, [extra_research], name="v1-2.jsonl")

    with MusicKBRepository(audience_database) as repository:
        repository.import_audience(iter_audience_file(SHANFENG))
        repository.import_audience(
            iter_audience_file(CHART_EVIDENCE), kind="chart_evidence"
        )
        repository.import_audience(iter_audience_file(extra_path))
        master_counts = repository.status()["counts"]
        assert master_counts["audience_profiles"] == 4
        assert master_counts["audience_observations"] == (
            SHANFENG_OBSERVATION_COUNT + 3 + 2
        )
        assert master_counts["audience_sources"] == 4
        assert master_counts["audience_entries"] == SHANFENG_ENTRY_COUNT
        assert repository.validate()["valid"] is True
        lyric_seed(repository)

    release = create_snapshot(
        audience_database, tmp_path / "published", release_name="audience-v9"
    )
    verified = verify_snapshot(release["manifest"])
    manifest = json.loads(Path(release["manifest"]).read_text(encoding="utf-8"))
    counts = manifest["counts"]
    assert counts["audience_profiles"] == 2
    assert counts["audience_observations"] == 3
    assert counts["audience_sources"] == 2
    assert counts["audience_entries"] == SHANFENG_ENTRY_COUNT
    assert counts == verified["status"]["counts"]

    with MusicKBRepository(release["database"], read_only=True) as snapshot:
        audience = snapshot.get_audience(SHANFENG_RECORDING_ID)
        research, chart = audience["groups"]
        assert len(research["profiles"]) == 1
        assert research["profiles"][0]["method_version"] == "v1.2"
        assert len(research["observations"]) == 2
        assert len(chart["profiles"]) == 1
        assert chart["profiles"][0]["researched_at"] == "2026-10-27"
        assert len(chart["observations"]) == 1
        assert len(audience["entries"]) == SHANFENG_ENTRY_COUNT
        local_paths = [
            row[0]
            for row in snapshot.connection.execute(
                "SELECT local_path FROM audience_source"
            )
        ]
        assert local_paths == [None, None]
        assert list(snapshot.connection.execute("PRAGMA foreign_key_check")) == []


def test_audience_cli_import_and_get(audience_database: Path, capsys) -> None:
    exit_code = cli.main(
        [
            "--json",
            "import-audience",
            "--db",
            str(audience_database),
            "--input",
            str(SHANFENG),
            "--kind",
            "profile",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["profiles"] == 1
    assert payload["result"]["errors"] == 0

    exit_code = cli.main(
        [
            "--json",
            "get-audience",
            "--db",
            str(audience_database),
            SHANFENG_RECORDING_ID,
            "--window",
            "full_lifecycle",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["counts"]["profiles"] == 1
    assert payload["result"]["groups"][0]["label"] == "调研画像"
    assert payload["result"]["filters"] == {
        "window": "full_lifecycle",
        "method": None,
    }


def test_audience_cli_reports_bad_rows_with_nonzero_exit(
    audience_database: Path, tmp_path: Path, capsys
) -> None:
    bad = copy.deepcopy(load_fixture_line(SHANFENG))
    bad["profile"].pop("time_window")
    path = write_jsonl(tmp_path, [bad], name="bad.jsonl")
    exit_code = cli.main(
        [
            "--json",
            "import-audience",
            "--db",
            str(audience_database),
            "--input",
            str(path),
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["errors"] == 1
    assert payload["result"]["error_details"][0]["line"] == 1
