from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from music_kb.errors import ReadOnlyError, SnapshotVerificationError
from music_kb.repository import MusicKBRepository
from music_kb.snapshot import create_snapshot, install_snapshot, verify_snapshot


def test_snapshot_is_canonical_only_and_installs_atomically(master_database, fixture_payload, tmp_path: Path) -> None:
    replacement = copy.deepcopy(fixture_payload)
    replacement["analysis"]["raw_text"] = "Replacement public analysis."
    replacement["tags"] = [
        {
            "namespace": "texture",
            "name": "soft diffusion pad",
            "aliases": ["柔和扩散铺底"],
            "status": "approved",
            "suno_safe": True,
        }
    ]
    with MusicKBRepository(master_database) as repository:
        repository.import_analysis(replacement)
    release = create_snapshot(master_database, tmp_path / "published", release_name="music-kb-2026w29")
    verified = verify_snapshot(release["manifest"])
    assert verified["valid"]
    with MusicKBRepository(release["database"], read_only=True) as snapshot:
        assert snapshot.status()["metadata"]["database_kind"] == "snapshot"
        assert snapshot.status()["counts"]["analysis_revisions"] == 1
        assert snapshot.get_canonical_analysis("rec_neon_night_studio")["analysis"]["raw_text"] == "Replacement public analysis."
        assert snapshot.tag_facets(prefix="syncopated rimshot") == []
    installed = install_snapshot(release["release_dir"], tmp_path / "client")
    current = Path(installed["current_database"])
    assert current.is_symlink()
    assert current.resolve().name == "music-kb-2026w29.sqlite"


def test_snapshot_removes_noncanonical_recording_identity_data(master_database, fixture_payload, tmp_path: Path) -> None:
    candidate = copy.deepcopy(fixture_payload)
    candidate["recording"]["id"] = "rec_candidate_only"
    candidate["recording"]["audio_sha256"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    candidate["recording"]["title"] = "未发布候选曲"
    candidate["analysis"]["raw_text"] = "Candidate-only analysis."
    candidate["canonical"] = False
    candidate["tags"] = [
        {
            "namespace": "production",
            "name": "secret candidate texture",
            "status": "candidate",
        }
    ]
    with MusicKBRepository(master_database) as repository:
        repository.import_analysis(candidate)
    release = create_snapshot(master_database, tmp_path / "published", release_name="music-kb-2026w31")
    with MusicKBRepository(release["database"], read_only=True) as snapshot:
        assert snapshot.status()["counts"]["recordings"] == 1
        assert snapshot.search(title="未发布候选曲") == []
        assert snapshot.tag_facets(prefix="secret candidate texture") == []


def test_verify_rejects_tampered_database(master_database, tmp_path: Path) -> None:
    release = create_snapshot(master_database, tmp_path / "published", release_name="music-kb-2026w30")
    database = Path(release["database"])
    os.chmod(database, 0o644)
    with database.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(SnapshotVerificationError, match="SHA-256 mismatch"):
        verify_snapshot(release["manifest"])


def test_client_snapshot_rejects_writer_operations_even_if_file_is_made_writable(
    master_database, fixture_payload, tmp_path: Path
) -> None:
    release = create_snapshot(master_database, tmp_path / "published", release_name="music-kb-2026w32")
    database = Path(release["database"])
    os.chmod(database, 0o644)
    payload = copy.deepcopy(fixture_payload)
    payload["analysis"]["raw_text"] = "This must never write to a client snapshot."
    with MusicKBRepository(database) as snapshot:
        with pytest.raises(ReadOnlyError, match="never valid write targets"):
            snapshot.import_analysis(payload)
