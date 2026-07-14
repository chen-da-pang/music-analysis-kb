from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from music_kb.distribution import load_distribution_peers, publish_snapshot
from music_kb.errors import ValidationError
from music_kb.snapshot import create_snapshot


def _write_peers(
    tmp_path: Path,
    *,
    key: Path,
    peers: tuple[tuple[str, str], ...] = (("first-mac", "first.example.test"),),
    target_dir: str = "~/.music-kb",
) -> Path:
    rows = [
        "version = 1",
        "",
        "[defaults]",
        f'identity_file = "{key}"',
        f'target_dir = "{target_dir}"',
        "port = 2222",
        "connect_timeout_seconds = 7",
        "command_timeout_seconds = 33",
    ]
    for name, host in peers:
        rows.extend(
            [
                "",
                "[[peers]]",
                f'name = "{name}"',
                f'host = "{host}"',
                'user = "music-user"',
            ]
        )
    path = tmp_path / "peers.toml"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _release(master_database: Path, tmp_path: Path) -> Path:
    return Path(
        create_snapshot(master_database, tmp_path / "published", release_name="distribution-fixture")["release_dir"]
    )


class RecordingRunner:
    def __init__(
        self,
        *,
        failed_host: str | None = None,
        failed_stage: str | None = None,
        timeout_stage: str | None = None,
    ) -> None:
        self.commands: list[tuple[list[str], int]] = []
        self.failed_host = failed_host
        self.failed_stage = failed_stage
        self.timeout_stage = timeout_stage

    @staticmethod
    def stage_name(command: list[str]) -> str:
        if command[0] == "rsync":
            return "rsync"
        remote = command[-1]
        if " --help >/dev/null" in remote:
            return "preflight"
        if "mkdir -p" in remote:
            return "mkdir"
        if "snapshot verify" in remote:
            return "verify"
        if "snapshot install" in remote:
            return "install"
        raise AssertionError(f"Unknown command stage: {command}")

    def __call__(self, command: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        copied = list(command)
        self.commands.append((copied, timeout_seconds))
        rendered = " ".join(copied)
        stage = self.stage_name(copied)
        selected_host = self.failed_host is None or self.failed_host in rendered
        if selected_host and self.timeout_stage == stage:
            raise subprocess.TimeoutExpired(copied, timeout_seconds, output="partial output", stderr="timed out")
        if selected_host and self.failed_stage == stage:
            return subprocess.CompletedProcess(copied, 1, stdout="", stderr=f"{stage} failed")
        if (
            self.failed_host
            and self.failed_stage is None
            and self.timeout_stage is None
            and self.failed_host in rendered
        ):
            return subprocess.CompletedProcess(copied, 255, stdout="", stderr="Connection timed out")
        return subprocess.CompletedProcess(copied, 0, stdout="ok", stderr="")


def test_publish_dry_run_verifies_release_but_never_invokes_transport(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(tmp_path, key=key)
    release = _release(master_database, tmp_path)

    def must_not_run(command: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"transport was called for dry run: {command} {timeout_seconds}")

    result = publish_snapshot(release, peers, dry_run=True, runner=must_not_run)

    assert result["dry_run"] is True
    assert result["failed_count"] == 0
    assert result["peers"] == [
        {
            "name": "first-mac",
            "host": "first.example.test",
            "incoming_dir": "~/.music-kb/incoming/distribution-fixture",
            "target_dir": "~/.music-kb",
            "stages": [],
            "status": "planned",
        }
    ]


def test_publish_stages_then_verifies_and_installs_without_inplace(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(tmp_path, key=key)
    release = _release(master_database, tmp_path)
    runner = RecordingRunner()

    result = publish_snapshot(release, peers, runner=runner)

    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 0
    commands = [command for command, _ in runner.commands]
    assert [command[0] for command in commands] == ["ssh", "ssh", "rsync", "ssh", "ssh"]
    assert [timeout for _, timeout in runner.commands] == [33, 33, 33, 33, 33]
    preflight, mkdir, rsync, verify, install = commands
    assert "BatchMode=yes" in mkdir
    assert "StrictHostKeyChecking=yes" in mkdir
    assert "ConnectTimeout=7" in mkdir
    assert "-i" in mkdir and str(key.resolve()) in mkdir
    assert preflight[-1] == (
        'set -eu; test -x "$HOME"/.local/bin/music-kb; '
        '"$HOME"/.local/bin/music-kb --help >/dev/null'
    )
    assert mkdir[-1] == 'set -eu; mkdir -p "$HOME"/.music-kb/incoming/distribution-fixture'
    assert "--inplace" not in rsync
    assert "--partial" in rsync
    assert "--checksum" in rsync
    assert rsync[-1] == "music-user@first.example.test:~/.music-kb/incoming/distribution-fixture/"
    assert "music-master.sqlite" not in " ".join(rsync)
    assert verify[-1] == (
        'set -eu; "$HOME"/.local/bin/music-kb snapshot verify '
        '--manifest "$HOME"/.music-kb/incoming/distribution-fixture/manifest.json'
    )
    assert install[-1] == (
        'set -eu; "$HOME"/.local/bin/music-kb snapshot install --release-dir '
        '"$HOME"/.music-kb/incoming/distribution-fixture --target-dir "$HOME"/.music-kb'
    )
    assert [stage["name"] for stage in result["peers"][0]["stages"]] == [
        "preflight",
        "mkdir",
        "rsync",
        "verify",
        "install",
    ]


def test_one_failed_peer_does_not_block_other_peers(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(
        tmp_path,
        key=key,
        peers=(("offline-mac", "offline.example.test"), ("online-mac", "online.example.test")),
    )
    release = _release(master_database, tmp_path)
    runner = RecordingRunner(failed_host="offline.example.test")

    result = publish_snapshot(release, peers, runner=runner)

    assert result["failed_count"] == 1
    assert result["succeeded_count"] == 1
    assert result["peers"][0]["status"] == "failed"
    assert result["peers"][0]["stages"] == [
        {"name": "preflight", "ok": False, "returncode": 255, "stderr": "Connection timed out"}
    ]
    assert result["peers"][1]["status"] == "succeeded"
    assert len(runner.commands) == 6


def test_rsync_failure_stops_that_peer_but_continues_to_another_peer(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(
        tmp_path,
        key=key,
        peers=(("first-mac", "first.example.test"), ("second-mac", "second.example.test")),
    )
    runner = RecordingRunner(failed_host="first.example.test", failed_stage="rsync")

    result = publish_snapshot(_release(master_database, tmp_path), peers, runner=runner)

    assert result["failed_count"] == 1
    assert result["succeeded_count"] == 1
    assert [stage["name"] for stage in result["peers"][0]["stages"]] == [
        "preflight",
        "mkdir",
        "rsync",
    ]
    assert [stage["name"] for stage in result["peers"][1]["stages"]] == [
        "preflight",
        "mkdir",
        "rsync",
        "verify",
        "install",
    ]
    assert len(runner.commands) == 8


def test_timeout_stops_later_stages_for_that_peer(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(tmp_path, key=key)
    runner = RecordingRunner(failed_host="first.example.test", timeout_stage="verify")

    result = publish_snapshot(_release(master_database, tmp_path), peers, runner=runner)

    stages = result["peers"][0]["stages"]
    assert [stage["name"] for stage in stages] == ["preflight", "mkdir", "rsync", "verify"]
    assert stages[-1]["error"] == "timeout"


def test_private_peer_config_rejects_unsafe_rsync_path_and_unknown_selection(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(tmp_path, key=key, target_dir="relative; rm -rf /")
    with pytest.raises(ValidationError, match="target_dir"):
        load_distribution_peers(peers)

    peers = _write_peers(tmp_path, key=key, target_dir="~/.music-kb/../outside")
    with pytest.raises(ValidationError, match="must not contain"):
        load_distribution_peers(peers)

    valid = _write_peers(tmp_path, key=key, target_dir="~/.music-kb")
    release = _release(master_database, tmp_path)
    with pytest.raises(ValidationError, match="Requested peer"):
        publish_snapshot(release, valid, peer_names=["missing"])
