from __future__ import annotations

import hashlib
import json
import sqlite3
from unittest.mock import patch
from pathlib import Path

import pytest

from music_kb.campaign_delivery import load_campaign_delivery_file
from music_kb.cli import build_parser, run
from music_kb.errors import ReadOnlyError, ValidationError
from music_kb.repository import MusicKBRepository
from music_kb.schema import SCHEMA_VERSION, initialize_database
from music_kb.snapshot import create_snapshot
from music_kb.tagging import PARSER_SOURCE, extract_music_flamingo_metadata


FIXTURE = Path(__file__).parent / "fixtures" / "kugou_canonical_delivery.jsonl"


def fixture_record() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8").split("\n")[0])


def write_jsonl(path: Path, record: dict) -> Path:
    path.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


SYNTHETIC_ANALYSIS = """- Tempo/feel: Fast, driving 4/4 at 130.43 BPM.
- Rhythm and groove: Syncopated trap bounce with double-time hi-hats and triplet flurries.
- Instrumentation/production: 808 sub-bass, synth pads, sidechain compression, wide stereo image, reverb and delay.
- Tonality/harmony: D minor with maj7 color and modal interchange.
- Vocals: Male baritone rap in Mandarin with autotune and ad-libs.
- Lyrics/themes: Love and heartbreak are explicit, but the exact lyric text is not a style tag.
- Structure: Intro → verse → pre-chorus → hook → chorus → bridge → outro.
- Mood/genre context: Energetic, assertive Mandarin trap with EDM-pop production.
"""


def tag_names(tags: list[dict]) -> set[tuple[str, str]]:
    return {(tag["namespace"], tag["name"]) for tag in tags}


def test_extractor_emits_detailed_retrieval_tags_and_keeps_lyric_themes() -> None:
    tags, features = extract_music_flamingo_metadata(SYNTHETIC_ANALYSIS)
    names = tag_names(tags)
    assert {
        ("section", "tempo"),
        ("section", "production"),
        ("genre", "trap"),
        ("meter", "4/4"),
        ("rhythm", "syncopated"),
        ("rhythm", "double time"),
        ("instrument", "808 sub bass"),
        ("production", "sidechain"),
        ("harmony", "key center D minor"),
        ("vocal", "baritone"),
        ("structure", "pre chorus"),
        ("mood", "energetic"),
    }.issubset(names)
    assert features == [
        {
            "name": "bpm",
            "value": 130.43,
            "unit": "bpm",
            "confidence": 1.0,
            "source": PARSER_SOURCE,
        }
    ]
    lyric_tags = [tag for tag in tags if tag["namespace"] == "lyric_theme"]
    assert {tag["name"] for tag in lyric_tags} >= {"love", "heartbreak"}
    # Tagging is for retrieval; it carries no generation-approval metadata.
    assert all("suno_safe" not in tag for tag in tags)


def test_extractor_maps_chinese_music_flamingo_terms_to_the_same_taxonomy() -> None:
    text = """- 速度/感觉：快速四四拍，带有切分和三连音。
- 配器/制作：808低音、合成器铺底、侧链、混响和宽立体声。
- 和声：小调配合大七和弦。
- 人声：男中音中文人声，使用自动音高与即兴呼喊。
- 歌词/主题：爱情与心碎。
- 结构：前奏、主歌、预副歌、副歌、桥段和尾奏。
- 情绪/流派：忧郁但充满希望的华语流行与电子舞曲。"""
    tags, _ = extract_music_flamingo_metadata(text)
    names = tag_names(tags)
    assert {
        ("section", "tempo"),
        ("section", "instrumentation"),
        ("genre", "mandopop"),
        ("genre", "edm"),
        ("meter", "4/4"),
        ("rhythm", "syncopated"),
        ("instrument", "808 sub bass"),
        ("production", "sidechain"),
        ("harmony", "minor key"),
        ("vocal", "baritone"),
        ("structure", "pre chorus"),
        ("mood", "melancholic"),
    }.issubset(names)
    assert any(tag["namespace"] == "lyric_theme" for tag in tags)


def test_extractor_excludes_identity_and_lyric_vocabulary_from_descriptor_tags() -> None:
    tags, _ = extract_music_flamingo_metadata(
        """Title: Rock
Artist: The Trap House
Lyrics/themes: quoted lyric says \"sidechain, reverb, drop, and warm\".
Vocals: English lyrics are sung by a clear female vocal.
"""
    )
    names = {tag["name"] for tag in tags}
    assert not names.intersection({"rock", "trap", "house", "sidechain", "reverb", "drop", "warm"})
    # ``English lyrics`` is a real vocal descriptor, not a lyric section.
    assert ("vocal", "english vocals") in tag_names(tags)


def test_extractor_keeps_genuine_production_terms_after_safety_filtering() -> None:
    tags, _ = extract_music_flamingo_metadata(
        """Title: Rock
Artist: The Trap House
Production: sidechain compression and controlled reverb create the pulse.
"""
    )
    names = tag_names(tags)
    assert ("production", "sidechain") in names
    assert ("production", "reverb") in names
    assert ("genre", "rock") not in names
    assert ("genre", "trap") not in names
    assert ("genre", "house") not in names


@pytest.mark.parametrize(
    "raw_text",
    (
        "**Title:** Rock\n**Artist:** The Trap House\nProduction: piano.",
        "### Title: Rock\n### Artists: The Trap House\nProduction: piano.",
        "Track Name: Rock\nSource Artist: The Trap House\nProduction: piano.",
        "Source Title: Rock\nArtist Credit: The Trap House\nProduction: piano.",
    ),
)
def test_extractor_excludes_markdown_and_source_identity_labels(raw_text: str) -> None:
    tags, _ = extract_music_flamingo_metadata(raw_text)
    names = tag_names(tags)
    assert ("instrument", "piano") in names
    assert not {"rock", "trap", "house"}.intersection({name for _, name in names})


def test_campaign_import_indexes_parser_tags_for_retrieval(tmp_path: Path) -> None:
    record = fixture_record()
    record["output_text"] = SYNTHETIC_ANALYSIS
    record["output_text_sha256"] = hashlib.sha256(SYNTHETIC_ANALYSIS.encode("utf-8")).hexdigest()
    path = write_jsonl(tmp_path / "campaign.jsonl", record)
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        result = repository.import_campaign_delivery(load_campaign_delivery_file(path))
        recording_id = result["imports"][0]["recording_id"]
        assert repository.search(tags=["sidechain"])[0]["recording_id"] == recording_id
        assert repository.search(query="侧链")[0]["recording_id"] == recording_id
        canonical = repository.get_canonical_analysis(recording_id)
        assert canonical["numeric_features"] == [
            {
                "name": "bpm",
                "value": 130.43,
                "unit": "bpm",
                "confidence": 1.0,
                "source": PARSER_SOURCE,
            }
        ]
        assert repository.search(tags=["love"])[0]["recording_id"] == recording_id
        assert repository.search(title=record["title"])[0]["recording_id"] == recording_id
        assert repository.search(artist=record["artist"])[0]["recording_id"] == recording_id
        assert repository.validate()["valid"] is True


def test_publisher_backfill_is_idempotent_and_preserves_manual_assignments(tmp_path: Path) -> None:
    record = fixture_record()
    record["output_text"] = SYNTHETIC_ANALYSIS
    record["output_text_sha256"] = hashlib.sha256(SYNTHETIC_ANALYSIS.encode("utf-8")).hexdigest()
    path = write_jsonl(tmp_path / "campaign.jsonl", record)
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        imported = repository.import_campaign_delivery(load_campaign_delivery_file(path))
        recording_id = imported["imports"][0]["recording_id"]
        analysis_id = repository.get_canonical_analysis(recording_id)["analysis"]["id"]
        manual_tag_id, _ = repository._upsert_tag_from_payload(
            {"namespace": "manual", "name": "curated exception", "status": "approved"}
        )
        repository.connection.execute(
            "INSERT INTO analysis_tag(analysis_id, tag_id, confidence, source) VALUES (?, ?, ?, 'manual')",
            (analysis_id, manual_tag_id, 1.0),
        )
        # A human measurement takes precedence over parser-derived BPM and
        # must survive later deterministic parser reruns.
        repository.connection.execute(
            "UPDATE numeric_feature SET value = 99, source = 'manual' WHERE analysis_id = ? AND name = 'bpm'",
            (analysis_id,),
        )
        dry = repository.enrich_campaign_tags(dry_run=True)
        assert dry["dry_run"] is True
        assert dry["analysis_count"] == 1
        assert dry["tag_assignment_count"] >= 20
        first = repository.enrich_campaign_tags()
        second = repository.enrich_campaign_tags()
        assert first["parser_source"] == PARSER_SOURCE
        assert second["analysis_count"] == 1
        manual = repository.connection.execute(
            "SELECT source FROM analysis_tag WHERE analysis_id = ? AND tag_id = ?",
            (analysis_id, manual_tag_id),
        ).fetchone()
        assert manual["source"] == "manual"
        parser_count = repository.connection.execute(
            "SELECT COUNT(*) FROM analysis_tag WHERE analysis_id = ? AND source = ?",
            (analysis_id, PARSER_SOURCE),
        ).fetchone()[0]
        assert parser_count == first["tag_assignment_count"]
        assert repository.search(tags=["curated exception"])[0]["recording_id"] == recording_id
        assert repository.search(query="侧链")[0]["recording_id"] == recording_id
        manual_bpm = repository.connection.execute(
            "SELECT value, source FROM numeric_feature WHERE analysis_id = ? AND name = 'bpm'",
            (analysis_id,),
        ).fetchone()
        assert dict(manual_bpm) == {"value": 99.0, "source": "manual"}
        assert repository.validate()["valid"] is True


def test_retrieval_parser_tags_ignore_legacy_suno_safe_metadata(tmp_path: Path) -> None:
    record = fixture_record()
    record["output_text"] = SYNTHETIC_ANALYSIS
    record["output_text_sha256"] = hashlib.sha256(SYNTHETIC_ANALYSIS.encode("utf-8")).hexdigest()
    path = write_jsonl(tmp_path / "campaign.jsonl", record)
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        # Simulate a pre-existing global tag carrying a legacy prompt-use flag
        # before the retrieval parser began assigning the same namespace/name.
        repository.import_analysis(
            {
                "recording": {"id": "rec_safe_seed", "title": "safe seed"},
                "artists": [{"name": "safe seed artist"}],
                "analysis": {"raw_text": "seed", "quality_state": "passed"},
                "tags": [{"namespace": "genre", "name": "trap", "status": "approved", "suno_safe": True}],
                "numeric_features": [],
                "source_tracks": [],
            }
        )
        imported = repository.import_campaign_delivery(load_campaign_delivery_file(path))
        recording_id = imported["imports"][0]["recording_id"]
        canonical = repository.get_canonical_analysis(recording_id)
        parser_trap = next(
            tag for tag in canonical["tags"] if tag["namespace"] == "genre" and tag["canonical_name"] == "trap"
        )
        assert parser_trap["source"] == PARSER_SOURCE
        assert "suno_safe" not in parser_trap
        assert repository.search(tags=["trap"])[0]["recording_id"] == recording_id


def test_backfill_cli_dry_run_and_snapshot_write_rejection(tmp_path: Path) -> None:
    record = fixture_record()
    record["output_text"] = SYNTHETIC_ANALYSIS
    record["output_text_sha256"] = hashlib.sha256(SYNTHETIC_ANALYSIS.encode("utf-8")).hexdigest()
    path = write_jsonl(tmp_path / "campaign.jsonl", record)
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_campaign_delivery(load_campaign_delivery_file(path))

    args = build_parser().parse_args(
        ["enrich-campaign-tags", "--db", str(database), "--dry-run"]
    )
    code, result = run(args)
    assert code == 0
    assert result["dry_run"] is True
    release = create_snapshot(database, tmp_path / "releases", release_name="tagger-fixture")
    with MusicKBRepository(release["database"]) as snapshot:
        with pytest.raises(ReadOnlyError):
            snapshot.enrich_campaign_tags()


def test_backfill_streams_in_bounded_batches(tmp_path: Path) -> None:
    records = [fixture_record(), json.loads(FIXTURE.read_text(encoding="utf-8").split("\n")[1])]
    for record in records:
        record["output_text"] = SYNTHETIC_ANALYSIS
        record["output_text_sha256"] = hashlib.sha256(SYNTHETIC_ANALYSIS.encode("utf-8")).hexdigest()
    path = tmp_path / "campaign.jsonl"
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_campaign_delivery(load_campaign_delivery_file(path))
        with patch.object(repository, "rebuild_search_projection", wraps=repository.rebuild_search_projection) as single_rebuild:
            result = repository.enrich_campaign_tags(batch_size=1)
        assert single_rebuild.call_count == 0
        assert result["analysis_count"] == 2
        assert result["batch_size"] == 1
        assert result["batch_count"] == 2
        assert repository.connection.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0] == 2
        assert len(repository.search(query="sidechain")) == 2
        with pytest.raises(ValidationError, match="batch_size"):
            repository.enrich_campaign_tags(batch_size=0)


def test_campaign_import_defers_fts_rebuild_until_the_full_delivery(tmp_path: Path) -> None:
    records = [fixture_record(), json.loads(FIXTURE.read_text(encoding="utf-8").split("\n")[1])]
    path = tmp_path / "campaign.jsonl"
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        with patch.object(repository, "rebuild_search_projection", wraps=repository.rebuild_search_projection) as single_rebuild:
            imported = repository.import_campaign_delivery(load_campaign_delivery_file(path))
        assert imported["recording_count"] == 2
        assert single_rebuild.call_count == 0
        assert repository.connection.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0] == 2
        assert len(repository.search(query="music")) == 2


def test_campaign_import_rolls_back_when_the_final_fts_rebuild_fails(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "campaign.jsonl", fixture_record())
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        with patch.object(
            repository,
            "_insert_search_projection",
            side_effect=sqlite3.OperationalError("forced FTS failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="forced FTS failure"):
                repository.import_campaign_delivery(load_campaign_delivery_file(path))
        assert repository.status()["counts"]["recordings"] == 0
        assert repository.status()["counts"]["campaign_delivery_provenance"] == 0
        assert repository.connection.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0] == 0


def test_init_migrates_v3_numeric_feature_source_and_preserves_legacy_value(tmp_path: Path) -> None:
    record = fixture_record()
    record["output_text"] = SYNTHETIC_ANALYSIS
    record["output_text_sha256"] = hashlib.sha256(SYNTHETIC_ANALYSIS.encode("utf-8")).hexdigest()
    path = write_jsonl(tmp_path / "campaign.jsonl", record)
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        imported = repository.import_campaign_delivery(load_campaign_delivery_file(path))
        recording_id = imported["imports"][0]["recording_id"]
        analysis_id = repository.get_canonical_analysis(recording_id)["analysis"]["id"]

    # Recreate the v3 table shape faithfully: it had no feature source column.
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE numeric_feature RENAME TO numeric_feature_v4")
        connection.execute(
            """
            CREATE TABLE numeric_feature (
                analysis_id TEXT NOT NULL REFERENCES analysis_revision(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT '',
                confidence REAL,
                PRIMARY KEY (analysis_id, name),
                CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
            )
            """
        )
        connection.execute(
            """
            INSERT INTO numeric_feature(analysis_id, name, value, unit, confidence)
            SELECT analysis_id, name, value, unit, confidence FROM numeric_feature_v4
            """
        )
        connection.execute("DROP TABLE numeric_feature_v4")
        connection.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")

    initialize_database(database)
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        column_rows = list(repository.connection.execute("PRAGMA table_info(numeric_feature)"))
        columns = {row["name"] for row in column_rows}
        assert "source" in columns
        assert next(row["dflt_value"] for row in column_rows if row["name"] == "source") == "'model'"
        legacy = repository.connection.execute(
            "SELECT value, source FROM numeric_feature WHERE analysis_id = ? AND name = 'bpm'",
            (analysis_id,),
        ).fetchone()
        assert dict(legacy) == {"value": 130.43, "source": "legacy"}
        repository.enrich_campaign_tags()
        preserved = repository.connection.execute(
            "SELECT value, source FROM numeric_feature WHERE analysis_id = ? AND name = 'bpm'",
            (analysis_id,),
        ).fetchone()
        assert dict(preserved) == {"value": 130.43, "source": "legacy"}
        assert repository.status()["schema_version"] == SCHEMA_VERSION
        assert repository.validate()["valid"] is True


def test_init_migrates_real_v1_shape_before_marking_schema_v4(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    legacy_payload = {
        "recording": {"id": "rec_v1_legacy", "title": "legacy record"},
        "artists": [{"name": "legacy artist"}],
        "analysis": {"raw_text": "legacy analysis", "quality_state": "passed"},
        "tags": [],
        "numeric_features": [{"name": "bpm", "value": 99, "unit": "bpm"}],
        "source_tracks": [],
    }
    with MusicKBRepository(database) as repository:
        repository.import_analysis(legacy_payload)

    # v1 had no campaign provenance table and no numeric_feature.source.
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE campaign_delivery_provenance")
        connection.execute("ALTER TABLE numeric_feature RENAME TO numeric_feature_v4")
        connection.execute(
            """
            CREATE TABLE numeric_feature (
                analysis_id TEXT NOT NULL REFERENCES analysis_revision(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT '',
                confidence REAL,
                PRIMARY KEY (analysis_id, name),
                CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
            )
            """
        )
        connection.execute(
            """
            INSERT INTO numeric_feature(analysis_id, name, value, unit, confidence)
            SELECT analysis_id, name, value, unit, confidence FROM numeric_feature_v4
            """
        )
        connection.execute("DROP TABLE numeric_feature_v4")
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")

    initialize_database(database)
    with MusicKBRepository(database) as repository:
        legacy = repository.connection.execute(
            """
            SELECT nf.source FROM numeric_feature nf
            JOIN analysis_revision ar ON ar.id = nf.analysis_id
            WHERE ar.recording_id = 'rec_v1_legacy' AND nf.name = 'bpm'
            """
        ).fetchone()
        assert legacy["source"] == "legacy"
        assert repository.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'campaign_delivery_provenance'"
        ).fetchone()
        repository.import_analysis(
            {
                "recording": {"id": "rec_v4_new", "title": "new record"},
                "artists": [{"name": "new artist"}],
                "analysis": {"raw_text": "new analysis", "quality_state": "passed"},
                "tags": [],
                "numeric_features": [{"name": "bpm", "value": 120, "unit": "bpm"}],
                "source_tracks": [],
            }
        )
        created = repository.connection.execute(
            """
            SELECT nf.source FROM numeric_feature nf
            JOIN analysis_revision ar ON ar.id = nf.analysis_id
            WHERE ar.recording_id = 'rec_v4_new' AND nf.name = 'bpm'
            """
        ).fetchone()
        assert created["source"] == "model"
        assert repository.status()["schema_version"] == SCHEMA_VERSION
        assert repository.validate()["valid"] is True
