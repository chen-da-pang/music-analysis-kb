from __future__ import annotations

import sqlite3
import struct
from copy import deepcopy

import pytest
from pathlib import Path

from music_kb.repository import MusicKBRepository
from music_kb.errors import DatabaseNotInitializedError, NotFoundError, ReadOnlyError, ValidationError
from music_kb.schema import initialize_database


MODEL = dict(model_id="muq-v3", model_name="MuQ-MuLan", recipe="v3", dim=2,
             metric="cosine", weight_license="CC-BY-NC-4.0")
SEED = "rec_neon_night_studio"


def test_public_normalized_embedding_helpers(master_database):
    with MusicKBRepository(master_database) as repository:
        repository.import_embeddings([embedding(vector=(3, 4))], **MODEL)
    with MusicKBRepository(master_database, read_only=True) as repository:
        with repository.read_snapshot():
            assert repository.embedding_model(MODEL["model_id"])["dim"] == 2
        with pytest.raises(NotFoundError):
            repository.embedding_model("missing")
        vector = repository.normalized_embedding(SEED, MODEL["model_id"], "")
        assert vector.tolist() == pytest.approx([0.6, 0.8])
        rows = list(repository.iter_normalized_embeddings(MODEL["model_id"], ""))
        assert len(rows) == 1
        assert rows[0][0] == SEED
        assert rows[0][1]
        assert rows[0][2].tolist() == pytest.approx([0.6, 0.8])
        assert repository.cosine_distance(vector, vector) == pytest.approx(0)
        assert repository.cosine_distance(vector, -vector) == pytest.approx(2)
        assert repository.cosine_distance([1, 0], [0, 1]) == 1
        assert repository.cosine_distance([-1.0000001, 0], [1, 0]) == 2
        assert repository.cosine_distance([1.0000001, 0], [1, 0]) == 0
        with pytest.raises(NotFoundError):
            repository.normalized_embedding("missing", MODEL["model_id"], "")


def embedding(recording_id=SEED, vector=(1.0, 0.0), sha="a" * 64, **extra):
    return dict(recording_id=recording_id, audio_sha256=sha,
                vector=struct.pack("<" + "f" * len(vector), *vector), **extra)


def test_import_and_read_only_similar_are_idempotent(master_database):
    with MusicKBRepository(master_database) as repository:
        assert repository.import_embeddings([embedding()], **MODEL)["imported"] == 1
        assert repository.import_embeddings([embedding()], **MODEL)["imported"] == 1
        assert repository.status()["counts"]["recordings_with_embeddings"] == 1
        assert repository.status()["embedding_coverage"]["ratio"] == 1.0
        assert repository.validate()["valid"]
    with MusicKBRepository(master_database, read_only=True) as repository:
        assert repository.similar(SEED)["results"] == []
        assert repository.status()["counts"]["embedding_models"] == 1


def test_similar_orders_collapses_versions_and_excludes_seed(master_database, fixture_payload):
    with MusicKBRepository(master_database) as repository:
        entries = [embedding()]
        for recording_id, title, vector, digit in [
            ("close", "Other Song", (3.0, 4.0), "b"),
            ("version", "other song", (0.0, 1.0), "c"),
            ("tie_a", "Tie A", (0.0, 1.0), "d"),
            ("tie_b", "Tie B", (0.0, 1.0), "e"),
            ("opposite", "Opposite", (-1.0, 0.0), "f"),
        ]:
            payload = deepcopy(fixture_payload)
            payload["recording"].update(id=recording_id, title=title, audio_sha256=digit * 64)
            payload["source_tracks"][0].update(source_track_id=recording_id)
            repository.import_analysis(payload)
            entries.append(embedding(recording_id, vector, digit * 64))
        repository.import_embeddings(entries, **MODEL)
    with MusicKBRepository(master_database, read_only=True) as repository:
        result = repository.similar(SEED, limit=3)
        assert [row["recording_id"] for row in result["results"]] == ["close", "tie_a", "tie_b"]
        assert [row["distance"] for row in result["results"]] == [0.4, 1.0, 1.0]
        assert result["results"][0] == dict(
            recording_id="close", title="Other Song", version_label="studio",
            artists=["示例乐队"], distance=0.4, more_versions=1,
            listen_url="https://music.example/listen/fixture-001",
        )
        assert repository.similar(SEED, limit=10)["results"][-1]["distance"] == 2.0


def test_channels_are_separate_and_model_selection_is_explicit(master_database, fixture_payload):
    with MusicKBRepository(master_database) as repository:
        payload = deepcopy(fixture_payload)
        payload["recording"].update(id="other", title="Other", audio_sha256="b" * 64)
        payload["source_tracks"] = []
        repository.import_analysis(payload)
        repository.import_embeddings([embedding(), embedding("other", (-1, 0), "b" * 64)], **MODEL)
        merit = dict(MODEL, model_id="merit", model_name="MERIT", recipe="heads")
        repository.import_embeddings([embedding(), embedding("other", (1, 0), "b" * 64)], factor="rhythm", **merit)
        repository.import_embeddings([embedding(), embedding("other", (0, 1), "b" * 64)], factor="timbre", **merit)
        assert repository.similar(SEED)["results"][0]["distance"] == 2.0
        assert repository.similar(SEED, channel="rhythm")["results"][0]["distance"] == 0.0
        assert repository.similar(SEED, channel="timbre")["results"][0]["distance"] == 1.0
        with pytest.raises(ValidationError):
            repository.similar(SEED, channel="melody")
        with pytest.raises(NotFoundError):
            repository.similar("missing")


def test_external_vector_order_collapse_limits_and_no_exclusion(master_database, fixture_payload):
    with MusicKBRepository(master_database) as repository:
        entries = [embedding()]
        for recording_id, title, vector, digit in [
            ("close", "Other", (3, 4), "b"),
            ("version", "other", (0, 1), "c"),
            ("tie_a", "Tie A", (0, 1), "d"),
            ("tie_b", "Tie B", (0, 1), "e"),
        ]:
            payload = deepcopy(fixture_payload)
            payload["recording"].update(id=recording_id, title=title, audio_sha256=digit * 64)
            payload["source_tracks"] = []
            repository.import_analysis(payload)
            entries.append(embedding(recording_id, vector, digit * 64))
        repository.import_embeddings(entries, **MODEL)
    with MusicKBRepository(master_database, read_only=True) as repository:
        query = struct.pack("<2f", 2, 0)
        result = repository.similar_to_vector(query, model_id=MODEL["model_id"])
        assert [row["recording_id"] for row in result["results"]] == [SEED, "close", "tie_a", "tie_b"]
        assert [row["distance"] for row in result["results"]] == [0, 0.4, 1, 1]
        assert result["results"][1]["more_versions"] == 1
        assert result["results"][1]["artists"] == ["示例乐队"]
        for limit, expected in [(0, 1), (1, 1), (2, 2), (10000, 4)]:
            assert repository.similar_to_vector(query, model_id=MODEL["model_id"], limit=limit)["count"] == expected
        assert repository.similar_to_vector(query, model_id=MODEL["model_id"], factor="rhythm")["results"] == []
        assert repository.similar(SEED)["results"] == result["results"][1:]


@pytest.mark.parametrize("vector", [b"", b"bad", struct.pack("<2f", 0, 0),
                                     struct.pack("<2f", float("nan"), 1),
                                     struct.pack("<2f", float("inf"), 1), [1, 0]])
def test_external_vector_rejects_invalid_vectors(master_database, vector):
    with MusicKBRepository(master_database) as repository:
        repository.import_embeddings([embedding()], **MODEL)
        with pytest.raises(ValidationError):
            repository.similar_to_vector(vector, model_id=MODEL["model_id"])


def test_external_vector_rejects_unknown_model_and_factor(master_database):
    with MusicKBRepository(master_database) as repository:
        with pytest.raises(ValidationError, match="not registered"):
            repository.similar_to_vector(embedding()["vector"], model_id="missing")
        with pytest.raises(ValidationError, match="factor"):
            repository.similar_to_vector(embedding()["vector"], model_id="missing", factor="melody")


def test_validate_detects_corrupt_vector_norm_length_and_identity(master_database):
    with MusicKBRepository(master_database) as repository:
        repository.import_embeddings([embedding()], **MODEL)
        for column, value, code in [
            ("norm", 7.0, "embedding_norm_mismatch"),
            ("vector", b"bad", "embedding_vector_invalid"),
            ("source_audio_sha256", "b" * 64, "embedding_audio_sha256_mismatch"),
        ]:
            with repository.connection:
                repository.connection.execute(f"UPDATE recording_embedding SET {column}=?", (value,))
            report = repository.validate()
            assert not report["valid"]
            assert code in {issue["code"] for issue in report["issues"]}
            repository.import_embeddings([embedding()], **MODEL)
        assert repository.validate()["valid"]


@pytest.mark.parametrize("bad", [
    embedding(vector=(1.0,)), embedding(vector=(0.0, 0.0)),
    embedding(vector=(float("nan"), 1.0)), embedding(vector=(float("inf"), 0.0)),
    embedding(norm=2.0), embedding(sha="b" * 64), embedding(recording_id="missing"),
    {"recording_id": SEED, "audio_sha256": "a" * 64, "vector": "not bytes"},
    None,
])
def test_bad_embedding_rolls_back_whole_batch_and_model(master_database, bad):
    with MusicKBRepository(master_database) as repository:
        with pytest.raises(ValidationError):
            repository.import_embeddings([embedding(), bad], **MODEL)
        assert repository.status()["counts"]["embedding_models"] == 0
        assert repository.status()["counts"]["recordings_with_embeddings"] == 0
        assert repository.status()["metadata"]["default_embedding_model"] == ""


def test_embeddings_are_publisher_only_even_with_snapshot_write_override(master_database):
    with MusicKBRepository(master_database) as repository:
        with repository.connection:
            repository.connection.execute("UPDATE meta SET value='snapshot' WHERE key='database_kind'")
    with MusicKBRepository(master_database, allow_snapshot_write=True) as repository:
        with pytest.raises(ReadOnlyError):
            repository.import_embeddings([embedding()], **MODEL)


def test_production_dimensions_model_registration_and_partial_coverage(master_database, fixture_payload):
    with MusicKBRepository(master_database) as repository:
        payload = deepcopy(fixture_payload)
        payload["recording"].update(id="unembedded", title="Unembedded", audio_sha256="b" * 64)
        payload["source_tracks"] = []
        repository.import_analysis(payload)
        with pytest.raises(ValidationError, match="not registered"):
            repository.import_embeddings([embedding()], model_id="missing")
        for model_id, name, factor, dim in [
            ("muq", "MuQ-MuLan", "", 512),
            ("merit", "MERIT", "rhythm", 128),
            ("merit", "MERIT", "timbre", 128),
        ]:
            model = dict(MODEL, model_id=model_id, model_name=name, dim=dim)
            repository.register_embedding_model(**model)
            row = embedding(vector=(1.0,) + (0.0,) * (dim - 1), norm=1.000005)
            repository.import_embeddings([row], model_id=model_id, factor=factor)
        counts = repository.status()["counts"]
        assert counts["embedding_models"] == 2
        assert counts["recordings_with_embeddings"] == 1
        assert repository.status()["embedding_coverage"]["ratio"] == 0.5
        assert repository.validate()["valid"]
        with pytest.raises(ValidationError, match="different metadata"):
            repository.register_embedding_model(**dict(MODEL, model_id="muq", dim=128))
        with pytest.raises(ValidationError, match="another id"):
            repository.register_embedding_model(**dict(MODEL, model_id="other-muq", dim=512))


def test_upsert_changes_neighbors_and_keeps_default_model(master_database, fixture_payload):
    with MusicKBRepository(master_database) as repository:
        payload = deepcopy(fixture_payload)
        payload["recording"].update(id="other", title="Other", audio_sha256="b" * 64)
        payload["source_tracks"] = []
        repository.import_analysis(payload)
        repository.import_embeddings([embedding(), embedding("other", (0, 1), "b" * 64)], **MODEL)
        assert repository.similar(SEED)["results"][0]["distance"] == 1.0
        repository.import_embeddings([embedding("other", (-1, 0), "b" * 64)], model_id=MODEL["model_id"])
        assert repository.similar(SEED)["results"][0]["distance"] == 2.0
        alternate = dict(MODEL, model_id="muq-v4", recipe="v4")
        repository.import_embeddings([embedding(), embedding("other", (1, 0), "b" * 64)], **alternate)
        assert repository.similar(SEED)["model_id"] == "muq-v3"
        assert repository.similar(SEED, model_id="muq-v4")["results"][0]["distance"] == 0.0


def test_real_v7_database_upgrades_with_data_intact(tmp_path):
    database = tmp_path / "v7.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            (Path(__file__).parent / "fixtures/schema_v7.sql").read_text()
        )
        connection.execute(
            "INSERT INTO recording(id, canonical_title, normalized_title, audio_sha256) "
            "VALUES ('legacy', 'Legacy song', 'legacy song', ?)", ("b" * 64,)
        )
    with pytest.raises(DatabaseNotInitializedError, match="expected 9"):
        MusicKBRepository(database, read_only=True)
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        assert repository.status()["schema_version"] == 9
        assert repository.status()["counts"]["embedding_models"] == 0
        assert repository.status()["counts"]["recordings"] == 2
        assert repository.search(title="Legacy song") == []
        assert len(repository.search(title="霓虹夜航")) == 1
        assert repository.search(tags=["颗粒人声切片"])[0]["recording_id"] == SEED
        assert repository.search(artist="SLYD")[0]["artists"] == ["示例乐队"]
        assert repository.get_canonical_analysis(SEED)["listen_url"] == "https://music.example/listen/fixture-001"
        assert repository.status()["counts"]["lyrics_available"] == 1
        assert repository.validate()["valid"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT canonical_title, audio_sha256 FROM recording WHERE id='legacy'"
        ).fetchone() == ("Legacy song", "b" * 64)
