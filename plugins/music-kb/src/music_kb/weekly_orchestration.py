"""End-to-end publisher orchestration for one resumable weekly run."""

from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .campaign_delivery import load_campaign_delivery_file
from .lyrics import load_lyric_receipts
from .operation_context import RunContext, atom, atomic_write_json
from .preflight import run_preflight
from .repository import MusicKBRepository
from .snapshot import create_snapshot, current_snapshot_target, install_snapshot, verify_snapshot


DEFAULT_CHART_PROFILE = Path(__file__).resolve().parents[2] / "references" / "kugou-chart-profile.json"
DEFAULT_CNB_STORAGE_POLICY = Path(__file__).resolve().parents[2] / "references" / "cnb-storage-policy.json"


def _json_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout_seconds}s: {shlex.join(command)}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed with exit={completed.returncode}: {shlex.join(command)}\n"
            f"{completed.stderr.strip()}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"command returned no JSON output: {shlex.join(command)}")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command returned invalid JSON: {shlex.join(command)}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"command JSON result must be an object: {shlex.join(command)}")
    return value, completed


def _json_command_allow_failure(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    """Run a JSON atom while retaining its structured failure receipt."""

    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return {"status": "command_failed", "stderr": completed.stderr[-2000:]}, completed
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        payload = {"status": "invalid_json", "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:]}
    if not isinstance(payload, dict):
        payload = {"status": "invalid_json", "value": payload, "stderr": completed.stderr[-2000:]}
    return payload, completed


def _load_script_module(path: Path, module_name: str) -> Any:
    """Load one publisher atom helper without making scripts a package."""

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load atom helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_head_commit(repository_root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"unable to resolve GitHub source commit: {completed.stderr.strip()}")
    commit = completed.stdout.strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError(f"Git source commit is not a full lowercase SHA: {commit!r}")
    return commit


def _git_toplevel(path: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value).expanduser().resolve() if value else None


def _canonical_remote(repository_root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), "remote", "get-url", "origin"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    remote = completed.stdout.strip().lower().removesuffix(".git").rstrip("/")
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote.removeprefix("git@github.com:")
    return remote


def _git_worktree_clean(repository_root: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def _git_commit_reachable(repository_root: Path, commit: str | None) -> bool:
    if not commit:
        return True
    exists = subprocess.run(
        ["git", "-C", str(repository_root), "cat-file", "-e", f"{commit}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        return False
    reachable = subprocess.run(
        ["git", "-C", str(repository_root), "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"],
        text=True,
        capture_output=True,
        check=False,
    )
    return reachable.returncode == 0


def _resolve_campaign_repository_root(workspace: Path, preferred_commit: str | None = None) -> Path:
    """Find the GitHub source repo when data and code use separate roots."""

    candidates: list[Path] = [workspace]
    try:
        candidates.extend(
            child for child in workspace.iterdir() if child.is_dir() and (child / ".git").exists()
        )
    except OSError:
        pass
    source_root = Path(__file__).resolve().parents[4]
    if source_root not in candidates:
        candidates.append(source_root)

    discovered: list[tuple[bool, bool, bool, bool, Path]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        top = _git_toplevel(candidate)
        if top is None or top in seen:
            continue
        seen.add(top)
        discovered.append(
            (
                _canonical_remote(top) == "https://github.com/chen-da-pang/music-analysis-kb",
                _git_worktree_clean(top),
                top == source_root,
                _git_commit_reachable(top, preferred_commit),
                top,
            )
        )
    expected = [item for item in discovered if item[0] and item[3]]
    if not expected and preferred_commit:
        expected = [item for item in discovered if item[0]]
    if expected:
        expected.sort(key=lambda item: (not item[1], not item[2]))
        return expected[0][4]
    return discovered[0][4] if discovered else workspace


def _inventory_database_path(workspace: Path, chart_database: Path | None) -> Path:
    return chart_database if chart_database is not None else workspace / "data" / "music_trends.sqlite"


def _safe_run_id(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if not value or any(character not in allowed for character in value):
        raise ValueError("run_id contains unsafe characters")
    return value


def _resolve_workspace_path(value: str | Path, workspace: Path) -> Path:
    path = Path(value).expanduser()
    return (workspace / path).resolve() if not path.is_absolute() else path.resolve()


def _load_chart_profile(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path).expanduser().resolve()
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Kugou chart profile is unreadable: {profile_path}: {exc}") from exc
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise ValueError(f"Kugou chart profile schema must be 1: {profile_path}")
    charts = profile.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ValueError(f"Kugou chart profile has no charts: {profile_path}")
    rank_ids: set[str] = set()
    for chart in charts:
        if not isinstance(chart, dict):
            raise ValueError(f"Kugou chart profile contains a non-object chart: {profile_path}")
        rank_id = str(chart.get("rank_id", "")).strip()
        if not rank_id.isdigit() or int(rank_id) <= 0 or rank_id in rank_ids:
            raise ValueError(f"Kugou chart profile has an invalid/duplicate rank id: {rank_id}")
        rank_ids.add(rank_id)
    page_size = profile.get("page_size", 100)
    if not isinstance(page_size, int) or page_size < 1 or page_size > 500:
        raise ValueError(f"Kugou chart profile page_size must be between 1 and 500: {profile_path}")
    max_pages = profile.get("max_pages", 100)
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError(f"Kugou chart profile max_pages must be positive: {profile_path}")
    minimum_total = profile.get("minimum_total_source_records", 1)
    if not isinstance(minimum_total, int) or minimum_total < 1:
        raise ValueError(
            f"Kugou chart profile minimum_total_source_records must be positive: {profile_path}"
        )
    return {
        **profile,
        "profile_path": str(profile_path),
        "page_size": page_size,
        "max_pages": max_pages,
        "minimum_total_source_records": minimum_total,
    }


def _merge_chart_exports(paths: Sequence[Path], output: Path, *, run_id: str) -> dict[str, Any]:
    unique: dict[str, dict[str, Any]] = {}
    source_records = 0
    duplicate_records = 0
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("songs", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError(f"chart songs payload is not an array: {path}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_records += 1
            key = row.get("identity_key") or row.get("title_artist_key")
            if not key:
                continue
            if key in unique:
                duplicate_records += 1
                old_appearances = unique[key].setdefault("chart_appearances", [])
                old_appearances.extend(row.get("chart_appearances", []))
                if not unique[key].get("play_link") and row.get("play_link"):
                    unique[key]["play_link"] = row["play_link"]
                continue
            unique[key] = dict(row)

    payload = {
        "schema_version": 1,
        "platform": "kugou",
        "run_id": run_id,
        "summary": {
            "run_id": run_id,
            "source_records": source_records,
            "source_unique_records": len(unique),
            "duplicate_source_records": duplicate_records,
        },
        "songs": list(unique.values()),
    }
    atomic_write_json(output, payload)
    return payload["summary"]


def _inventory_song_count(path: Path) -> int:
    value = json.loads(path.read_text(encoding="utf-8"))
    songs = value.get("songs") if isinstance(value, dict) else None
    if not isinstance(songs, list) or not songs:
        raise ValueError(f"inventory has no songs: {path}")
    return len(songs)


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"campaign receipt is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"campaign receipt must be a JSON object: {path}")
    return value


def _delivery_file_binding(path: Path, *, expected_count: int | None = None) -> dict[str, Any]:
    entries = load_campaign_delivery_file(path, expected_count=expected_count)
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "count": len(entries),
    }


def _receipt_delivery_matches(receipt: Mapping[str, Any], delivery: Mapping[str, Any]) -> bool:
    recorded = receipt.get("delivery")
    if not isinstance(recorded, Mapping):
        return False
    try:
        recorded_path = Path(str(recorded.get("path", ""))).expanduser().resolve()
        supplied_path = Path(str(delivery.get("path", ""))).expanduser().resolve()
        return (
            recorded_path == supplied_path
            and str(recorded.get("sha256", "")) == str(delivery.get("sha256", ""))
            and int(recorded.get("count", -1)) == int(delivery.get("count", -2))
        )
    except (OSError, TypeError, ValueError):
        return False


def _cleanup_gate_satisfied(
    *,
    publish: bool,
    release_result: dict[str, Any] | None,
    skip_peers: bool,
    publish_result: dict[str, Any],
) -> bool:
    """Allow destructive cleanup only after local publication is explicit and safe."""

    if not publish or release_result is None:
        return False
    if skip_peers:
        return True
    return (
        int(publish_result.get("peer_count", 0)) > 0
        and int(publish_result.get("failed_count", 0)) == 0
    )


def _cnb_cleanup_receipt_is_acceptable(result: dict[str, Any]) -> bool:
    """Accept async GC only after every explicitly required deletion is proven."""

    repository_cleanup_ok = not bool(result.get("repository_cleanup_required")) or bool(
        result.get("destructive_repository_cleanup_complete")
    )
    return (
        bool(result.get("visible_cleanup_complete"))
        and not result.get("failures")
        and repository_cleanup_ok
        and (bool(result.get("clean")) or bool(result.get("server_gc_pending")))
    )


def run_weekly_run(
    *,
    workspace: str | Path,
    run_id: str,
    rank_ids: Sequence[str],
    chart_page: int,
    chart_size: int,
    chart_profile: str | Path | None,
    database: str | Path,
    inventory: str | Path,
    audio_root: str | Path,
    legacy_progress: str | Path,
    operations_file: str | Path,
    output_dir: str | Path,
    release_name: str | None,
    local_snapshot_dir: str | Path | None = None,
    install_local: bool | None = None,
    peers_file: str | Path | None,
    peer_names: Sequence[str],
    publish: bool,
    delivery: str | Path | None,
    lyric_receipt_paths: Sequence[str | Path] = (),
    cnb_command: str | None,
    chart_database: str | Path | None,
    state_file: str | Path,
    proxy: str | None = None,
    download_dry_run: bool = False,
    download_max_items: int | None = None,
    confirm_delete_audio: bool = False,
    expected_count: int | None = None,
    timeout_seconds: int = 86_400,
    skip_peers: bool = False,
    cnb_storage_policy: str | Path = DEFAULT_CNB_STORAGE_POLICY,
    confirm_delete_cnb_storage: bool = False,
    confirm_delete_cnb_repositories: bool = False,
    cnb_transport: str = "lfs",
    cnb_campaign_dry_run: bool = False,
    cnb_campaign_poll_seconds: float = 10.0,
    cnb_campaign_timeout_seconds: int | None = None,
    cnb_github_commit: str | None = None,
    cnb_campaign_work_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run all safe publisher stages and stop at the first failed atom."""

    root = Path(workspace).expanduser().resolve()
    run_id = _safe_run_id(run_id)
    run_dir = root / "data" / "weekly_runs" / run_id
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    database_path = _resolve_workspace_path(database, root)
    inventory_path = _resolve_workspace_path(inventory, root)
    audio_path = _resolve_workspace_path(audio_root, root)
    progress_path = _resolve_workspace_path(legacy_progress, root)
    operations_path = _resolve_workspace_path(operations_file, root)
    output_dir_path = _resolve_workspace_path(output_dir, root)
    local_snapshot_path = (
        _resolve_workspace_path(local_snapshot_dir, root)
        if local_snapshot_dir is not None
        else database_path.parent
    )
    if publish and install_local is False:
        raise ValueError("--no-install-local cannot be combined with --publish")
    install_local_enabled = publish if install_local is None else bool(install_local)
    peers_path = _resolve_workspace_path(peers_file, root) if peers_file else None
    state_path = _resolve_workspace_path(state_file, root)
    chart_db_path = _resolve_workspace_path(chart_database, root) if chart_database else None
    campaign_repository_root = _resolve_campaign_repository_root(root, cnb_github_commit)
    cnb_storage_policy_path = Path(cnb_storage_policy).expanduser().resolve()
    if cnb_transport not in {"lfs", "git-objects"}:
        raise ValueError("cnb_transport must be lfs or git-objects")
    if cnb_campaign_poll_seconds <= 0:
        raise ValueError("cnb_campaign_poll_seconds must be positive")
    if cnb_campaign_timeout_seconds is not None and cnb_campaign_timeout_seconds <= 0:
        raise ValueError("cnb_campaign_timeout_seconds must be positive")
    delivery_path: Path | None = _resolve_workspace_path(delivery, root) if delivery else None
    explicit_lyric_receipt_paths = [
        _resolve_workspace_path(path, root) for path in lyric_receipt_paths
    ]
    delivery_supplied = delivery is not None
    resume_reason = "verified supplied delivery resumes after Music Flamingo analysis"
    # The exact command set is finalized after we inspect a same-run receipt.
    # A receipt resume skips chart capture/download entirely, so requiring
    # those upstream tools would make an otherwise recoverable campaign fail
    # before it reaches its receipt-bound CNB work.
    required_commands: tuple[str, ...] = ()
    explicit_ranks = list(rank_ids)
    profile_path = Path(chart_profile).expanduser().resolve() if chart_profile else DEFAULT_CHART_PROFILE
    profile = None if explicit_ranks else _load_chart_profile(profile_path)
    capture_mode = "explicit_page" if explicit_ranks else "full_profile"
    capture_size = chart_size if explicit_ranks else int(profile["page_size"])
    campaign_adapter_path = scripts_dir / "cnb_campaign_repository.py"
    materializer_path = scripts_dir / "prepare_weekly_cnb_campaign.py"
    campaign_receipt_path = run_dir / "cnb" / "campaign-receipt.json"
    campaign_staging_path = run_dir / "cnb-input"
    campaign_receipt_result: dict[str, Any] | None = None
    materialization_result: dict[str, Any] | None = None
    download_result: dict[str, Any] = {}
    campaign_resume = False
    campaign_resume_has_delivery = False
    download_resume = False

    with RunContext(run_id=run_id, run_dir=run_dir, operations_file=operations_path) as context:
        campaign_adapter = None
        existing_campaign_receipt = _read_optional_json(campaign_receipt_path)
        if existing_campaign_receipt is not None:
            campaign_adapter = _load_script_module(campaign_adapter_path, "music_kb_campaign_resume")
            resume_policy = campaign_adapter._policy_with_transport(
                campaign_adapter.load_campaign_policy(cnb_storage_policy_path), cnb_transport
            )
            if delivery_supplied:
                binding = campaign_adapter.validate_campaign_receipt_binding(
                    resume_policy,
                    existing_campaign_receipt,
                    run_id=run_id,
                    transport=cnb_transport,
                )
                if not binding["valid"]:
                    raise RuntimeError("supplied delivery campaign receipt binding failed: " + "; ".join(binding["errors"]))
                supplied_binding = _delivery_file_binding(delivery_path, expected_count=expected_count) if delivery_path else None
                if supplied_binding is not None:
                    if not _receipt_delivery_matches(existing_campaign_receipt, supplied_binding):
                        raise RuntimeError("supplied delivery does not match the campaign receipt delivery hash/count/path")
                    campaign_receipt_result = existing_campaign_receipt
                    campaign_resume_has_delivery = True
            else:
                campaign_resume = True
                expected_commit = cnb_github_commit or _git_head_commit(root)
                staging_manifest = campaign_adapter._read_manifest(
                    campaign_staging_path,
                    expected_count=int((existing_campaign_receipt.get("manifest") or {}).get("item_count", 0)),
                )
                binding = campaign_adapter.validate_campaign_receipt_binding(
                    resume_policy,
                    existing_campaign_receipt,
                    run_id=run_id,
                    github_commit=expected_commit,
                    manifest=staging_manifest,
                    transport=cnb_transport,
                )
                if not binding["valid"]:
                    raise RuntimeError("same-run campaign receipt binding failed: " + "; ".join(binding["errors"]))
                campaign_receipt_result = existing_campaign_receipt
                materialization_result = {
                    "item_count": int(staging_manifest["item_count"]),
                    "source_links": int(staging_manifest["source_links"]),
                    "source_bytes": int(staging_manifest["source_bytes"]),
                    "manifest": str(staging_manifest["path"]),
                    "manifest_sha256": str(staging_manifest["sha256"]),
                    "status": "resumed_from_receipt",
                }
                delivery_info = existing_campaign_receipt.get("delivery")
                if (
                    existing_campaign_receipt.get("status") == "completed"
                    and isinstance(delivery_info, dict)
                    and delivery_info.get("path")
                ):
                    candidate_delivery = Path(str(delivery_info["path"])).expanduser().resolve()
                    if candidate_delivery.is_file():
                        candidate_binding = _delivery_file_binding(
                            candidate_delivery,
                            expected_count=int(staging_manifest["item_count"]),
                        )
                        if not _receipt_delivery_matches(existing_campaign_receipt, candidate_binding):
                            raise RuntimeError(
                                "same-run receipt delivery binding failed; refusing to re-use the delivery"
                            )
                        delivery_path = candidate_delivery
                        campaign_resume_has_delivery = True
        prior_download_atom = context.state.get("atoms", {}).get("claude_download", {})
        download_run_dir = root / "data" / "download_runs" / run_id
        download_resume = bool(
            context.resumed
            and not delivery_supplied
            and not campaign_resume
            # Preserve the exact queue after any downstream failure too;
            # rebuilding it from the live inventory would drop tracks that
            # were already downloaded in this run before the failure.
            and isinstance(prior_download_atom, Mapping)
            and prior_download_atom.get("status") in {"failed", "running", "succeeded"}
            and (download_run_dir / "download_queue.jsonl").is_file()
            and (download_run_dir / "queue_manifest.json").is_file()
        )
        if not delivery_supplied and not campaign_resume:
            # The default primary and fallback download executors run their
            # deterministic workers directly. Claude remains an explicit
            # compatibility fallback, not a weekly preflight dependency.
            required_commands = ("kugou-cli",)
        if publish and not skip_peers:
            required_commands += ("rsync",)
        with atom(context, "preflight", inputs={"workspace": str(root), "publish": publish} ) as outputs:
            preflight = run_preflight(
                workspace=root,
                operations_file=operations_path,
                database=database_path,
                inventory=inventory_path,
                audio_root=audio_path,
                peers_file=None if skip_peers else peers_path,
                publish=publish and not skip_peers,
                required_commands=required_commands,
            )
            outputs.update(preflight)
            if not preflight["valid"]:
                raise RuntimeError(f"weekly preflight failed: {preflight['failed_required']}")
            if publish:
                missing_cleanup_confirmations = []
                if not confirm_delete_audio:
                    missing_cleanup_confirmations.append("--confirm-delete-audio")
                if not confirm_delete_cnb_storage:
                    missing_cleanup_confirmations.append("--confirm-delete-cnb-storage")
                if not confirm_delete_cnb_repositories:
                    missing_cleanup_confirmations.append("--confirm-delete-cnb-repositories")
                if missing_cleanup_confirmations:
                    outputs["missing_cleanup_confirmations"] = missing_cleanup_confirmations
                    raise RuntimeError(
                        "this publishing weekly-run invocation must guarantee post-run cleanup; missing: "
                        + ", ".join(missing_cleanup_confirmations)
                    )

        with atom(
            context,
            "cnb_storage_preflight",
            inputs={"policy": str(cnb_storage_policy_path), "transport": cnb_transport},
        ) as outputs:
            if delivery_supplied:
                if delivery_path is None or not delivery_path.is_file():
                    raise RuntimeError(f"supplied canonical delivery does not exist: {delivery_path}")
                entries = load_campaign_delivery_file(delivery_path, expected_count=expected_count)
                outputs.update(
                    {
                        "status": "skipped",
                        "reason": resume_reason,
                        "delivery": str(delivery_path),
                        "delivery_count": len(entries),
                    }
                )
                if campaign_receipt_result is not None:
                    outputs["campaign_receipt"] = str(campaign_receipt_path)
                    outputs["campaign_receipt_bound"] = True
            elif campaign_resume:
                command = [
                    sys.executable,
                    str(campaign_adapter_path),
                    "preflight",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--operations-file",
                    str(operations_path),
                    "--transport",
                    cnb_transport,
                    "--resume-repository",
                    str(campaign_receipt_result["repository"]),
                ]
                payload, completed = _json_command_allow_failure(
                    command, cwd=root, timeout_seconds=min(timeout_seconds, 300)
                )
                outputs.update(payload)
                if completed.returncode != 0:
                    raise RuntimeError(
                        "CNB same-campaign resume preflight failed: "
                        f"{completed.stderr.strip() or outputs}"
                    )
            elif cnb_command:
                # Explicit legacy fallback: it still targets the historical
                # protected-repository route and therefore keeps the old
                # storage gate.  The automatic fresh path below never uses it.
                command = [
                    sys.executable,
                    str(scripts_dir / "cnb_storage_lifecycle.py"),
                    "inspect",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--transport",
                    cnb_transport,
                ]
                payload, completed = _json_command_allow_failure(
                    command, cwd=root, timeout_seconds=min(timeout_seconds, 300)
                )
                outputs.update(payload)
                if completed.returncode != 0:
                    raise RuntimeError(
                        "legacy CNB storage preflight failed: "
                        f"{completed.stderr.strip() or payload}"
                    )
            else:
                command = [
                    sys.executable,
                    str(campaign_adapter_path),
                    "preflight",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--operations-file",
                    str(operations_path),
                    "--transport",
                    cnb_transport,
                ]
                payload, completed = _json_command_allow_failure(
                    command, cwd=root, timeout_seconds=min(timeout_seconds, 300)
                )
                outputs.update(payload)
                if completed.returncode != 0:
                    raise RuntimeError(
                        "CNB disposable campaign preflight failed before this weekly-run invocation: "
                        f"{completed.stderr.strip() or outputs}"
                    )

        chart_dir = run_dir / "charts"
        capture_results: list[dict[str, Any]] = []
        with atom(
            context,
            "chart_capture",
            inputs={
                "mode": capture_mode,
                "rank_ids": explicit_ranks if explicit_ranks else [chart["rank_id"] for chart in profile["charts"]],
                "profile": str(profile_path) if profile else None,
                "page": chart_page,
                "size": capture_size,
                "termination": profile.get("termination") if profile else "single_page",
            },
        ) as outputs:
            if delivery_supplied or campaign_resume:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                charts = (
                    [{"rank_id": rank_id} for rank_id in explicit_ranks]
                    if explicit_ranks
                    else profile["charts"]
                )
                for chart in charts:
                    rank_id = str(chart["rank_id"])
                    page = chart_page
                    while True:
                        command = [
                            sys.executable,
                            str(scripts_dir / "capture_kugou_chart.py"),
                            "--run-id",
                            run_id,
                            "--rank-id",
                            rank_id,
                            "--page",
                            str(page),
                            "--size",
                            str(capture_size),
                            "--output-dir",
                            str(chart_dir),
                            "--operations-file",
                            str(operations_path),
                        ]
                        if proxy:
                            command.extend(["--proxy", proxy])
                        result, _ = _json_command(command, cwd=root, timeout_seconds=min(timeout_seconds, 300))
                        capture_results.append(result)
                        if capture_mode == "explicit_page":
                            break
                        source_records = int(result.get("source_records", 0))
                        if source_records == 0 or source_records < capture_size:
                            break
                        page += 1
                        if page - chart_page >= int(profile["max_pages"]):
                            raise RuntimeError(
                                f"Kugou chart pagination exceeded max_pages={profile['max_pages']}: rank_id={rank_id}"
                            )
                total_source_records = sum(int(item.get("source_records", 0)) for item in capture_results)
                if capture_mode == "full_profile" and total_source_records < int(profile["minimum_total_source_records"]):
                    raise RuntimeError(
                        "full Kugou chart profile captured too few records: "
                        f"{total_source_records} < {profile['minimum_total_source_records']}"
                    )
                outputs.update(
                    {
                        "mode": capture_mode,
                        "profile": str(profile_path) if profile else None,
                        "captures": capture_results,
                        "count": len(capture_results),
                        "source_records": total_source_records,
                    }
                )

        merged_chart = run_dir / "chart-songs.json"
        with atom(context, "chart_dedupe", inputs={"captures": [item["songs"] for item in capture_results]}) as outputs:
            if delivery_supplied or campaign_resume:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                summary = _merge_chart_exports(
                    [Path(item["songs"]) for item in capture_results], merged_chart, run_id=run_id
                )
                outputs.update({**summary, "songs": str(merged_chart)})

        queue_path = run_dir / "download_queue.jsonl"
        queue_manifest: dict[str, Any] = {}
        with atom(context, "historical_dedupe", inputs={"inventory": str(inventory_path), "source": str(merged_chart)}) as outputs:
            if delivery_supplied or campaign_resume:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                inventory_command = [
                    sys.executable,
                    str(scripts_dir / "build_song_inventory.py"),
                    "--db",
                    str(_inventory_database_path(root, chart_db_path)),
                    "--progress",
                    str(progress_path),
                    "--inventory",
                    str(inventory_path),
                    "--audio-root",
                    str(audio_path),
                ]
                inventory_result, _ = _json_command(inventory_command, cwd=root, timeout_seconds=min(timeout_seconds, 600))
                queue_command = [
                    sys.executable,
                    str(scripts_dir / "prepare_download_queue.py"),
                    "--source",
                    str(merged_chart),
                    "--inventory",
                    str(inventory_path),
                    "--output",
                    str(queue_path),
                    "--audio-root",
                    str(audio_path),
                ]
                queue_manifest, _ = _json_command(queue_command, cwd=root, timeout_seconds=min(timeout_seconds, 600))
                outputs.update({"inventory": inventory_result, "queue": queue_manifest})

        with atom(
            context,
            "claude_download",
            inputs={
                "queue": str(queue_path),
                "dry_run": download_dry_run,
                "reuse_queue": download_resume,
                "executor": "direct",
            },
        ) as outputs:
            if delivery_supplied or campaign_resume:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                command = [
                    sys.executable,
                    str(scripts_dir / "run_claude_download.py"),
                    "--workspace",
                    str(root),
                    "--source",
                    str(merged_chart),
                    "--run-id",
                    run_id,
                    "--operations-file",
                    str(operations_path),
                    "--timeout-seconds",
                    str(timeout_seconds),
                    "--executor",
                    "direct",
                ]
                if proxy:
                    command.extend(["--proxy", proxy])
                if download_dry_run:
                    command.append("--dry-run")
                if download_max_items is not None:
                    command.extend(["--max-items", str(download_max_items)])
                if download_resume:
                    command.append("--reuse-queue")
                download_result, _ = _json_command(command, cwd=root, timeout_seconds=timeout_seconds + 30)
                outputs.update(download_result)

        fallback_run_id = f"{run_id}-fallback"
        with atom(
            context,
            "fallback_download",
            inputs={"run_id": fallback_run_id, "dry_run": download_dry_run, "executor": "direct"},
        ) as outputs:
            if delivery_supplied or campaign_resume:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                fallback_command = [
                    sys.executable,
                    str(scripts_dir / "run_claude_fallback.py"),
                    "--workspace",
                    str(root),
                    "--run-id",
                    fallback_run_id,
                    "--operations-file",
                    str(operations_path),
                    "--timeout-seconds",
                    str(timeout_seconds),
                    "--executor",
                    "direct",
                ]
                if proxy:
                    fallback_command.extend(["--proxy", proxy])
                if download_dry_run:
                    fallback_command.append("--dry-run")
                fallback_result, _ = _json_command(fallback_command, cwd=root, timeout_seconds=timeout_seconds + 30)
                outputs.update(fallback_result)

        with atom(
            context,
            "cnb_input_materialization",
            inputs={
                "queue": str(queue_path),
                "inventory": str(inventory_path),
                "destination": str(campaign_staging_path),
                "delivery": str(delivery_path) if delivery_path else None,
            },
        ) as outputs:
            if delivery_supplied or campaign_resume:
                outputs.update({"status": "skipped", "reason": resume_reason})
            elif cnb_command:
                outputs.update({"status": "skipped", "reason": "legacy --cnb-command fallback"})
            elif download_dry_run:
                outputs.update({"status": "skipped", "reason": "download dry-run has no materialized audio"})
            else:
                queue_value = (
                    (download_result.get("queue_manifest") or {}).get("queue")
                    or queue_manifest.get("queue")
                    or str(queue_path)
                )
                actual_queue_path = Path(str(queue_value)).expanduser().resolve()
                materializer = _load_script_module(materializer_path, "music_kb_weekly_materializer")
                materialization_result = materializer.materialize(
                    actual_queue_path,
                    inventory_path,
                    audio_path,
                    campaign_staging_path,
                    run_id,
                )
                if (
                    int(materialization_result.get("item_count", 0)) > 0
                    and int(materialization_result.get("source_links", 0))
                    != int(materialization_result.get("item_count", 0))
                ):
                    raise RuntimeError(
                        "campaign input source-link completeness gate failed: "
                        f"{materialization_result.get('source_links')} / {materialization_result.get('item_count')}"
                    )
                outputs.update(materialization_result)

        with atom(
            context,
            "cnb_campaign_repository",
            inputs={
                "policy": str(cnb_storage_policy_path),
                "staging": str(campaign_staging_path),
                "receipt": str(campaign_receipt_path),
                "repository_root": str(campaign_repository_root),
                "dry_run": cnb_campaign_dry_run or download_dry_run,
            },
        ) as outputs:
            if delivery_supplied:
                outputs.update({"status": "skipped", "reason": resume_reason})
            elif campaign_resume and campaign_resume_has_delivery:
                outputs.update(
                    {
                        "status": "receipt_delivery_reused",
                        "reason": "same-run receipt already contains a verified delivery",
                        "delivery": str(delivery_path),
                    }
                )
            elif cnb_command:
                outputs.update({"status": "skipped", "reason": "legacy --cnb-command fallback"})
            elif download_dry_run:
                outputs.update({"status": "skipped", "reason": "download dry-run has no campaign repository"})
            elif not materialization_result or int(materialization_result.get("item_count", 0)) == 0:
                outputs.update({"status": "skipped", "reason": "no newly downloaded songs"})
            else:
                github_commit = cnb_github_commit or _git_head_commit(campaign_repository_root)
                command = [
                    sys.executable,
                    str(campaign_adapter_path),
                    "prepare",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--operations-file",
                    str(operations_path),
                    "--repository-root",
                    str(campaign_repository_root),
                    "--run-id",
                    run_id,
                    "--staging",
                    str(campaign_staging_path),
                    "--run-dir",
                    str(run_dir),
                    "--receipt",
                    str(campaign_receipt_path),
                    "--github-commit",
                    github_commit,
                    "--expected-count",
                    str(materialization_result["item_count"]),
                    "--transport",
                    cnb_transport,
                ]
                if cnb_campaign_work_dir is not None:
                    command.extend(["--work-dir", str(_resolve_workspace_path(cnb_campaign_work_dir, root))])
                if not cnb_campaign_dry_run:
                    command.append("--execute")
                command_result, _ = _json_command(
                    command,
                    cwd=root,
                    timeout_seconds=min(timeout_seconds, 3600),
                )
                on_disk_receipt = _read_optional_json(campaign_receipt_path)
                if on_disk_receipt is not None:
                    campaign_receipt_result = {**on_disk_receipt, **command_result}
                else:
                    campaign_receipt_result = command_result
                outputs.update(campaign_receipt_result)

        with atom(
            context,
            "cnb_campaign_submit",
            inputs={
                "receipt": str(campaign_receipt_path),
                "dry_run": cnb_campaign_dry_run or download_dry_run,
                "wait": True,
            },
        ) as outputs:
            if delivery_supplied:
                outputs.update({"status": "skipped", "reason": resume_reason})
            elif campaign_resume and campaign_resume_has_delivery:
                outputs.update(
                    {
                        "status": "receipt_delivery_reused",
                        "reason": "same-run receipt already contains a verified delivery",
                        "delivery": str(delivery_path),
                    }
                )
            elif cnb_command:
                outputs.update({"status": "skipped", "reason": "legacy --cnb-command fallback"})
            elif download_dry_run:
                outputs.update({"status": "skipped", "reason": "download dry-run has no campaign submission"})
            elif not materialization_result or int(materialization_result.get("item_count", 0)) == 0:
                outputs.update({"status": "skipped", "reason": "no newly downloaded songs"})
            else:
                command = [
                    sys.executable,
                    str(campaign_adapter_path),
                    "submit",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--operations-file",
                    str(operations_path),
                    "--receipt",
                    str(campaign_receipt_path),
                    "--run-dir",
                    str(run_dir),
                    "--wait",
                    "--timeout-seconds",
                    str(cnb_campaign_timeout_seconds or timeout_seconds),
                    "--poll-seconds",
                    str(cnb_campaign_poll_seconds),
                    "--transport",
                    cnb_transport,
                ]
                if materialization_result.get("source_links") == materialization_result.get("item_count"):
                    command.append("--require-source-url")
                if not cnb_campaign_dry_run:
                    command.append("--execute")
                command_result, _ = _json_command(
                    command,
                    cwd=root,
                    timeout_seconds=(cnb_campaign_timeout_seconds or timeout_seconds) + 30,
                )
                on_disk_receipt = _read_optional_json(campaign_receipt_path)
                if on_disk_receipt is not None:
                    campaign_receipt_result = {**on_disk_receipt, **command_result}
                else:
                    campaign_receipt_result = command_result
                outputs.update(campaign_receipt_result)
                delivery_info = campaign_receipt_result.get("delivery")
                if isinstance(delivery_info, dict) and delivery_info.get("path"):
                    delivery_path = Path(str(delivery_info["path"])).expanduser().resolve()

        if delivery_path is None and cnb_command:
            delivery_path = run_dir / "cnb" / "canonical_delivery.jsonl"
        analysis_expected_count = expected_count
        if materialization_result and materialization_result.get("item_count"):
            analysis_expected_count = int(materialization_result["item_count"])
        with atom(context, "cnb_analysis", inputs={"delivery": str(delivery_path) if delivery_path else None, "command": cnb_command}) as outputs:
            if delivery_supplied and delivery_path is not None and delivery_path.is_file():
                entries = load_campaign_delivery_file(delivery_path, expected_count=analysis_expected_count)
                outputs.update(
                    {
                        "status": "supplied_delivery_validated",
                        "delivery": str(delivery_path),
                        "count": len(entries),
                    }
                )
            elif cnb_command and delivery_path is not None:
                delivery_path.parent.mkdir(parents=True, exist_ok=True)
                env = os.environ.copy()
                env.update(
                    {
                        "MUSIC_KB_WEEKLY_RUN_ID": run_id,
                        "MUSIC_KB_CNB_OUTPUT": str(delivery_path),
                        "MUSIC_KB_CNB_INPUT": str(merged_chart),
                        "MUSIC_KB_AUDIO_ROOT": str(audio_path),
                    }
                )
                command = shlex.split(cnb_command)
                completed = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=timeout_seconds, check=False)
                outputs.update({"command": command, "returncode": completed.returncode, "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:]})
                if completed.returncode != 0 or not delivery_path.is_file():
                    raise RuntimeError("CNB command did not produce a canonical delivery")
                outputs["count"] = len(load_campaign_delivery_file(delivery_path, expected_count=analysis_expected_count))
            elif delivery_path is not None and delivery_path.is_file():
                entries = load_campaign_delivery_file(delivery_path, expected_count=analysis_expected_count)
                outputs.update(
                    {
                        "status": "campaign_delivery_validated",
                        "delivery": str(delivery_path),
                        "count": len(entries),
                    }
                )
            elif download_dry_run or cnb_campaign_dry_run:
                outputs.update({"status": "skipped", "reason": "dry-run has no CNB delivery"})
            elif not materialization_result or int(materialization_result.get("item_count", 0)) == 0:
                outputs.update({"status": "skipped", "reason": "no newly downloaded songs"})
            else:
                raise RuntimeError("CNB stage did not produce a canonical campaign delivery")

        import_result: dict[str, Any]
        with atom(context, "knowledge_import", inputs={"delivery": str(delivery_path) if delivery_path else None}) as outputs:
            if delivery_path is None or not delivery_path.is_file():
                outputs.update({"status": "skipped", "reason": "no delivery in dry-run"})
            else:
                entries = load_campaign_delivery_file(delivery_path, expected_count=analysis_expected_count)
                with MusicKBRepository(database_path, read_only=False) as repository:
                    import_result = repository.import_campaign_delivery(entries)
                    if chart_db_path is not None and chart_db_path.is_file():
                        link_result = repository.backfill_source_links(chart_db_path)
                    else:
                        link_result = {"skipped": True, "reason": "chart database not supplied"}
                    tag_result = repository.enrich_campaign_tags(dry_run=False, batch_size=500)
                    validation = repository.validate()
                    status = repository.status()
                if not validation["valid"]:
                    raise RuntimeError(f"master validation failed: {validation}")
                counts = status["counts"]
                if counts["source_tracks"] <= 0 or counts["source_links"] != counts["source_tracks"]:
                    raise RuntimeError(
                        "source link completeness gate failed: "
                        f"source_tracks={counts['source_tracks']} source_links={counts['source_links']}"
                    )
                import_result = {"import": import_result, "links": link_result, "tags": tag_result, "validation": validation, "status": status}
                outputs.update(import_result)

        receipt_paths: list[Path] = []
        seen_receipt_paths: set[Path] = set()
        for candidate in explicit_lyric_receipt_paths:
            if candidate not in seen_receipt_paths:
                seen_receipt_paths.add(candidate)
                receipt_paths.append(candidate)
        automatic_receipt = download_result.get("lyrics_receipt")
        if automatic_receipt:
            candidate = Path(str(automatic_receipt)).expanduser().resolve()
            if candidate not in seen_receipt_paths:
                seen_receipt_paths.add(candidate)
                receipt_paths.append(candidate)

        with atom(
            context,
            "lyrics_import",
            inputs={
                "receipts": [str(path) for path in receipt_paths],
                "delivery": str(delivery_path) if delivery_path else None,
            },
        ) as outputs:
            if delivery_path is None or not delivery_path.is_file():
                outputs.update({"status": "skipped", "reason": "no delivery in dry-run"})
            elif not receipt_paths:
                outputs.update(
                    {
                        "status": "skipped",
                        "reason": "no lyric receipts supplied; coverage gate will block release",
                    }
                )
            else:
                receipts = [receipt for path in receipt_paths for receipt in load_lyric_receipts(path)]
                with MusicKBRepository(database_path, read_only=False) as repository:
                    lyric_result = repository.import_lyric_receipts(receipts)
                    coverage = repository.lyric_coverage()
                outputs.update(
                    {
                        **lyric_result,
                        "receipt_files": [str(path) for path in receipt_paths],
                        "coverage": coverage,
                    }
                )

        with atom(
            context,
            "lyrics_coverage",
            inputs={"database": str(database_path)},
        ) as outputs:
            if delivery_path is None or not delivery_path.is_file():
                outputs.update({"status": "skipped", "reason": "no delivery in dry-run"})
            else:
                with MusicKBRepository(database_path, read_only=True) as repository:
                    lyric_validation = repository.validate(require_lyrics=True)
                    lyric_status = repository.status()
                outputs.update(
                    {
                        "validation": lyric_validation,
                        "coverage": lyric_validation["lyrics_coverage"],
                        "status": lyric_status,
                    }
                )
                if not lyric_validation["valid"]:
                    raise RuntimeError(f"lyric coverage gate failed: {lyric_validation}")

        release_result: dict[str, Any] | None = None
        release_verification: dict[str, Any] | None = None
        with atom(context, "snapshot", inputs={"database": str(database_path), "output_dir": str(output_dir_path)}) as outputs:
            if delivery_path is None or not delivery_path.is_file():
                outputs.update({"status": "skipped", "reason": "no delivery in dry-run"})
            else:
                release_result = create_snapshot(database_path, output_dir_path, release_name=release_name)
                release_verification = verify_snapshot(Path(release_result["manifest"]))
                outputs.update({"release": release_result, "verification": release_verification})

        local_install_result: dict[str, Any] | None = None
        with atom(
            context,
            "local_snapshot_install",
            inputs={
                "release_dir": str(release_result["release_dir"]) if release_result else None,
                "release_sha256": str((release_verification or {}).get("sha256") or ""),
                "target_dir": str(local_snapshot_path),
                "enabled": install_local_enabled,
            },
        ) as outputs:
            verification_receipt = None
            release_sha256 = ""
            release_name = None
            previous_current = current_snapshot_target(local_snapshot_path)
            if release_verification is not None:
                release_sha256 = str(release_verification.get("sha256") or "")
                release_name = release_verification.get("release_name")
                verification_receipt = {
                    "valid": bool(release_verification.get("valid")),
                    "manifest": str(release_verification.get("manifest") or ""),
                    "sha256": release_sha256,
                }
            outputs.update(
                {
                    "release_name": release_name,
                    "release_sha256": release_sha256,
                    "verification": verification_receipt,
                    "target_dir": str(local_snapshot_path),
                    "previous_current": previous_current,
                }
            )
            if release_result is None:
                outputs.update({"status": "skipped", "reason": "no release in dry-run"})
            elif not install_local_enabled:
                outputs.update(
                    {
                        "status": "skipped",
                        "reason": "publisher-local install disabled",
                    }
                )
            else:
                try:
                    local_install_result = install_snapshot(release_result["release_dir"], local_snapshot_path)
                except Exception:
                    outputs["current_target"] = current_snapshot_target(local_snapshot_path)
                    raise
                outputs.update({"status": "succeeded", **local_install_result})

        publish_result: dict[str, Any] = {"status": "skipped", "reason": "no release in dry-run"}
        with atom(context, "peer_publish", inputs={"publish": publish, "peers_file": str(peers_path) if peers_path else None}) as outputs:
            if release_result is None:
                outputs.update(publish_result)
            elif skip_peers:
                publish_result = {
                    "status": "skipped",
                    "reason": "peer sync explicitly skipped",
                    "peer_count": 0,
                    "succeeded_count": 0,
                    "failed_count": 0,
                    "dry_run": not publish,
                }
                outputs.update(publish_result)
            else:
                from .distribution import publish_snapshot

                publish_result = publish_snapshot(
                    release_result["release_dir"],
                    peers_path,
                    peer_names=peer_names,
                    dry_run=not publish,
                )
                outputs.update(publish_result)
                if publish and publish_result["failed_count"]:
                    raise RuntimeError(f"peer publish failed: {publish_result}")
                if publish:
                    from .publish_state import record_publish_result

                    record_publish_result(
                        state_path,
                        publish_result,
                        release_sha256=str((release_verification or {}).get("sha256") or ""),
                    )
                    outputs["state_file"] = str(state_path)

        cleanup_gate = _cleanup_gate_satisfied(
            publish=publish,
            release_result=release_result,
            skip_peers=skip_peers,
            publish_result=publish_result,
        )
        with atom(
            context,
            "cnb_campaign_cleanup",
            inputs={
                "receipt": str(campaign_receipt_path),
                "confirm": confirm_delete_cnb_repositories,
                "cleanup_gate": cleanup_gate,
            },
        ) as outputs:
            campaign_exists = campaign_receipt_path.is_file() and campaign_receipt_result is not None
            if not campaign_exists:
                outputs.update({"status": "skipped", "reason": "no disposable campaign receipt"})
            else:
                command = [
                    sys.executable,
                    str(campaign_adapter_path),
                    "cleanup",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--operations-file",
                    str(operations_path),
                    "--receipt",
                    str(campaign_receipt_path),
                    "--transport",
                    cnb_transport,
                ]
                if confirm_delete_cnb_repositories:
                    command.append("--confirm-delete-cnb-repositories")
                if release_result is not None:
                    command.append("--release-verified")
                if cleanup_gate:
                    command.append("--peer-gate")
                cleanup_result, cleanup_completed = _json_command_allow_failure(
                    command,
                    cwd=root,
                    timeout_seconds=min(timeout_seconds, 600),
                )
                outputs.update(cleanup_result)
                if not confirm_delete_cnb_repositories:
                    outputs.setdefault("status", "dry_run")
                elif cleanup_result.get("clean") is not True:
                    raise RuntimeError(f"disposable campaign repository cleanup did not verify: {cleanup_result}")
        with atom(
            context,
            "audio_cleanup",
            inputs={"confirm": confirm_delete_audio, "cleanup_gate": cleanup_gate},
        ) as outputs:
            if not confirm_delete_audio:
                outputs.update({"status": "skipped", "reason": "confirmation flag not supplied"})
            elif not cleanup_gate:
                raise RuntimeError(
                    "audio cleanup requires a verified release and either explicit peer skip or successful peer publication"
                )
            else:
                expected_inventory_count = _inventory_song_count(inventory_path)
                cleanup_command = [
                    sys.executable,
                    str(scripts_dir / "prune_audio_library.py"),
                    "--inventory",
                    str(inventory_path),
                    "--audio-root",
                    str(audio_path),
                    "--knowledge-db",
                    str(database_path),
                    "--expected-count",
                    str(expected_inventory_count),
                    "--confirm-delete-audio",
                ]
                cleanup_result, _ = _json_command(cleanup_command, cwd=root, timeout_seconds=min(timeout_seconds, 600))
                outputs.update(cleanup_result)

        with atom(
            context,
            "cnb_storage_cleanup",
            inputs={
                "policy": str(cnb_storage_policy_path),
                "confirm": confirm_delete_cnb_storage,
                "confirm_delete_repositories": confirm_delete_cnb_repositories,
                "transport": cnb_transport,
            },
        ) as outputs:
            if not confirm_delete_cnb_storage:
                outputs.update({"status": "skipped", "reason": "confirmation flag not supplied"})
            elif not cleanup_gate:
                raise RuntimeError(
                    "CNB storage cleanup requires a verified release and either explicit peer skip or successful peer publication"
                )
            else:
                command = [
                    sys.executable,
                    str(scripts_dir / "cnb_storage_lifecycle.py"),
                    "cleanup",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--transport",
                    cnb_transport,
                    "--confirm-cleanup",
                ]
                if confirm_delete_cnb_repositories:
                    command.append("--confirm-delete-cnb-repositories")
                completed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
                lines = [line for line in completed.stdout.splitlines() if line.strip()]
                if lines:
                    try:
                        outputs.update(json.loads(lines[-1]))
                    except json.JSONDecodeError:
                        outputs["stdout"] = completed.stdout[-2000:]
                if completed.returncode != 0:
                    if _cnb_cleanup_receipt_is_acceptable(outputs):
                        outputs["status"] = "succeeded_with_server_gc_pending"
                    else:
                        raise RuntimeError(
                            "CNB object storage cleanup did not verify: "
                            f"{completed.stderr.strip() or outputs}"
                        )

    return {
        "workflow": "weekly-run",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "state": str(run_dir / "run-state.json"),
        "local_install": local_install_result,
        "publish": publish_result,
    }
