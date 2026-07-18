from __future__ import annotations

from music_kb.weekly_orchestration import _cleanup_gate_satisfied


def test_cleanup_gate_requires_publish_and_release() -> None:
    assert not _cleanup_gate_satisfied(
        publish=False,
        release_result={"release_dir": "/tmp/release"},
        skip_peers=True,
        publish_result={},
    )
    assert not _cleanup_gate_satisfied(
        publish=True,
        release_result=None,
        skip_peers=True,
        publish_result={},
    )


def test_cleanup_gate_accepts_explicit_peer_skip_after_release() -> None:
    assert _cleanup_gate_satisfied(
        publish=True,
        release_result={"release_dir": "/tmp/release"},
        skip_peers=True,
        publish_result={"peer_count": 0, "failed_count": 0},
    )


def test_cleanup_gate_requires_all_selected_peers_without_skip() -> None:
    release = {"release_dir": "/tmp/release"}
    assert _cleanup_gate_satisfied(
        publish=True,
        release_result=release,
        skip_peers=False,
        publish_result={"peer_count": 2, "failed_count": 0},
    )
    assert not _cleanup_gate_satisfied(
        publish=True,
        release_result=release,
        skip_peers=False,
        publish_result={"peer_count": 2, "failed_count": 1},
    )
    assert not _cleanup_gate_satisfied(
        publish=True,
        release_result=release,
        skip_peers=False,
        publish_result={"peer_count": 0, "failed_count": 0},
    )
