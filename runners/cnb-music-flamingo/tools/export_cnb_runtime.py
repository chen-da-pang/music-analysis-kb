#!/usr/bin/env python3
"""Export a pinned, code-only CNB runner from the GitHub source tree.

The exporter intentionally reads the requested Git commit with Git plumbing,
not the working tree. That makes the CNB checkout reproducible and prevents
local audio, ledgers, caches, or other untracked run state from crossing the
GitHub-to-CNB boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


GITHUB_REPOSITORY = "https://github.com/chen-da-pang/music-analysis-kb"
RUNNER_SUBTREE = "runners/cnb-music-flamingo"
PROVENANCE_NAME = ".github-source.json"
MAX_FILE_BYTES = 1024 * 1024

ROOT_FILES = frozenset({".cnb.yml", ".dockerignore", ".gitignore", "README.md"})
ROOT_DIRECTORIES = frozenset({".cnb", "config", "scripts", "tests"})
MANUAL_QUALITY_ROUTE_PATHS = frozenset(
    {
        "scripts/devgpu_run_manual_kugou_quality_rerun.sh",
        "scripts/manual_kugou_quality_route.py",
        "scripts/check_manual_gpu_gate.py",
        "scripts/prepare_kugou_quality_rerun.sh",
        "scripts/prepare_kugou_quality_rerun.py",
    }
)
REQUIRED_PATHS = frozenset(
    {
        ".cnb.yml",
        ".cnb/Dockerfile.flamingo",
        ".cnb/requirements.runtime.txt",
        ".dockerignore",
        ".gitignore",
        "README.md",
        "config/env.example",
    }
) | MANUAL_QUALITY_ROUTE_PATHS

AUDIO_EXTENSIONS = frozenset(
    {
        ".mp3",
        ".flac",
        ".wav",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wma",
        ".aiff",
        ".aif",
        ".ape",
        ".alac",
    }
)
DATABASE_EXTENSIONS = frozenset({".jsonl", ".sqlite", ".sqlite3", ".db", ".parquet"})
MODEL_EXTENSIONS = frozenset({".pt", ".pth", ".ckpt", ".safetensors", ".onnx"})
RUNTIME_ARTIFACT_EXTENSIONS = frozenset({".pyc", ".pyo", ".log", ".tmp", ".tar", ".zip"})
FORBIDDEN_COMPONENTS = frozenset(
    {
        "data",
        "cache",
        "caches",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "work",
        "logs",
        "artifacts",
        "cnb-artifacts",
        "ledger",
        "ledgers",
        "canonical-delivery",
        "canonical-deliveries",
        "quality-run",
        "quality-runs",
        "input",
        "inputs",
        "output",
        "outputs",
    }
)
FORBIDDEN_NAMES = frozenset(
    {
        "batch_report.json",
        "campaign_ledger.jsonl",
        "campaign_manifest.json",
        "campaign_report.json",
        "campaign_state.json",
        "canonical_delivery_manifest.jsonl",
        "manifest.jsonl",
        "model_report.json",
        "progress.jsonl",
        "quality_audit.json",
        "quality_report.json",
        "run_report.json",
        "run.log",
        "run_status.json",
        "stderr.txt",
        "stdout.txt",
    }
)


class ExportError(ValueError):
    """Raised when a source tree cannot be safely mirrored."""


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    object_id: str
    size: int


def _run_git(repository_root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository_root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ExportError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _tree_entries(repository_root: Path, commit: str) -> list[TreeEntry]:
    prefix = f"{RUNNER_SUBTREE}/"
    payload = _run_git(repository_root, "ls-tree", "-r", "-l", "-z", commit, "--", RUNNER_SUBTREE)
    entries: list[TreeEntry] = []
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        metadata, raw_path = raw.split(b"\t", 1)
        mode, object_type, object_id, raw_size = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if object_type != "blob" or not path.startswith(prefix):
            raise ExportError(f"runner tree contains non-regular entry: {path}")
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise ExportError(f"runner tree has invalid size for {path}: {raw_size!r}") from exc
        entries.append(TreeEntry(path=path[len(prefix) :], mode=mode, object_id=object_id, size=size))
    if not entries:
        raise ExportError(f"commit {commit} has no {RUNNER_SUBTREE} subtree")
    return sorted(entries, key=lambda entry: entry.path)


def _is_allowed(path: str) -> bool:
    pure = PurePosixPath(path)
    if len(pure.parts) == 1:
        return pure.name in ROOT_FILES
    return pure.parts[0] in ROOT_DIRECTORIES


def _validate_entries(entries: list[TreeEntry]) -> list[TreeEntry]:
    paths = {entry.path for entry in entries}
    missing = sorted(REQUIRED_PATHS - paths)
    if missing:
        raise ExportError(f"runner tree is missing required paths: {', '.join(missing)}")

    for entry in entries:
        pure = PurePosixPath(entry.path)
        if pure.is_absolute() or ".." in pure.parts or "" in pure.parts:
            raise ExportError(f"unsafe runner path: {entry.path!r}")
        if entry.size > MAX_FILE_BYTES:
            raise ExportError(f"file exceeds {MAX_FILE_BYTES} bytes: {entry.path} ({entry.size})")
        if entry.mode not in {"100644", "100755"}:
            raise ExportError(f"runner file has unsupported mode {entry.mode}: {entry.path}")
        lowered_parts = {part.lower() for part in pure.parts}
        if lowered_parts & FORBIDDEN_COMPONENTS:
            bad = sorted(lowered_parts & FORBIDDEN_COMPONENTS)[0]
            raise ExportError(f"forbidden production path component {bad!r}: {entry.path}")
        suffix = pure.suffix.lower()
        if suffix in AUDIO_EXTENSIONS:
            raise ExportError(f"audio file is forbidden in the code mirror: {entry.path}")
        if suffix in DATABASE_EXTENSIONS:
            raise ExportError(f"database/ledger artifact is forbidden in the code mirror: {entry.path}")
        if suffix in MODEL_EXTENSIONS:
            raise ExportError(f"model artifact is forbidden in the code mirror: {entry.path}")
        if suffix in RUNTIME_ARTIFACT_EXTENSIONS or pure.name.lower().endswith(".tar.gz"):
            raise ExportError(f"runtime artifact is forbidden in the code mirror: {entry.path}")
        if pure.name.lower() in FORBIDDEN_NAMES:
            raise ExportError(f"production run artifact is forbidden in the code mirror: {entry.path}")
    selected = [entry for entry in entries if _is_allowed(entry.path)]
    if not selected:
        raise ExportError("allowlist selected no files")
    return selected


def _validate_commit(commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ExportError("--github-commit must be a full 40-character lowercase commit SHA")


def _canonical_remote(url: str) -> str:
    value = url.strip()
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value.removeprefix("ssh://git@github.com/")
    return value.removesuffix(".git").rstrip("/")


def _validate_published_source(repository_root: Path, commit: str) -> None:
    remote = _run_git(repository_root, "remote", "get-url", "origin").decode("utf-8").strip()
    if _canonical_remote(remote) != _canonical_remote(GITHUB_REPOSITORY):
        raise ExportError(
            f"origin must be {GITHUB_REPOSITORY!r} before exporting, got {remote!r}"
        )
    probe = subprocess.run(
        ["git", "-C", str(repository_root), "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if probe.returncode:
        raise ExportError(f"commit {commit} is not published on origin/main")


def export_runtime(
    repository_root: Path,
    commit: str,
    output: Path,
    *,
    require_published: bool = True,
) -> dict[str, object]:
    repository_root = Path(repository_root).resolve()
    output = Path(output).resolve()
    _validate_commit(commit)
    _run_git(repository_root, "cat-file", "-e", f"{commit}^{{commit}}")
    if require_published:
        _validate_published_source(repository_root, commit)
    entries = _validate_entries(_tree_entries(repository_root, commit))
    if output.exists():
        raise ExportError(f"output must not already exist: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        exported: list[dict[str, object]] = []
        for entry in entries:
            destination = temporary / Path(*PurePosixPath(entry.path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = _run_git(repository_root, "cat-file", "blob", entry.object_id)
            if len(content) != entry.size:
                raise ExportError(f"blob size changed while exporting {entry.path}")
            destination.write_bytes(content)
            destination.chmod(0o755 if entry.mode == "100755" else 0o644)
            exported.append(
                {"path": entry.path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            )

        provenance = {
            "schema_version": 1,
            "source_repository": GITHUB_REPOSITORY,
            "source_commit": commit,
            "runner_subtree": RUNNER_SUBTREE,
            "files": exported,
        }
        provenance_bytes = (
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        (temporary / PROVENANCE_NAME).write_bytes(provenance_bytes)
        (temporary / PROVENANCE_NAME).chmod(0o644)
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return provenance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-commit", required=True, help="full 40-character GitHub commit SHA")
    parser.add_argument("--output", required=True, type=Path, help="new directory to create")
    parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="allow a local commit not yet reachable from origin/main (for local tests only)",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="GitHub checkout containing runners/cnb-music-flamingo (default: this checkout)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        provenance = export_runtime(
            args.repository_root,
            args.github_commit,
            args.output,
            require_published=not args.allow_unpublished,
        )
    except (ExportError, OSError) as exc:
        print(f"export failed: {exc}")
        return 2
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "source_commit": provenance["source_commit"],
                "file_count": len(provenance["files"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
