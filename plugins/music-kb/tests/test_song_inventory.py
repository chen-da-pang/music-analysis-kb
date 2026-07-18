from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from build_song_inventory import audio_retention_summary


def test_inventory_rebuild_preserves_purge_audit_fields() -> None:
    songs = [
        {"download": {"retention": "purged_after_analysis", "purged_at": "2026-07-10T00:00:00Z"}},
        {"download": {"retention": "purged_after_analysis", "purged_at": "2026-07-10T00:00:00Z"}},
    ]
    assert audio_retention_summary(
        {"audio_retention": "purged_after_analysis", "audio_files_deleted_at": "2026-07-10T00:00:00Z"},
        songs,
    ) == {
        "audio_retention": "purged_after_analysis",
        "audio_files_deleted_at": "2026-07-10T00:00:00Z",
    }


def test_inventory_rebuild_marks_new_retained_audio() -> None:
    songs = [
        {"download": {"retention": "purged_after_analysis"}},
        {"download": {"retention": "retained"}},
    ]
    assert audio_retention_summary({}, songs) == {
        "audio_retention": "retained",
        "audio_files_deleted_at": None,
    }
