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
        assert rare[0]["listen_url"] == "https://music.example/listen/fixture-001"
        assert rare[0]["source_links"][0]["url"] == rare[0]["listen_url"]
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
        assert public["listen_url"] == "https://music.example/listen/fixture-001"


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


def test_backfill_kugou_source_links_from_chart_database(master_database, tmp_path) -> None:
    chart_database = tmp_path / "charts.sqlite"
    with sqlite3.connect(chart_database) as connection:
        connection.executescript(
            """
            CREATE TABLE songs(song_id INTEGER PRIMARY KEY, canonical_title TEXT, canonical_artist TEXT);
            CREATE TABLE platform_tracks(song_id INTEGER, platform TEXT, play_link TEXT);
            INSERT INTO songs VALUES (1, '霓虹夜航', '示例乐队');
            INSERT INTO platform_tracks VALUES (
              1, 'kugou', 'https://www.kugou.com/mixsong/agent_gateway/fixture.html'
            );
            """
        )
    with MusicKBRepository(master_database) as repository:
        repository.connection.execute("UPDATE source_track SET source_name = 'kugou', source_url = NULL")
        result = repository.backfill_source_links(chart_database)
        assert result["matched"] == 1
        found = repository.search(title="霓虹夜航")[0]
        assert found["listen_url"] == "https://www.kugou.com/mixsong/agent_gateway/fixture.html"


def test_init_migrates_v5_source_tracks_to_v6(master_database) -> None:
    with sqlite3.connect(master_database) as connection:
        connection.executescript(
            """
            DROP TABLE recording_lyric;
            ALTER TABLE source_track RENAME TO source_track_v6;
            CREATE TABLE source_track (
                id TEXT PRIMARY KEY,
                recording_id TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
                source_name TEXT NOT NULL,
                source_track_id TEXT NOT NULL,
                source_title TEXT,
                source_artist_credit TEXT,
                UNIQUE(source_name, source_track_id)
            );
            INSERT INTO source_track(
              id, recording_id, source_name, source_track_id, source_title, source_artist_credit
            )
            SELECT id, recording_id, source_name, source_track_id, source_title, source_artist_credit
            FROM source_track_v6;
            DROP TABLE source_track_v6;
            UPDATE meta SET value = '5' WHERE key = 'schema_version';
            """
        )
    from music_kb.schema import initialize_database

    initialize_database(master_database)
    with MusicKBRepository(master_database) as repository:
        columns = {row["name"] for row in repository.connection.execute("PRAGMA table_info(source_track)")}
        assert "source_url" in columns
        assert repository.status()["schema_version"] == 7


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
