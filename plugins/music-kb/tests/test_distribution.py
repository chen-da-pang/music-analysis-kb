from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest

import music_kb.distribution as distribution
from music_kb.distribution import (
    _REMOTE_COMPATIBILITY_CODE,
    _REMOTE_INSTALL_CODE,
    _REMOTE_VERIFY_CODE,
    load_distribution_peers,
    publish_snapshot,
)
from music_kb.errors import ValidationError
from music_kb.snapshot import create_snapshot, verify_snapshot


def test_current_plugin_version_uses_the_shipped_default_when_metadata_is_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(distribution, "PLUGIN_MANIFEST_PATH", tmp_path / "missing-plugin.json")

    def no_package(_name: str) -> str:
        raise distribution.PackageNotFoundError

    monkeypatch.setattr(distribution, "package_version", no_package)

    assert distribution._current_plugin_version() == "0.8.5"


def _write_peers(
    tmp_path: Path,
    *,
    key: Path,
    peers: tuple[tuple[str, str], ...] = (("first-mac", "first.example.test"),),
    target_dir: str = "~/.music-kb",
    enabled: tuple[bool, ...] | None = None,
    version: int = 1,
) -> Path:
    rows = [
        f"version = {version}",
        "",
        "[defaults]",
        f'identity_file = "{key}"',
        f'target_dir = "{target_dir}"',
        "port = 2222",
        "connect_timeout_seconds = 7",
        "command_timeout_seconds = 33",
    ]
    for index, (name, host) in enumerate(peers):
        rows.extend(
            [
                "",
                "[[peers]]",
                f'name = "{name}"',
                f"enabled = {str(enabled[index] if enabled is not None else True).lower()}",
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
        if "import hashlib, json, sqlite3" in remote:
            return "preflight"
        if "music-kb plugin/schema incompatible" in remote:
            return "plugin_compatibility"
        if "mkdir -p" in remote:
            return "mkdir"
        if "PRAGMA integrity_check" in remote and "current.sqlite" not in remote:
            return "verify"
        if "current.sqlite" in remote:
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
    assert [command[0] for command in commands] == ["ssh", "ssh", "ssh", "rsync", "ssh", "ssh"]
    assert [timeout for _, timeout in runner.commands] == [33, 33, 33, 33, 33, 33]
    preflight, compatibility, mkdir, rsync, verify, install = commands
    assert "BatchMode=yes" in mkdir
    assert "StrictHostKeyChecking=yes" in mkdir
    assert "ConnectTimeout=7" in mkdir
    assert "-i" in mkdir and str(key.resolve()) in mkdir
    assert preflight[-1] == "set -eu; python3 -c 'import hashlib, json, sqlite3'"
    assert "required_schema_version" in compatibility[-1]
    assert mkdir[-1] == 'set -eu; mkdir -p "$HOME"/.music-kb/incoming/distribution-fixture'
    assert "--inplace" not in rsync
    assert "--partial" in rsync
    assert "--checksum" in rsync
    assert rsync[-1] == "music-user@first.example.test:~/.music-kb/incoming/distribution-fixture/"
    assert "music-master.sqlite" not in " ".join(rsync)
    assert "PRAGMA integrity_check" in verify[-1]
    assert "manifest.json" in verify[-1]
    assert "current.sqlite" in install[-1]
    assert "snapshot install" not in install[-1]
    rendered_commands = " ".join(" ".join(command) for command in commands)
    assert "music-kb --help" not in rendered_commands
    assert "snapshot verify" not in rendered_commands
    assert "snapshot install" not in rendered_commands
    assert [stage["name"] for stage in result["peers"][0]["stages"]] == [
        "preflight",
        "plugin_compatibility",
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
    assert len(runner.commands) == 7


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
        "plugin_compatibility",
        "mkdir",
        "rsync",
    ]
    assert [stage["name"] for stage in result["peers"][1]["stages"]] == [
        "preflight",
        "plugin_compatibility",
        "mkdir",
        "rsync",
        "verify",
        "install",
    ]
    assert len(runner.commands) == 10


def test_timeout_stops_later_stages_for_that_peer(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(tmp_path, key=key)
    runner = RecordingRunner(failed_host="first.example.test", timeout_stage="verify")

    result = publish_snapshot(_release(master_database, tmp_path), peers, runner=runner)

    stages = result["peers"][0]["stages"]
    assert [stage["name"] for stage in stages] == ["preflight", "plugin_compatibility", "mkdir", "rsync", "verify"]
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


def test_disabled_peers_are_excluded_from_all_peer_publish_but_explicit_retry_can_target_them(
    master_database, tmp_path: Path
) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(
        tmp_path,
        key=key,
        peers=(("enabled-mac", "enabled.example.test"), ("paused-mac", "paused.example.test")),
        enabled=(True, False),
    )
    release = _release(master_database, tmp_path)

    all_peers = publish_snapshot(release, peers, dry_run=True)
    assert [peer["name"] for peer in all_peers["peers"]] == ["enabled-mac"]

    explicit = publish_snapshot(release, peers, peer_names=["paused-mac"], dry_run=True)
    assert [peer["name"] for peer in explicit["peers"]] == ["paused-mac"]


def test_peer_config_v1_remains_compatible(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(tmp_path, key=key, version=1)
    text = peers.read_text(encoding="utf-8").replace("enabled = true\n", "")
    peers.write_text(text, encoding="utf-8")

    loaded = load_distribution_peers(peers)
    assert loaded[0].enabled is True


def test_peer_config_rejects_non_boolean_enabled(master_database, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("fixture key", encoding="utf-8")
    peers = _write_peers(tmp_path, key=key, version=2)
    text = peers.read_text(encoding="utf-8").replace("enabled = true\n", 'enabled = "yes"\n')
    peers.write_text(text, encoding="utf-8")

    with pytest.raises(ValidationError, match="enabled must be a boolean"):
        load_distribution_peers(peers)


def test_remote_plugin_compatibility_script_accepts_matching_cache(tmp_path: Path) -> None:
    install = tmp_path / "0.8.0"
    (install / ".codex-plugin").mkdir(parents=True)
    (install / "src" / "music_kb").mkdir(parents=True)
    (install / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"music-kb","version":"0.8.0"}\n', encoding="utf-8"
    )
    (install / "src" / "music_kb" / "schema.py").write_text("SCHEMA_VERSION = 7\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _REMOTE_COMPATIBILITY_CODE, str(tmp_path), "0.8.0", "7"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"compatible": true' in result.stdout


def test_remote_plugin_compatibility_script_rejects_old_schema(tmp_path: Path) -> None:
    install = tmp_path / "0.6.0"
    (install / ".codex-plugin").mkdir(parents=True)
    (install / "src" / "music_kb").mkdir(parents=True)
    (install / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"music-kb","version":"0.6.0"}\n', encoding="utf-8"
    )
    (install / "src" / "music_kb" / "schema.py").write_text("SCHEMA_VERSION = 5\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _REMOTE_COMPATIBILITY_CODE, str(tmp_path), "0.8.0", "7"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "incompatible" in result.stderr


def test_self_contained_remote_scripts_verify_and_atomically_install_snapshot(master_database, tmp_path: Path) -> None:
    release = _release(master_database, tmp_path)
    client = tmp_path / "client"

    subprocess.run(
        [sys.executable, "-c", _REMOTE_VERIFY_CODE, str(release / "manifest.json")],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-c", _REMOTE_INSTALL_CODE, str(release), str(client)],
        check=True,
    )

    current = client / "current.sqlite"
    installed_manifest = client / "releases" / "distribution-fixture.manifest.json"
    assert current.is_symlink()
    assert current.resolve().name == "distribution-fixture.sqlite"
    assert verify_snapshot(installed_manifest)["valid"] is True
