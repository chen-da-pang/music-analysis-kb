from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from music_kb.campaign_delivery import load_campaign_delivery_file
from music_kb.errors import ValidationError
from music_kb.repository import MusicKBRepository
from music_kb.schema import SCHEMA_VERSION, initialize_database
from music_kb.snapshot import create_snapshot


FIXTURE = Path(__file__).parent / "fixtures" / "kugou_canonical_delivery.jsonl"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "music_kb.cli", *arguments], text=True, capture_output=True, check=False
    )


def fixture_records() -> list[dict]:
    raw = FIXTURE.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    return [json.loads(line) for line in raw.split("\n") if line]


def write_jsonl(path: Path, records: list[dict], *, newline: str = "\n") -> Path:
    path.write_text(
        newline.join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records)
        + newline,
        encoding="utf-8",
        newline="",
    )
    return path


def with_output(record: dict, text: str) -> dict:
    changed = copy.deepcopy(record)
    changed["output_text"] = text
    changed["output_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return changed


def test_delivery_loader_is_strict_lf_and_keeps_unicode_line_separator() -> None:
    entries = load_campaign_delivery_file(FIXTURE, expected_count=2)
    assert [entry.delivery_id for entry in entries] == ["kg-fixture-0001", "kg-fixture-0002"]
    assert entries[0].output_text.endswith("\u2028Keep the arrangement compact.")
    with pytest.raises(ValidationError, match="expected 927"):
        load_campaign_delivery_file(FIXTURE, expected_count=927)


@pytest.mark.parametrize(
    ("name", "mutate", "match"),
    [
        ("missing-field", lambda rows: rows[0].pop("contract"), "missing required fields"),
        (
            "output-hash",
            lambda rows: rows[0].__setitem__("output_text_sha256", "0" * 64),
            "does not match",
        ),
        (
            "duplicate-id",
            lambda rows: rows[1].__setitem__("id", rows[0]["id"]),
            "duplicate 'id'",
        ),
        (
            "duplicate-index",
            lambda rows: rows[1].__setitem__("manifest_index", rows[0]["manifest_index"]),
            "duplicate 'manifest_index'",
        ),
        (
            "feigua",
            lambda rows: rows[0].__setitem__("canonical_source", "Feigua weekly topics"),
            "Feigua",
        ),
        (
            "token-limit",
            lambda rows: rows[0].__setitem__("generated_token_count", 1401),
            "exceeds max_new_tokens",
        ),
        (
            "unsupported-schema",
            lambda rows: rows[0].__setitem__("schema_version", 2),
            "unsupported schema_version",
        ),
    ],
)
def test_delivery_loader_rejects_invalid_contract(
    tmp_path: Path, name: str, mutate, match: str
) -> None:
    records = fixture_records()
    mutate(records)
    path = write_jsonl(tmp_path / f"{name}.jsonl", records)
    with pytest.raises(ValidationError, match=match):
        load_campaign_delivery_file(path)


def test_delivery_loader_rejects_crlf(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "crlf.jsonl", fixture_records(), newline="\r\n")
    with pytest.raises(ValidationError, match="LF"):
        load_campaign_delivery_file(path)


def test_cli_import_campaign_delivery_preserves_verified_output_and_provenance(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    assert run_cli("--json", "init", "--db", str(database)).returncode == 0
    imported = run_cli(
        "--json",
        "import-campaign-delivery",
        "--db",
        str(database),
        "--input",
        str(FIXTURE),
        "--expected-count",
        "2",
    )
    assert imported.returncode == 0, imported.stderr
    result = json.loads(imported.stdout)["result"]
    assert result["count"] == 2
    assert result["imports"][0]["recording_id"] == f"rec_kugou_{'a' * 64}"
    assert result["imports"][0]["provenance_idempotent"] is False

    with MusicKBRepository(database, read_only=True) as repository:
        canonical = repository.get_canonical_analysis(f"rec_kugou_{'a' * 64}")
        status = repository.status()
        assert canonical["analysis"]["raw_text"].endswith("\u2028Keep the arrangement compact.")
        assert canonical["analysis"]["raw_text_truncated"] is False
        assert canonical["campaign_delivery_provenance"] == [
            {
                "delivery_schema_version": 1,
                "campaign_id": "fixture-kugou-20260706",
                "delivery_id": "kg-fixture-0001",
                "manifest_index": 0,
                "relative_audio_path": "audio/0000-neon-night.mp3",
                "source_sha256": "a" * 64,
                "source_bytes": 1234567,
                "output_text_sha256": "f0311a515f31852e256318b941b1a432742df0ace21b46bf89330fc8d2aa66ca",
                "generated_token_count": 993,
                "max_new_tokens": 1400,
                "contract": "fixture-contract-v1",
                "attempt_id": "cnb-fixture-attempt-1",
                "canonical_source": "fixture-kugou-canonical-v1",
                "provenance": {"ledger": "synthetic", "selection": "fixture"},
                "imported_at": canonical["campaign_delivery_provenance"][0]["imported_at"],
            }
        ]
        assert status["counts"]["recordings"] == 2
        assert status["counts"]["campaign_delivery_provenance"] == 2
        assert repository.validate()["valid"] is True


def test_duplicate_audio_rows_become_one_recording_with_source_aliases(tmp_path: Path) -> None:
    records = fixture_records()
    audio_alias = copy.deepcopy(records[0])
    audio_alias.update(
        {
            "id": "kg-fixture-0001-alias",
            "manifest_index": 2,
            "title": "霓虹夜航 (源别名)",
            "artist": "示例乐队别名",
            "relative_audio_path": "audio/0002-neon-night-alias.mp3",
        }
    )
    path = write_jsonl(tmp_path / "audio-alias.jsonl", [*records, audio_alias])
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        result = repository.import_campaign_delivery(load_campaign_delivery_file(path))
        assert result["count"] == 3
        assert result["recording_count"] == 2
        assert result["source_track_count"] == 3
        assert result["imports"][0]["source_track_count"] == 2
        assert repository.status()["counts"]["recordings"] == 2
        assert repository.status()["counts"]["campaign_delivery_provenance"] == 3
        assert repository.connection.execute("SELECT COUNT(*) FROM source_track").fetchone()[0] == 3
        canonical = repository.get_canonical_analysis(f"rec_kugou_{'a' * 64}")
        assert "霓虹夜航 (源别名)" in canonical["title_aliases"]
        assert canonical["artists"] == ["示例乐队", "示例乐队别名"]
        assert [item["delivery_id"] for item in canonical["campaign_delivery_provenance"]] == [
            "kg-fixture-0001",
            "kg-fixture-0001-alias",
        ]
        assert repository.validate()["valid"] is True


def test_idempotent_campaign_import_adds_new_later_source_alias_metadata(tmp_path: Path) -> None:
    records = fixture_records()
    first_path = write_jsonl(tmp_path / "first.jsonl", [records[0]])
    later_alias = copy.deepcopy(records[0])
    later_alias.update(
        {
            "id": "kg-fixture-0001-later-alias",
            "manifest_index": 2,
            "title": "霓虹夜航 (后续别名)",
            "artist": "后续艺人别名",
            "relative_audio_path": "audio/0002-neon-night-later-alias.mp3",
        }
    )
    alias_path = write_jsonl(tmp_path / "later-alias.jsonl", [later_alias])
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_campaign_delivery(load_campaign_delivery_file(first_path))
        result = repository.import_campaign_delivery(load_campaign_delivery_file(alias_path))
        assert result["imports"][0]["idempotent"] is True
        assert result["imports"][0]["provenance_idempotent"] is False
        assert repository.connection.execute("SELECT COUNT(*) FROM source_track").fetchone()[0] == 2
        canonical = repository.get_canonical_analysis(f"rec_kugou_{'a' * 64}")
        assert "霓虹夜航 (后续别名)" in canonical["title_aliases"]
        assert "后续艺人别名" in canonical["artists"]
        assert repository.status()["counts"]["campaign_delivery_provenance"] == 2
        assert repository.validate()["valid"] is True


def test_delivery_import_rejects_conflicting_duplicate_audio_before_any_write(tmp_path: Path) -> None:
    records = fixture_records()
    conflicting_alias = with_output(
        records[0],
        "Different verified model output for the same source audio must not choose a random canonical.",
    )
    conflicting_alias.update(
        {
            "id": "kg-fixture-0001-conflict",
            "manifest_index": 2,
            "relative_audio_path": "audio/0002-neon-night-conflict.mp3",
        }
    )
    path = write_jsonl(tmp_path / "conflicting-audio-alias.jsonl", [*records, conflicting_alias])
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        with pytest.raises(ValidationError, match="identical output_text_sha256"):
            repository.import_campaign_delivery(load_campaign_delivery_file(path))
        assert repository.status()["counts"]["recordings"] == 0
        assert repository.status()["counts"]["campaign_delivery_provenance"] == 0


def test_delivery_import_preserves_exact_output_whitespace_for_hash_audit(tmp_path: Path) -> None:
    record = with_output(
        fixture_records()[0],
        "  Exact model output with intentionally retained surrounding whitespace. \n",
    )
    path = write_jsonl(tmp_path / "exact-output.jsonl", [record])
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_campaign_delivery(load_campaign_delivery_file(path))
    with MusicKBRepository(database, read_only=True) as repository:
        canonical = repository.get_canonical_analysis(f"rec_kugou_{'a' * 64}")
        assert canonical["analysis"]["raw_text"] == record["output_text"]
        assert canonical["campaign_delivery_provenance"][0]["output_text_sha256"] == record[
            "output_text_sha256"
        ]
        assert repository.validate()["valid"] is True


def test_snapshot_keeps_only_provenance_for_canonical_campaign_analysis(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    records = fixture_records()
    replacement = with_output(records[0], "Synthetic canonical replacement for snapshot pruning.")
    replacement.update(
        {
            "contract": "fixture-contract-v2",
            "attempt_id": "cnb-fixture-attempt-2",
            "canonical_source": "fixture-kugou-canonical-v2",
        }
    )
    replacement_path = write_jsonl(tmp_path / "replacement.jsonl", [replacement])
    with MusicKBRepository(database) as repository:
        repository.import_campaign_delivery(load_campaign_delivery_file(FIXTURE))
        repository.import_campaign_delivery(load_campaign_delivery_file(replacement_path))

    release = create_snapshot(database, tmp_path / "releases", release_name="fixture-release")
    with MusicKBRepository(release["database"], read_only=True) as snapshot:
        canonical = snapshot.get_canonical_analysis(f"rec_kugou_{'a' * 64}")
        assert canonical["analysis"]["raw_text"] == replacement["output_text"]
        assert [item["canonical_source"] for item in canonical["campaign_delivery_provenance"]] == [
            "fixture-kugou-canonical-v2"
        ]
        assert snapshot.status()["counts"]["campaign_delivery_provenance"] == 2
        assert snapshot.validate()["valid"] is True


def test_delivery_import_is_idempotent_and_database_conflicts_rollback_entire_batch(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    entries = load_campaign_delivery_file(FIXTURE)
    with MusicKBRepository(database) as repository:
        first = repository.import_campaign_delivery(entries)
        second = repository.import_campaign_delivery(entries)
        assert first["imports"][0]["idempotent"] is False
        assert second["imports"][0]["idempotent"] is True
        assert second["imports"][0]["provenance_idempotent"] is True

        records = fixture_records()
        replacement = with_output(
            records[0],
            "Synthetic replacement output for the same audio with a more complete verified analysis.",
        )
        replacement.update(
            {
                "contract": "fixture-contract-v2",
                "attempt_id": "cnb-fixture-attempt-2",
                "canonical_source": "fixture-kugou-canonical-v2",
            }
        )
        replacement_path = write_jsonl(tmp_path / "replacement.jsonl", [replacement])
        replacement_result = repository.import_campaign_delivery(
            load_campaign_delivery_file(replacement_path)
        )
        assert replacement_result["imports"][0]["idempotent"] is False
        canonical = repository.get_canonical_analysis(f"rec_kugou_{'a' * 64}")
        assert canonical["analysis"]["raw_text"] == replacement["output_text"]
        assert repository.status()["counts"]["campaign_delivery_provenance"] == 3

        new_entry = with_output(
            records[1],
            "Synthetic new entry with a distinct source and a valid verified output.",
        )
        new_entry.update(
            {
                "id": "kg-fixture-new",
                "manifest_index": 2,
                "relative_audio_path": "audio/0002-new.mp3",
                "source_sha256": "c" * 64,
                "canonical_source": "fixture-kugou-canonical-v2",
            }
        )
        conflict = with_output(records[0], records[0]["output_text"])
        conflict.update(
            {
                # Existing delivery ID paired with different audio is a
                # database conflict. It appears after the new entry so the
                # test proves that the outer delivery transaction rolls back.
                "manifest_index": 3,
                "relative_audio_path": "audio/0003-conflict.mp3",
                "source_sha256": "d" * 64,
                "canonical_source": "fixture-kugou-canonical-v2",
            }
        )
        batch_path = write_jsonl(tmp_path / "database-conflict.jsonl", [new_entry, conflict])
        conflict_entries = load_campaign_delivery_file(batch_path)
        with pytest.raises(ValidationError, match="already associated with different source audio"):
            repository.import_campaign_delivery(conflict_entries)
        assert repository.status()["counts"]["recordings"] == 2
        assert repository.status()["counts"]["campaign_delivery_provenance"] == 3


def test_init_upgrades_a_v1_master_without_converting_a_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE campaign_delivery_provenance")
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    initialize_database(database)
    with MusicKBRepository(database, read_only=True) as repository:
        assert repository.status()["schema_version"] == SCHEMA_VERSION
        assert repository.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'campaign_delivery_provenance'"
        ).fetchone() is not None
