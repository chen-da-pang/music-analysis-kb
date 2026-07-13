from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from music_kb.errors import ReadOnlyError, ValidationError
from music_kb.repository import MusicKBRepository, load_import_file


def test_rare_tag_alias_and_identity_alias_are_recalled(master_database) -> None:
    with MusicKBRepository(master_database, read_only=True) as repository:
        rare = repository.search(tags=["切分边击"])
        assert [item["recording_id"] for item in rare] == ["rec_neon_night_studio"]
        title = repository.search(title="Ni Hong Ye Hang")
        assert title[0]["title"] == "霓虹夜航"
        artist = repository.search(artist="SLYD")
        assert artist[0]["artists"] == ["示例乐队"]


def test_replacement_keeps_one_canonical_revision_and_hides_history(master_database, fixture_payload) -> None:
    replacement = copy.deepcopy(fixture_payload)
    replacement["analysis"]["raw_text"] = "Replacement canonical analysis: spacious pads and a compact electronic-pop hook."
    replacement["analysis"]["summary"] = "Replacement canonical summary."
    with MusicKBRepository(master_database) as repository:
        result = repository.import_analysis(replacement)
        assert result["canonical"] is True
        validation = repository.validate()
        assert validation["valid"]
        statuses = list(
            repository.connection.execute(
                "SELECT status FROM analysis_revision WHERE recording_id = ? ORDER BY created_at, id",
                ("rec_neon_night_studio",),
            )
        )
        assert sorted(row["status"] for row in statuses) == ["canonical", "superseded"]
    with MusicKBRepository(master_database, read_only=True) as repository:
        public = repository.get_canonical_analysis("rec_neon_night_studio")
        assert public["analysis"]["raw_text"].startswith("Replacement canonical analysis")


def test_canonical_requires_passed_quality(master_database, fixture_payload) -> None:
    rejected = copy.deepcopy(fixture_payload)
    rejected["analysis"]["raw_text"] = "This should not be promoted."
    rejected["analysis"]["quality_state"] = "needs_review"
    with MusicKBRepository(master_database) as repository:
        with pytest.raises(Exception, match="Only an analysis"):
            repository.import_analysis(rejected)


def test_read_only_repository_cannot_mutate(master_database) -> None:
    with MusicKBRepository(master_database, read_only=True) as repository:
        with pytest.raises(ReadOnlyError):
            repository.import_analysis({})
        with pytest.raises(sqlite3.OperationalError):
            repository.connection.execute("INSERT INTO meta(key, value) VALUES ('bad', 'write')")


def test_suno_compiler_uses_only_approved_safe_tags(master_database) -> None:
    with MusicKBRepository(master_database, read_only=True) as repository:
        compiled = repository.compile_suno_style(recording_ids=["rec_neon_night_studio"])
    assert "electronic pop" in compiled["style_prompt"]
    assert "granular vocal chop" in compiled["style_prompt"]
    assert "syncopated rimshot" not in compiled["style_prompt"]
    assert "示例乐队" not in compiled["style_prompt"]


def test_importer_rejects_feigua_workflow_tags(master_database, fixture_payload) -> None:
    forbidden = copy.deepcopy(fixture_payload)
    forbidden["recording"]["id"] = "rec_should_not_import"
    forbidden["feigua_tags"] = ["weekly hotspot"]
    with MusicKBRepository(master_database) as repository:
        with pytest.raises(ValidationError, match="Music Flamingo-only"):
            repository.import_analysis(forbidden)

    forbidden = copy.deepcopy(fixture_payload)
    forbidden["recording"]["id"] = "rec_should_not_import_path"
    forbidden["tags"][0]["path"] = "feigua/hot-topic"
    with MusicKBRepository(master_database) as repository:
        with pytest.raises(ValidationError, match="Feigua tags"):
            repository.import_analysis(forbidden)


def test_jsonl_parser_keeps_unicode_line_separators_inside_a_record(tmp_path) -> None:
    source = tmp_path / "ledger.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"id": "first", "raw_text": "one\u2028two\u2029three"}, ensure_ascii=False),
                json.dumps({"id": "second", "raw_text": "four"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    parsed = load_import_file(source)
    assert [row["id"] for row in parsed] == ["first", "second"]
    assert parsed[0]["raw_text"] == "one\u2028two\u2029three"
