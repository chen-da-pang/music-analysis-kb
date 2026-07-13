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


@pytest.fixture()
def master_database(tmp_path: Path, fixture_payload: dict) -> Path:
    database = tmp_path / "music-master.sqlite"
    initialize_database(database)
    with MusicKBRepository(database) as repository:
        repository.import_analysis(fixture_payload)
    return database
