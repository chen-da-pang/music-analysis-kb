from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_kb.repository import MusicKBRepository
from music_kb.schema import initialize_database


FIXTURE = Path(__file__).parent / "fixtures" / "analysis.json"


@pytest.fixture()
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def seed_available_lyrics(repository: MusicKBRepository) -> None:
    """Make a generic fixture database publishable without hiding lyric tests.

    Most pre-#52 tests exercise snapshot, distribution, or retrieval behavior
    and intentionally do not care about lyric wording.  A small synthetic
    terminal result keeps those tests focused while dedicated lyric tests use
    ``unresolved_master_database`` below.
    """

    rows = list(
        repository.connection.execute(
            """
            SELECT r.id AS recording_id, st.id AS source_track_row_id,
                   st.source_name, st.source_track_id
            FROM recording r
            JOIN source_track st ON st.recording_id = r.id
            WHERE r.canonical_analysis_id IS NOT NULL
            ORDER BY r.id, st.source_name, st.source_track_id, st.id
            """
        )
    )
    seen: set[str] = set()
    for row in rows:
        recording_id = str(row["recording_id"])
        if recording_id in seen:
            continue
        seen.add(recording_id)
        repository.import_lyric(
            {
                "recording_id": recording_id,
                "source_track_row_id": str(row["source_track_row_id"]),
                "status": "available",
                "lyric_text": f"Synthetic fixture lyric for {recording_id}.",
                "evidence": {
                    "source_name": str(row["source_name"]),
                    "source_track_id": str(row["source_track_id"]),
                    "reason": "synthetic test fixture lyric",
                    "query_method": "fixture_seed",
                },
            }
        )


@pytest.fixture()
def master_database(tmp_path: Path, fixture_payload: dict) -> Path:
    database = tmp_path / "music-master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_analysis(fixture_payload)
        seed_available_lyrics(repository)
    return database


@pytest.fixture()
def unresolved_master_database(tmp_path: Path, fixture_payload: dict) -> Path:
    """Canonical fixture data with no lyric row for coverage-gate tests."""

    database = tmp_path / "music-master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_analysis(fixture_payload)
    return database


@pytest.fixture()
def lyric_seed():
    """Expose the focused synthetic seed to integration tests with custom DBs."""

    return seed_available_lyrics


@pytest.fixture()
def lyric_receipt_writer(tmp_path: Path):
    """Build a synthetic CC receipt file for workflow-level tests."""

    def write(name: str, source_tracks: list[tuple[str, str]]) -> Path:
        path = tmp_path / f"{name}.lyrics.jsonl"
        rows = []
        for index, (source_name, source_track_id) in enumerate(source_tracks, start=1):
            rows.append(
                {
                    "schema_version": 1,
                    "source_name": source_name,
                    "source_track_id": source_track_id,
                    "status": "available",
                    "lyric_text": f"Synthetic workflow lyric {index}.",
                    "evidence": {
                        "source_name": source_name,
                        "source_track_id": source_track_id,
                        "reason": "synthetic workflow lyric receipt",
                        "query_method": "fixture_receipt",
                    },
                }
            )
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    return write
