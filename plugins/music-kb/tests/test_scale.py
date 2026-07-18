from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from music_kb.errors import DatabaseNotInitializedError, ValidationError
from music_kb.repository import _BoundedImportLookupCache, MusicKBRepository, iter_import_file
from music_kb.schema import SCHEMA_VERSION, initialize_database


def payload(index: int, *, tag: str = "scale common") -> dict:
    return {
        "recording": {"id": f"rec_scale_{index:05d}", "title": f"scale title {index:05d}"},
        "artists": [{"name": "scale artist"}],
        "analysis": {
            "raw_text": f"shared scale description for recording {index:05d}",
            "quality_state": "passed",
        },
        "tags": [{"namespace": "genre", "name": tag, "status": "approved"}],
        "numeric_features": [],
        "source_tracks": [],
    }


def test_iter_import_file_streams_physical_lf_jsonl(tmp_path: Path) -> None:
    source = tmp_path / "stream.jsonl"
    source.write_bytes(
        b'{"id":"first","raw_text":"one\xe2\x80\xa8two\xe2\x80\xa9three"}\n'
        b'{"id":"second","raw_text":"four"}\n'
        b'{not-json}\n'
    )
    records = iter_import_file(source)
    assert next(records) == {"id": "first", "raw_text": "one\u2028two\u2029three"}
    assert next(records) == {"id": "second", "raw_text": "four"}
    with pytest.raises(ValidationError, match="line 3"):
        next(records)


def test_iter_import_file_preserves_legacy_json_shapes_with_jsonl_suffix(tmp_path: Path) -> None:
    array_source = tmp_path / "legacy.jsonl"
    array_source.write_text(json.dumps([{"id": "first"}, {"id": "second"}], indent=2), encoding="utf-8")
    assert list(iter_import_file(array_source)) == [{"id": "first"}, {"id": "second"}]

    object_source = tmp_path / "pretty.ndjson"
    object_source.write_text(json.dumps({"id": "only"}, indent=2), encoding="utf-8")
    assert list(iter_import_file(object_source)) == [{"id": "only"}]


def test_import_lookup_cache_bounds_namespace_memory() -> None:
    cache = _BoundedImportLookupCache(limit=2)
    cache.remember_namespace("first")
    cache.remember_namespace("second")
    assert cache.has_namespace("first") is True
    cache.remember_namespace("third")
    assert cache.has_namespace("first") is True
    assert cache.has_namespace("second") is False
    assert len(cache._namespaces) == 2


def test_generic_batch_import_is_bounded_and_rebuilds_fts_once(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        records = (payload(index) for index in range(5))
        with patch.object(
            repository,
            "rebuild_search_projection",
            wraps=repository.rebuild_search_projection,
        ) as one_projection, patch.object(
            repository,
            "rebuild_all_search_projections",
            wraps=repository.rebuild_all_search_projections,
        ) as full_projection:
            result = repository.import_analyses(records, batch_size=2)
        assert {key: result[key] for key in result if key not in {"imports", "imports_returned", "imports_truncated"}} == {
            "count": 5,
            "created_count": 5,
            "idempotent_count": 0,
            "canonical_count": 5,
            "batch_size": 2,
            "batch_count": 3,
            "search_projection_rebuilt": True,
        }
        assert len(result["imports"]) == 5
        assert result["imports_returned"] == 5
        assert result["imports_truncated"] is False
        assert one_projection.call_count == 0
        assert full_projection.call_count == 1
        assert repository.search(tags=["scale common"], limit=10)
        assert repository.status()["metadata"]["search_projection_state"] == "current"
        assert repository.validate()["valid"] is True


def test_generic_batch_import_caps_legacy_per_record_results(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        result = repository.import_analyses((payload(index) for index in range(1_001)), batch_size=250)
    assert result["count"] == 1_001
    assert result["imports_returned"] == 1_000
    assert len(result["imports"]) == 1_000
    assert result["imports_truncated"] is True


def test_interrupted_generic_batch_marks_projection_dirty_until_rebuilt(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        with pytest.raises(ValidationError, match="recording.title"):
            repository.import_analyses([payload(0), payload(1), {"artists": [{"name": "bad"}]}], batch_size=2)
        assert repository.status()["counts"]["recordings"] == 2
        validation = repository.validate()
        assert any(issue["code"] == "search_projection_dirty" for issue in validation["issues"])
        repository.rebuild_all_search_projections()
        assert repository.validate()["valid"] is True
        assert [item["recording_id"] for item in repository.search(tags=["scale common"])]


def test_sql_search_avoids_large_python_in_clause_for_common_tag(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        # 1,100 candidates exceed SQLite's traditional 999 bind-variable
        # threshold. The query must still use SQL-side set operations and a
        # final LIMIT rather than constructing a Python ID set + IN (...).
        repository.import_analyses((payload(index) for index in range(1_100)), batch_size=250)
        results = repository.search(
            query="shared scale",
            tags=["scale common"],
            title="scale title",
            artist="scale artist",
            limit=50,
        )
        assert len(results) == 50
        assert all(item["title"].startswith("scale title") for item in results)
        assert repository.validate()["valid"] is True


def test_text_search_unions_fts_with_partial_identity_fallback(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    rockstar = payload(0)
    rockstar["recording"]["title"] = "Rockstar"
    rockstar["analysis"]["raw_text"] = "ambient pads with no matching raw-text token"
    raw_rock = payload(1)
    raw_rock["recording"]["title"] = "Different title"
    raw_rock["analysis"]["raw_text"] = "rock music with a matching FTS token"
    with MusicKBRepository(database) as repository:
        repository.import_analyses([rockstar, raw_rock], batch_size=2)
        fts_ids = {
            str(row["recording_id"])
            for row in repository.connection.execute(
                "SELECT recording_id FROM search_fts WHERE search_fts MATCH ?", ('"rock"',)
            )
        }
        assert fts_ids == {"rec_scale_00001"}
        results = repository.search(query="rock", limit=10)
        assert {item["recording_id"] for item in results} == {
            "rec_scale_00000",
            "rec_scale_00001",
        }


def test_dirty_state_is_visible_to_read_only_validation(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as writer:
        with pytest.raises(ValidationError):
            writer.import_analyses([payload(0), {"recording": {}}], batch_size=1)
    with MusicKBRepository(database, read_only=True) as reader:
        assert any(issue["code"] == "search_projection_dirty" for issue in reader.validate()["issues"])
        assert reader.connection.execute("SELECT value FROM meta WHERE key = 'search_projection_state'").fetchone()[0] == "dirty"


def test_validate_detects_a_missing_fts_row_without_correlated_fts_scans(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_analyses((payload(index) for index in range(25)), batch_size=10)
        repository.connection.execute("DELETE FROM search_fts WHERE recording_id = ?", ("rec_scale_00007",))
        validation = repository.validate()
        assert {
            "code": "missing_search_projection",
            "recording_id": "rec_scale_00007",
        } in validation["issues"]


def test_schema_has_recording_status_index_for_canonical_switches(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        indexes = {
            str(row["name"])
            for row in repository.connection.execute("PRAGMA index_list('analysis_revision')")
        }
    assert "idx_analysis_revision_recording_status" in indexes


def test_init_migrates_v4_scale_indexes_and_projection_state(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_tag_normalized_name")
        connection.execute("DROP INDEX idx_analysis_revision_recording_status")
        connection.execute("DELETE FROM meta WHERE key = 'search_projection_state'")
        connection.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")

    with pytest.raises(DatabaseNotInitializedError, match="expected 6"):
        MusicKBRepository(database)
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        indexes = {
            str(row["name"])
            for row in repository.connection.execute("PRAGMA index_list('analysis_revision')")
        }
        tag_indexes = {
            str(row["name"])
            for row in repository.connection.execute("PRAGMA index_list('tag')")
        }
        assert repository.status()["schema_version"] == SCHEMA_VERSION
        assert "idx_analysis_revision_recording_status" in indexes
        assert "idx_tag_normalized_name" in tag_indexes
        assert repository.status()["metadata"]["search_projection_state"] == "current"


def test_generic_import_works_with_sqlite_without_returning_support(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository._supports_returning = False
        result = repository.import_analyses([payload(1)], batch_size=1)
        assert result["created_count"] == 1
        assert repository.search(tags=["scale common"])[0]["recording_id"] == "rec_scale_00001"
