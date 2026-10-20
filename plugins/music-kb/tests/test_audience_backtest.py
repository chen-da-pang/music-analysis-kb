"""Offline tests for the ticket #142 audience backtest (scripts/audience_backtest.py).

These tests only use committed fixtures and a temporary SQLite database: no
network, no Feigua CLI, no external baseline file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import audience_backtest as backtest_module
from audience_backtest import (
    AGE_BINS,
    age_31_50,
    age_l1_pp,
    anti_bias_answers,
    backtest,
    backtest_observations,
    build_payload,
    format_text,
    gender_direction,
    load_profile,
    main,
    self_consistency,
)
from music_kb import cli
from music_kb.repository import MusicKBRepository
from music_kb.schema import initialize_database

FIXTURE = Path(__file__).parent / "fixtures" / "audience" / "shanfeng.jsonl"
RECORDING_ID = "rec_shanfeng_backtest"
TITLE = "山风山风等等我"
ARTIST = "万海东"


def load_fixture_record() -> dict[str, Any]:
    return load_profile(FIXTURE)


def seed_recording(repository: MusicKBRepository) -> str:
    """Create the canonical recording the fixture resolves to."""

    audio_sha256 = hashlib.sha256(RECORDING_ID.encode("utf-8")).hexdigest()
    repository.import_analysis(
        {
            "recording": {
                "id": RECORDING_ID,
                "title": TITLE,
                "audio_sha256": audio_sha256,
            },
            "artists": [{"name": ARTIST, "role": "primary"}],
            "analysis": {
                "raw_text": f"Synthetic canonical analysis for {TITLE}.",
                "summary": "",
                "model_version": "fixture-audience-backtest",
                "prompt_version": "fixture",
                "quality_state": "passed",
            },
            "source_tracks": [
                {
                    "source": "fixture",
                    "source_track_id": RECORDING_ID,
                    "source_title": TITLE,
                    "source_artist_credit": ARTIST,
                }
            ],
        }
    )
    return RECORDING_ID


def test_shanfeng_backtest_recomputes_the_ticket_expected_values() -> None:
    payload = build_payload(FIXTURE)
    result = payload["shanfeng"]

    assert payload["ticket"] == 142
    assert payload["window"] == "full_lifecycle"
    assert result["age"]["l1_pp"] == pytest.approx(24.05, abs=0.01)
    assert result["age_31_50"]["error_pp"] == pytest.approx(8.36, abs=0.01)
    assert result["age_31_50"]["profile_share"] == pytest.approx(0.64, abs=1e-9)
    assert result["age_31_50"]["baseline_share"] == pytest.approx(0.7236, abs=1e-9)
    assert result["gender"]["direction"] == "correct"
    assert result["gender"]["profile_male"] == pytest.approx(0.56, abs=1e-9)
    assert result["gender"]["baseline_male"] == pytest.approx(0.6040, abs=1e-9)
    assert result["verdict"] == "PASS"
    assert payload["verdict"] == "PASS"

    # Recomputing from the fixture must match the ticket’s “current expectation”
    # exactly; a drift means the fixture or the formula changed.
    assert result["expected"]["age_l1_delta_pp"] == pytest.approx(0.0, abs=0.01)
    assert result["expected"]["age_31_50_error_delta_pp"] == pytest.approx(0.0, abs=0.01)
    assert {check["id"] for check in result["checks"] if check["passed"]} == {
        "age_l1",
        "age_31_50",
        "gender_direction",
    }

    detail = {row["bucket"]: row for row in result["age"]["bins"]}
    assert [row["bucket"] for row in result["age"]["bins"]] == list(AGE_BINS)
    assert detail["31-40"]["abs_diff_pp"] == pytest.approx(12.02, abs=0.01)
    assert detail["50+"]["abs_diff_pp"] == pytest.approx(4.16, abs=0.01)

    # City tier / province are not backtested: the baseline has no city-tier
    # field, and the fixture has no province numbers.
    assert {row["dimension"] for row in result["not_compared"]} == {"city_tier", "province"}


def test_age_l1_sums_absolute_deviations_without_dividing_by_two() -> None:
    baseline = {bucket: 0.2 for bucket in AGE_BINS}
    profile = {"18-23": 1.0, "24-30": 0.0, "31-40": 0.0, "41-50": 0.0, "50+": 0.0}
    # |1.0-0.2| + 4*|0.0-0.2| = 1.6 -> 160pp.  Dividing by two would give 80.
    assert age_l1_pp(profile, baseline) == pytest.approx(160.0)


def test_age_31_50_is_the_sum_of_the_two_bins() -> None:
    baseline = {bucket: 0.2 for bucket in AGE_BINS}
    profile = {bucket: 0.2 for bucket in AGE_BINS}
    profile["31-40"] = 0.4
    profile["41-50"] = 0.2
    profile["18-23"] = 0.0
    profile["24-30"] = 0.2
    profile["50+"] = 0.2
    # p = 0.4 + 0.2 = 0.6; q = 0.2 + 0.2 = 0.4; error = 20pp.
    result = age_31_50(profile, baseline)
    assert result["profile_share"] == pytest.approx(0.6)
    assert result["baseline_share"] == pytest.approx(0.4)
    assert result["error_pp"] == pytest.approx(20.0)


def test_gender_direction_detects_flipped_and_tied_margins() -> None:
    baseline = {"male": 0.6040, "female": 0.3960}
    assert gender_direction({"male": 0.56, "female": 0.44}, baseline)["direction"] == "correct"
    assert gender_direction({"male": 0.40, "female": 0.60}, baseline)["direction"] == "incorrect"
    tie = gender_direction({"male": 0.5, "female": 0.5}, baseline)
    assert tie["profile_leans"] == "tie"
    assert tie["direction"] == "incorrect"


def test_second_song_is_blocked_without_offline_input_and_has_no_verdict() -> None:
    payload = build_payload(FIXTURE)
    second = payload["second_song"]
    assert second["status"] == "blocked: 无离线输入"
    assert second["verdict"] is None
    assert "不产出 PASS/FAIL" in second["reason"]
    text = format_text(payload)
    assert "blocked: 无离线输入" in text
    assert "不产出 PASS/FAIL" in text


def test_self_consistency_passes_for_the_shanfeng_record() -> None:
    record = load_fixture_record()
    result = self_consistency(record)

    assert result["verdict"] == "PASS"
    check_ids = {check["id"] for check in result["checks"]}
    assert check_ids == {
        "contract_fields",
        "share_sums",
        "source_coverage",
        "no_platform_share_fact",
        "anti_bias_eight",
    }
    assert all(check["passed"] for check in result["checks"])

    answers = anti_bias_answers(record)
    assert [row["id"] for row in answers] == list(range(1, 9))
    assert all(row["answer"] in {"是", "否", "无法判断"} for row in answers)
    assert all(row["basis"] for row in answers)
    # Record-level answers pinned so the report and the script cannot drift.
    assert [row["answer"] for row in answers] == [
        "否",
        "否",
        "无法判断",
        "否",
        "是",
        "否",
        "无法判断",
        "否",
    ]


def test_self_consistency_flags_a_broken_record() -> None:
    base = load_fixture_record()

    missing_source = copy.deepcopy(base)
    missing_source["observations"][0].pop("source_id", None)
    result = self_consistency(missing_source)
    assert result["verdict"] == "FAIL"
    assert {check["id"] for check in result["checks"] if not check["passed"]} == {
        "source_coverage"
    }

    platform_fact = copy.deepcopy(base)
    platform_fact["observations"][0]["evidence_level"] = "1_platform_public"
    result = self_consistency(platform_fact)
    assert result["verdict"] == "FAIL"
    assert {check["id"] for check in result["checks"] if not check["passed"]} == {
        "no_platform_share_fact"
    }

    no_coverage_note = copy.deepcopy(base)
    no_coverage_note["profile"].pop("coverage_note")
    result = self_consistency(no_coverage_note)
    assert result["verdict"] == "FAIL"
    # The missing note also removes the platform-caliber declaration.
    assert {check["id"] for check in result["checks"] if not check["passed"]} == {
        "contract_fields",
        "no_platform_share_fact",
    }

    bad_share = copy.deepcopy(base)
    bad_share["observations"][0]["value_mid"] = 0.9
    result = self_consistency(bad_share)
    assert result["verdict"] == "FAIL"
    assert {check["id"] for check in result["checks"] if not check["passed"]} == {
        "share_sums"
    }


def test_backtest_rejects_records_without_the_five_age_bins() -> None:
    record = load_fixture_record()
    record["observations"] = [
        observation
        for observation in record["observations"]
        if not (observation["dimension"] == "age" and observation["bucket"] == "50+")
    ]
    with pytest.raises(backtest_module.BacktestError):
        backtest(record)


def test_fixture_imports_through_import_audience_and_backtest_matches(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "music-master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        seed_recording(repository)

    exit_code = cli.main(
        [
            "--json",
            "import-audience",
            "--db",
            str(database),
            "--input",
            str(FIXTURE),
            "--kind",
            "profile",
        ]
    )
    assert exit_code == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["result"]["errors"] == 0
    assert imported["result"]["profiles"] == 1
    assert imported["result"]["observations"] == 8
    assert imported["result"]["sources"] == 1

    with MusicKBRepository(database) as repository:
        audience = repository.get_audience(RECORDING_ID, window="full_lifecycle")
    research = audience["groups"][0]
    assert research["group"] == "research"
    assert len(research["profiles"]) == 1
    assert len(research["observations"]) == 8

    # Stored observations (value_mid from the DB) reproduce the ticket numbers.
    result = backtest_observations(research["observations"])
    assert result["age"]["l1_pp"] == pytest.approx(24.05, abs=0.01)
    assert result["age_31_50"]["error_pp"] == pytest.approx(8.36, abs=0.01)
    assert result["gender"]["direction"] == "correct"
    assert result["verdict"] == "PASS"


CHART_FIXTURE = Path(__file__).parent / "fixtures" / "audience" / "chart-evidence.jsonl"


def _chart_evidence_rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in CHART_FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_chart_evidence_import_does_not_change_research_profile_values(
    tmp_path: Path,
) -> None:
    """Review finding #3: chart evidence is separate and never mutates research.

    The ticket #142 backtest compares research ``value_mid`` numbers; importing
    the weekly chart snapshot afterwards must leave every research observation,
    profile and the recomputed L1/31–50/sex-direction numbers byte-for-byte
    unchanged.
    """

    database = tmp_path / "music-master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        seed_recording(repository)
        assert repository.import_audience([load_fixture_record()])["errors"] == 0
        before = repository.get_audience(RECORDING_ID)
        research_before = before["groups"][0]
        observations_before = [
            (
                observation["dimension"],
                observation["bucket"],
                observation["value_low"],
                observation["value_mid"],
                observation["value_high"],
                observation["evidence_level"],
                observation["source_id"],
            )
            for observation in research_before["observations"]
        ]
        profile_before = dict(research_before["profiles"][0])

        chart_stats = repository.import_audience(
            _chart_evidence_rows(), kind="chart_evidence"
        )
        assert chart_stats["errors"] == 0
        assert chart_stats["profiles"] == 2

        after = repository.get_audience(RECORDING_ID)
        research_after = after["groups"][0]
        observations_after = [
            (
                observation["dimension"],
                observation["bucket"],
                observation["value_low"],
                observation["value_mid"],
                observation["value_high"],
                observation["evidence_level"],
                observation["source_id"],
            )
            for observation in research_after["observations"]
        ]
        assert observations_after == observations_before
        assert dict(research_after["profiles"][0]) == profile_before
        assert research_after["coverage_gaps"] == research_before["coverage_gaps"]

        # Chart evidence is grouped separately; a chart hit is not a gap and
        # its hit count is carried in basis_note.
        chart = after["groups"][1]
        assert len(chart["profiles"]) == 2
        assert chart["coverage_gaps"] == []
        assert all(profile["coverage_note"] is None for profile in chart["profiles"])
        assert all("命中" in profile["basis_note"] for profile in chart["profiles"])

        # The ticket numbers still reproduce from the untouched research rows.
        result = backtest_observations(research_after["observations"])
        assert result["age"]["l1_pp"] == pytest.approx(24.05, abs=0.01)
        assert result["age_31_50"]["error_pp"] == pytest.approx(8.36, abs=0.01)
        assert result["gender"]["direction"] == "correct"
        assert result["verdict"] == "PASS"


def test_cli_json_output_is_the_same_evidence_payload(capsys) -> None:
    exit_code = main(["--fixture", str(FIXTURE), "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "PASS"
    assert payload["shanfeng"]["age"]["l1_pp"] == pytest.approx(24.05, abs=0.01)
    assert payload["second_song"]["status"] == "blocked: 无离线输入"
