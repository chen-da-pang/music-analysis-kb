from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from music_kb.errors import ReadOnlyError, SnapshotVerificationError, ValidationError
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
    assert installed["previous_current"] is None
    assert installed["current_target"] == "releases/music-kb-2026w29.sqlite"

    second = create_snapshot(master_database, tmp_path / "published", release_name="music-kb-2026w29-r2")
    installed_again = install_snapshot(second["release_dir"], tmp_path / "client")
    assert installed_again["previous_current"] == "releases/music-kb-2026w29.sqlite"
    assert Path(installed_again["current_database"]).resolve().name == "music-kb-2026w29-r2.sqlite"


def test_snapshot_removes_noncanonical_recording_identity_data(master_database, fixture_payload, tmp_path: Path) -> None:
    candidate = copy.deepcopy(fixture_payload)
    candidate["recording"]["id"] = "rec_candidate_only"
    candidate["recording"]["audio_sha256"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    candidate["recording"]["title"] = "未发布候选曲"
    candidate["analysis"]["raw_text"] = "Candidate-only analysis."
    candidate["canonical"] = False
    candidate["source_tracks"][0]["source_track_id"] = "fixture-candidate-001"
    candidate["source_tracks"][0]["source_title"] = "未发布候选曲"
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


def test_failed_install_keeps_previous_current_snapshot(master_database, tmp_path: Path) -> None:
    first = create_snapshot(master_database, tmp_path / "published", release_name="first-release")
    installed = install_snapshot(first["release_dir"], tmp_path / "client")
    current = Path(installed["current_database"])

    second = create_snapshot(master_database, tmp_path / "published", release_name="second-release")
    manifest_path = Path(second["manifest"])
    os.chmod(manifest_path, 0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotVerificationError, match="SHA-256 mismatch"):
        install_snapshot(second["release_dir"], tmp_path / "client")

    assert current.is_symlink()
    assert current.resolve().name == "first-release.sqlite"


def test_install_retries_after_readonly_partial_files(master_database, tmp_path: Path) -> None:
    release = create_snapshot(master_database, tmp_path / "published", release_name="retry-release")
    target = tmp_path / "client"
    incoming = target / "incoming"
    incoming.mkdir(parents=True)
    database_partial = incoming / "retry-release.sqlite.partial"
    manifest_partial = incoming / "retry-release.manifest.json.partial"
    database_partial.write_bytes(b"stale partial database")
    manifest_partial.write_text("stale partial manifest", encoding="utf-8")
    os.chmod(database_partial, 0o444)
    os.chmod(manifest_partial, 0o444)

    installed = install_snapshot(release["release_dir"], target)

    assert Path(installed["current_database"]).is_symlink()
    assert Path(installed["current_database"]).resolve().name == "retry-release.sqlite"


@pytest.mark.parametrize("release_name", ["release;id #", "release name", "../outside", ".", "-starts-with-dash"])
def test_snapshot_release_name_rejects_shell_unsafe_values(master_database, tmp_path: Path, release_name: str) -> None:
    with pytest.raises(ValidationError, match="release name"):
        create_snapshot(master_database, tmp_path / "published", release_name=release_name)


def test_verify_rejects_shell_unsafe_manifest_release_name(master_database, tmp_path: Path) -> None:
    release = create_snapshot(master_database, tmp_path / "published", release_name="safe-release")
    manifest_path = Path(release["manifest"])
    os.chmod(manifest_path, 0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_name"] = "release;id #"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotVerificationError, match="release name is unsafe"):
        verify_snapshot(manifest_path)


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
