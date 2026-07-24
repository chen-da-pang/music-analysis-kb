from __future__ import annotations

import shlex
import subprocess
import tomllib
import json
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import ValidationError
from .schema import SCHEMA_VERSION
from .snapshot import validate_release_name, verify_snapshot


PEER_CONFIG_VERSION = 2
SUPPORTED_PEER_CONFIG_VERSIONS = {1, 2}
DEFAULT_TARGET_DIR = "~/.music-kb"
DEFAULT_PYTHON_PATH = "python3"
DEFAULT_PORT = 22
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
DEFAULT_PLUGIN_CACHE_ROOT = "~/.codex/plugins/cache/music-analysis-kb/music-kb"
MAX_OUTPUT_CHARS = 2_000
DEFAULT_PLUGIN_VERSION = "0.8.5"
PLUGIN_MANIFEST_PATH = Path(__file__).resolve().parents[2] / ".codex-plugin" / "plugin.json"

CommandRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class DistributionPeer:
    """A private, publisher-side SSH destination."""

    name: str
    host: str
    user: str
    port: int
    identity_file: Path | None
    target_dir: str
    cli_path: str | None
    plugin_cache_root: str
    python_path: str
    connect_timeout_seconds: int
    command_timeout_seconds: int
    enabled: bool

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}"


def _require_string(value: object, *, field: str, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}.{field} must be a non-empty string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValidationError(f"{context}.{field} contains an unsafe control character")
    return value.strip()


def _optional_string(value: object, *, field: str, context: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field=field, context=context)


def _positive_int(value: object, *, field: str, context: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationError(f"{context}.{field} must be an integer between 1 and {maximum}")
    return value


def _boolean(value: object, *, field: str, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{context}.{field} must be a boolean")
    return value


def _simple_name(value: str, *, context: str) -> str:
    if not value[0].isalnum() or any(not (character.isalnum() or character in "._-") for character in value):
        raise ValidationError(f"{context}.name must use only letters, numbers, '.', '_' or '-'")
    return value


def _host_or_user(value: str, *, field: str, context: str) -> str:
    if value.startswith("-") or any(character.isspace() or character in "@:/\\" for character in value):
        raise ValidationError(f"{context}.{field} is not a safe SSH {field}")
    return value


def _remote_path_setting(value: str, *, field: str, context: str) -> str:
    if not (value == "~" or value.startswith("~/") or value.startswith("/")):
        raise ValidationError(f"{context}.{field} must be an absolute path or start with '~/'")
    if any(not (character.isalnum() or character in "._~/-") for character in value):
        raise ValidationError(f"{context}.{field} contains unsafe characters")
    normalized = value.rstrip("/") or "/"
    if any(segment in {".", ".."} for segment in normalized.split("/")):
        raise ValidationError(f"{context}.{field} must not contain '.' or '..' segments")
    if normalized in {"~", "/"}:
        raise ValidationError(f"{context}.{field} must name a directory or executable below a home/root path")
    return normalized


def _remote_executable_setting(value: str, *, field: str, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}.{field} must be a non-empty executable path")
    value = value.strip()
    if value.startswith("-") or any(character.isspace() or character in "'\";$`()|&<>\\" for character in value):
        raise ValidationError(f"{context}.{field} contains unsafe shell characters")
    if any(not (character.isalnum() or character in "._~/-") for character in value):
        raise ValidationError(f"{context}.{field} contains unsafe characters")
    return value


def _current_plugin_version() -> str:
    try:
        manifest = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))
        version = str(manifest.get("version") or "").strip()
        if version:
            return version
    except (OSError, ValueError, TypeError):
        pass
    try:
        return package_version("music-kb")
    except PackageNotFoundError:
        return DEFAULT_PLUGIN_VERSION


def _identity_file(value: str | None, *, context: str) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValidationError(f"{context}.identity_file does not exist or is not a file: {path}")
    return path.resolve()


def _mapping(value: object, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{context} must be a TOML table")
    return value


def _get_setting(peer: dict[str, Any], defaults: dict[str, Any], name: str, default: object) -> object:
    return peer.get(name, defaults.get(name, default))


def load_distribution_peers(config_path: str | Path) -> list[DistributionPeer]:
    """Load a private TOML inventory without exposing it to the repository."""

    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ValidationError(f"Peer config does not exist: {path}")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"Peer config is not valid TOML: {exc}") from exc

    version = raw.get("version")
    if version not in SUPPORTED_PEER_CONFIG_VERSIONS:
        raise ValidationError(
            f"Peer config version must be one of {sorted(SUPPORTED_PEER_CONFIG_VERSIONS)}",
            details={"actual": version},
        )
    defaults_value = raw.get("defaults", {})
    defaults = _mapping(defaults_value, context="defaults")
    peer_values = raw.get("peers")
    if not isinstance(peer_values, list) or not peer_values:
        raise ValidationError("peers must be a non-empty TOML array of tables")

    peers: list[DistributionPeer] = []
    names: set[str] = set()
    for index, value in enumerate(peer_values, 1):
        context = f"peers[{index}]"
        peer = _mapping(value, context=context)
        name = _simple_name(_require_string(peer.get("name"), field="name", context=context), context=context)
        if name in names:
            raise ValidationError(f"Duplicate peer name: {name}")
        names.add(name)
        host = _host_or_user(
            _require_string(peer.get("host"), field="host", context=context), field="host", context=context
        )
        user = _host_or_user(
            _require_string(peer.get("user"), field="user", context=context), field="user", context=context
        )
        target_dir = _remote_path_setting(
            _require_string(
                _get_setting(peer, defaults, "target_dir", DEFAULT_TARGET_DIR),
                field="target_dir",
                context=context,
            ),
            field="target_dir",
            context=context,
        )
        # Keep accepting the v1 field so old inventories remain readable; the
        # self-contained remote installer deliberately never executes it.
        raw_cli_path = _optional_string(
            _get_setting(peer, defaults, "cli_path", None), field="cli_path", context=context
        )
        cli_path = (
            _remote_path_setting(raw_cli_path, field="cli_path", context=context)
            if raw_cli_path is not None
            else None
        )
        python_path = _remote_executable_setting(
            _require_string(
                _get_setting(peer, defaults, "python_path", DEFAULT_PYTHON_PATH),
                field="python_path",
                context=context,
            ),
            field="python_path",
            context=context,
        )
        plugin_cache_root = _remote_path_setting(
            _require_string(
                _get_setting(peer, defaults, "plugin_cache_root", DEFAULT_PLUGIN_CACHE_ROOT),
                field="plugin_cache_root",
                context=context,
            ),
            field="plugin_cache_root",
            context=context,
        )
        identity = _optional_string(
            _get_setting(peer, defaults, "identity_file", None), field="identity_file", context=context
        )
        peers.append(
            DistributionPeer(
                name=name,
                host=host,
                user=user,
                port=_positive_int(
                    _get_setting(peer, defaults, "port", DEFAULT_PORT),
                    field="port",
                    context=context,
                    maximum=65_535,
                ),
                identity_file=_identity_file(identity, context=context),
                target_dir=target_dir,
                cli_path=cli_path,
                plugin_cache_root=plugin_cache_root,
                python_path=python_path,
                connect_timeout_seconds=_positive_int(
                    _get_setting(
                        peer, defaults, "connect_timeout_seconds", DEFAULT_CONNECT_TIMEOUT_SECONDS
                    ),
                    field="connect_timeout_seconds",
                    context=context,
                    maximum=3_600,
                ),
                command_timeout_seconds=_positive_int(
                    _get_setting(peer, defaults, "command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS),
                    field="command_timeout_seconds",
                    context=context,
                    maximum=86_400,
                ),
                enabled=_boolean(
                    _get_setting(peer, defaults, "enabled", True),
                    field="enabled",
                    context=context,
                ),
            )
        )
    return peers


def _select_peers(peers: list[DistributionPeer], requested_names: Sequence[str]) -> list[DistributionPeer]:
    if not requested_names:
        return [peer for peer in peers if peer.enabled]
    requested = set(requested_names)
    available = {peer.name for peer in peers}
    unknown = sorted(requested - available)
    if unknown:
        raise ValidationError("Requested peer does not exist in config", details={"unknown_peers": unknown})
    return [peer for peer in peers if peer.name in requested]


def _remote_join(base: str, *parts: str) -> str:
    suffix = "/".join(parts)
    if base == "/":
        return f"/{suffix}"
    return f"{base.rstrip('/')}/{suffix}"


def _remote_shell_path(path: str) -> str:
    """Return a POSIX-shell-safe remote path while retaining only $HOME expansion."""

    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _ssh_transport(peer: DistributionPeer) -> list[str]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={peer.connect_timeout_seconds}",
        "-p",
        str(peer.port),
    ]
    if peer.identity_file is not None:
        command.extend(["-i", str(peer.identity_file)])
    return command


def _ssh_command(peer: DistributionPeer, remote_command: str) -> list[str]:
    return [*_ssh_transport(peer), peer.ssh_target, remote_command]


def _rsync_command(peer: DistributionPeer, release_dir: Path, incoming_dir: str) -> list[str]:
    return [
        "rsync",
        "-a",
        "--partial",
        "--checksum",
        "-e",
        shlex.join(_ssh_transport(peer)),
        f"{release_dir}/",
        f"{peer.ssh_target}:{incoming_dir}/",
    ]


def _remote_mkdir_command(incoming_dir: str) -> str:
    return f"set -eu; mkdir -p {_remote_shell_path(incoming_dir)}"


_REMOTE_VERIFY_CODE = r"""
import hashlib
import json
import os
import sqlite3
import sys

manifest_path = os.path.abspath(os.path.expanduser(sys.argv[1]))
with open(manifest_path, encoding="utf-8") as handle:
    manifest = json.load(handle)
release_name = manifest["release_name"]
if not isinstance(release_name, str) or not release_name or any(
    character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    for character in release_name
):
    raise SystemExit("unsafe release name")
filename = manifest["database"]["filename"]
if os.path.basename(filename) != filename or not filename.endswith(".sqlite"):
    raise SystemExit("unsafe database filename")
database_path = os.path.join(os.path.dirname(manifest_path), filename)
expected = manifest["database"]["sha256"]
digest = hashlib.sha256()
with open(database_path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected:
    raise SystemExit("snapshot SHA-256 mismatch")
connection = sqlite3.connect("file:" + database_path + "?mode=ro", uri=True)
try:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        raise SystemExit("SQLite integrity check failed")
    database_kind = connection.execute(
        "SELECT value FROM meta WHERE key = 'database_kind'"
    ).fetchone()
    stored_release = connection.execute(
        "SELECT value FROM meta WHERE key = 'release_name'"
    ).fetchone()
    if database_kind != ("snapshot",) or stored_release != (release_name,):
        raise SystemExit("snapshot metadata mismatch")
finally:
    connection.close()
"""

_REMOTE_COMPATIBILITY_CODE = r"""
import glob
import json
import os
import re
import sys

root = os.path.abspath(os.path.expanduser(sys.argv[1]))
required_version = tuple(int(part) for part in sys.argv[2].split('.') if part.isdigit())
required_schema = int(sys.argv[3])

def version_tuple(value):
    return tuple(int(part) for part in re.findall(r'\d+', str(value)))

matches = []
for manifest_path in glob.glob(os.path.join(root, '*', '.codex-plugin', 'plugin.json')):
    try:
        with open(manifest_path, encoding='utf-8') as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        continue
    if manifest.get('name') != 'music-kb':
        continue
    install_root = os.path.dirname(os.path.dirname(manifest_path))
    schema_path = os.path.join(install_root, 'src', 'music_kb', 'schema.py')
    try:
        with open(schema_path, encoding='utf-8') as handle:
            source = handle.read()
    except OSError:
        continue
    schema_match = re.search(r'^SCHEMA_VERSION\s*=\s*(\d+)', source, re.MULTILINE)
    if not schema_match:
        continue
    version = str(manifest.get('version', ''))
    schema = int(schema_match.group(1))
    matches.append({'version': version, 'schema_version': schema, 'manifest': manifest_path})

compatible = [item for item in matches if version_tuple(item['version']) >= required_version and item['schema_version'] >= required_schema]
if not compatible:
    raise SystemExit(json.dumps({'error': 'music-kb plugin/schema incompatible', 'required_version': sys.argv[2], 'required_schema_version': required_schema, 'found': matches}, ensure_ascii=False))
print(json.dumps({'compatible': True, 'required_version': sys.argv[2], 'required_schema_version': required_schema, 'found': compatible}, ensure_ascii=False))
"""


_REMOTE_INSTALL_CODE = r"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys

incoming_path = os.path.abspath(os.path.expanduser(sys.argv[1]))
target_path = os.path.abspath(os.path.expanduser(sys.argv[2]))
source_manifest = os.path.join(incoming_path, "manifest.json")
with open(source_manifest, encoding="utf-8") as handle:
    manifest = json.load(handle)
release_name = manifest["release_name"]
if not isinstance(release_name, str) or not release_name or any(
    character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    for character in release_name
):
    raise SystemExit("unsafe release name")
filename = manifest["database"]["filename"]
if os.path.basename(filename) != filename or not filename.endswith(".sqlite"):
    raise SystemExit("unsafe database filename")
expected = manifest["database"]["sha256"]
source_database = os.path.join(incoming_path, filename)
digest = hashlib.sha256()
with open(source_database, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected:
    raise SystemExit("snapshot SHA-256 mismatch")
connection = sqlite3.connect("file:" + source_database + "?mode=ro", uri=True)
try:
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise SystemExit("SQLite integrity check failed")
    if connection.execute("SELECT value FROM meta WHERE key = 'database_kind'").fetchone() != ("snapshot",):
        raise SystemExit("snapshot metadata mismatch")
    if connection.execute("SELECT value FROM meta WHERE key = 'release_name'").fetchone() != (release_name,):
        raise SystemExit("snapshot release metadata mismatch")
finally:
    connection.close()

"""
_REMOTE_INSTALL_CODE += r"""
release_dir = os.path.join(target_path, "releases")
target_incoming = os.path.join(target_path, "incoming")
os.makedirs(release_dir, exist_ok=True)
os.makedirs(target_incoming, exist_ok=True)
destination_database = os.path.join(release_dir, release_name + ".sqlite")
destination_manifest = os.path.join(release_dir, release_name + ".manifest.json")
temporary_database = os.path.join(target_incoming, release_name + ".sqlite.partial")
temporary_manifest = os.path.join(target_incoming, release_name + ".manifest.json.partial")
for path in (temporary_database, temporary_manifest):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
shutil.copy2(source_database, temporary_database)
digest = hashlib.sha256()
with open(temporary_database, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected:
    os.unlink(temporary_database)
    raise SystemExit("copied snapshot SHA-256 mismatch")
shutil.copy2(source_manifest, temporary_manifest)
os.chmod(temporary_database, 0o444)
os.chmod(temporary_manifest, 0o444)
os.replace(temporary_database, destination_database)
os.replace(temporary_manifest, destination_manifest)
temporary_link = os.path.join(target_path, ".current.sqlite.next")
try:
    os.unlink(temporary_link)
except FileNotFoundError:
    pass
os.symlink(os.path.relpath(destination_database, target_path), temporary_link)
os.replace(temporary_link, os.path.join(target_path, "current.sqlite"))
"""


def _remote_python_command(python_path: str, code: str, *arguments: str) -> str:
    executable = _remote_shell_path(python_path) if python_path.startswith(("~/", "/")) else shlex.quote(python_path)
    rendered_arguments = " ".join(_remote_shell_path(argument) for argument in arguments)
    suffix = f" {rendered_arguments}" if rendered_arguments else ""
    return f"set -eu; {executable} -c {shlex.quote(code)}{suffix}"


def _remote_preflight_command(python_path: str) -> str:
    return _remote_python_command(python_path, "import hashlib, json, sqlite3")


def _remote_compatibility_command(peer: DistributionPeer) -> str:
    return _remote_python_command(
        peer.python_path,
        _REMOTE_COMPATIBILITY_CODE,
        peer.plugin_cache_root,
        _current_plugin_version(),
        str(SCHEMA_VERSION),
    )


def _remote_verify_command(incoming_dir: str, python_path: str) -> str:
    manifest = _remote_join(incoming_dir, "manifest.json")
    return _remote_python_command(python_path, _REMOTE_VERIFY_CODE, manifest)


def _remote_install_command(incoming_dir: str, target_dir: str, python_path: str) -> str:
    return _remote_python_command(python_path, _REMOTE_INSTALL_CODE, incoming_dir, target_dir)


def _default_runner(command: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )


def _excerpt(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = value.strip()
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return f"{value[:MAX_OUTPUT_CHARS]}… [truncated]"


def _run_stage(
    *,
    name: str,
    command: Sequence[str],
    timeout_seconds: int,
    runner: CommandRunner,
) -> dict[str, Any]:
    try:
        completed = runner(command, timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "ok": False,
            "error": "timeout",
            "stdout": _excerpt(exc.stdout),
            "stderr": _excerpt(exc.stderr),
        }
    except OSError as exc:
        return {"name": name, "ok": False, "error": type(exc).__name__, "stderr": str(exc)}

    result: dict[str, Any] = {
        "name": name,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
    }
    stdout = _excerpt(completed.stdout)
    stderr = _excerpt(completed.stderr)
    if stdout:
        result["stdout"] = stdout
    if stderr:
        result["stderr"] = stderr
    return result


def _peer_plan(peer: DistributionPeer, release_name: str) -> dict[str, str]:
    incoming_dir = _remote_join(peer.target_dir, "incoming", release_name)
    return {
        "incoming_dir": incoming_dir,
        "target_dir": peer.target_dir,
    }


def publish_snapshot(
    release_dir: str | Path,
    peers_file: str | Path,
    *,
    peer_names: Sequence[str] = (),
    dry_run: bool = False,
    runner: CommandRunner = _default_runner,
) -> dict[str, Any]:
    """Fan out one verified release without ever copying a writable master DB."""

    source = Path(release_dir).expanduser().resolve()
    verified = verify_snapshot(source / "manifest.json")
    # verify_snapshot already validates the manifest, but retain the boundary
    # here because this name is later embedded in an rsync remote path.
    release_name = validate_release_name(verified["release_name"])
    peers = _select_peers(load_distribution_peers(peers_file), peer_names)
    peer_results: list[dict[str, Any]] = []

    for peer in peers:
        plan = _peer_plan(peer, release_name)
        result: dict[str, Any] = {"name": peer.name, "host": peer.host, **plan, "stages": []}
        if dry_run:
            result["status"] = "planned"
            peer_results.append(result)
            continue

        stages: list[tuple[str, Sequence[str]]] = [
            ("preflight", _ssh_command(peer, _remote_preflight_command(peer.python_path))),
            ("plugin_compatibility", _ssh_command(peer, _remote_compatibility_command(peer))),
            ("mkdir", _ssh_command(peer, _remote_mkdir_command(plan["incoming_dir"]))),
            ("rsync", _rsync_command(peer, source, plan["incoming_dir"])),
            ("verify", _ssh_command(peer, _remote_verify_command(plan["incoming_dir"], peer.python_path))),
            (
                "install",
                _ssh_command(
                    peer,
                    _remote_install_command(plan["incoming_dir"], plan["target_dir"], peer.python_path),
                ),
            ),
        ]
        for stage_name, command in stages:
            stage = _run_stage(
                name=stage_name,
                command=command,
                timeout_seconds=peer.command_timeout_seconds,
                runner=runner,
            )
            result["stages"].append(stage)
            if not stage["ok"]:
                result["status"] = "failed"
                break
        else:
            result["status"] = "succeeded"
        peer_results.append(result)

    succeeded = sum(result["status"] == "succeeded" for result in peer_results)
    failed = sum(result["status"] == "failed" for result in peer_results)
    return {
        "release_name": release_name,
        "release_dir": str(source),
        "dry_run": dry_run,
        "peer_count": len(peer_results),
        "succeeded_count": succeeded,
        "failed_count": failed,
        "peers": peer_results,
    }
