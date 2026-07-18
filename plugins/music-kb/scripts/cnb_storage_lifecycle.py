#!/usr/bin/env python3
"""Inspect and clean disposable CNB runtime storage residue.

GitHub is the source of truth for runner code. CNB keeps only the protected
code mirror and the required runtime image; campaign inputs, result branches,
ledgers, and temporary assets are disposable after their outputs have been
exported and verified locally. Normal campaigns use Git LFS and require CNB's
authoritative object counter to fall below the policy limit. A narrowly
bounded ``git-objects`` transport is available only when orphan LFS has not
been reclaimed: it uses the separately metered ordinary Git storage counter,
while retaining the same branch and asset cleanup rules.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence


Runner = Callable[[Sequence[str]], dict[str, Any]]


def load_policy(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ValueError(f"CNB storage policy schema must be 2: {path}")
    required = {
        "organization_slug",
        "repository_slug",
        "clean_repo_object_bytes_max",
        "clean_repo_git_bytes_max",
        "minimum_group_object_free_bytes",
        "minimum_group_git_free_bytes",
        "required_runtime",
        "protected_branches",
        "preserved_branch_patterns",
        "cleanup_branch_patterns",
        "cleanup_asset_name_patterns",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"CNB storage policy is missing fields: {missing}")
    if int(value["clean_repo_object_bytes_max"]) <= 0:
        raise ValueError("clean_repo_object_bytes_max must be positive")
    if int(value["clean_repo_git_bytes_max"]) <= 0:
        raise ValueError("clean_repo_git_bytes_max must be positive")
    if int(value["minimum_group_object_free_bytes"]) <= 0:
        raise ValueError("minimum_group_object_free_bytes must be positive")
    if int(value["minimum_group_git_free_bytes"]) <= 0:
        raise ValueError("minimum_group_git_free_bytes must be positive")
    for key in ("preserved_branch_patterns", "cleanup_branch_patterns", "cleanup_asset_name_patterns"):
        for pattern in value[key]:
            re.compile(pattern)
    return value


def run_cnb(command: Sequence[str]) -> dict[str, Any]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"CNB command failed with exit={completed.returncode}: {' '.join(command)}\n"
            f"{completed.stderr.strip()}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CNB command returned invalid JSON: {' '.join(command)}") from exc
    if not isinstance(value, dict) or value.get("status") not in (200, 201, 204):
        raise RuntimeError(f"CNB command returned an unsuccessful response: {value}")
    return value


def _data(response: dict[str, Any]) -> Any:
    return response.get("data")


def _matches(value: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, value) for pattern in patterns)


def inspect(
    policy: dict[str, Any],
    runner: Runner = run_cnb,
    *,
    transport: str = "lfs",
) -> dict[str, Any]:
    if transport not in {"lfs", "git-objects"}:
        raise ValueError("transport must be lfs or git-objects")
    repo = str(policy["repository_slug"])
    org = str(policy["organization_slug"])
    runtime = policy["required_runtime"]
    branches_response = runner(
        ["cnb", "git", "list-branches", "--repo", repo, "--page", "1", "--page-size", "100", "--verbose"]
    )
    workspaces_response = runner(
        [
            "cnb", "workspace", "list-workspaces", "--slug", repo, "--status", "running",
            "--page", "1", "--page-size", "100", "--verbose",
        ]
    )
    assets_response = runner(
        ["cnb", "assets", "list-assets", "--slug", repo, "--page", "1", "--page-size", "100", "--verbose"]
    )
    volume_response = runner(
        [
            "cnb", "charge", "get-repos-volume", "--slug", org, "--type", "charge_type_object",
            "--page", "1", "--page-size", "100", "--verbose",
        ]
    )
    git_volume_response = runner(
        [
            "cnb", "charge", "get-repos-volume", "--slug", org, "--type", "charge_type_git",
            "--page", "1", "--page-size", "100", "--verbose",
        ]
    )
    quota_response = runner(["cnb", "charge", "get-quota", "--slug", org, "--verbose"])
    group_volume_response = runner(["cnb", "charge", "get-volume", "--slug", org, "--verbose"])
    tags_response = runner(
        [
            "cnb", "registries", "list-package-tags", "--slug", str(runtime["registry_slug"]),
            "--type", str(runtime["package_type"]), "--name", str(runtime["package_name"]),
            "--page", "1", "--page-size", "100", "--verbose",
        ]
    )

    branches = [str(row.get("name", "")) for row in (_data(branches_response) or [])]
    workspace_data = _data(workspaces_response) or {}
    running_workspaces = workspace_data.get("list", []) if isinstance(workspace_data, dict) else []
    active_campaign_branches = sorted(
        {
            str(row.get("branch", ""))
            for row in running_workspaces
            if _matches(str(row.get("branch", "")), policy["cleanup_branch_patterns"])
        }
    )
    cleanup_branches = sorted(
        branch
        for branch in branches
        if branch not in policy["protected_branches"]
        and not _matches(branch, policy["preserved_branch_patterns"])
        and _matches(branch, policy["cleanup_branch_patterns"])
        and branch not in active_campaign_branches
    )
    unexpected_branches = sorted(
        branch
        for branch in branches
        if branch not in policy["protected_branches"]
        and not _matches(branch, policy["preserved_branch_patterns"])
        and not _matches(branch, policy["cleanup_branch_patterns"])
    )

    assets = _data(assets_response) or []
    cleanup_assets = []
    for asset in assets:
        name = Path(str(asset.get("path", ""))).name
        if _matches(name, policy["cleanup_asset_name_patterns"]):
            cleanup_assets.append(
                {
                    "id": str(asset.get("id", "")),
                    "name": name,
                    "path": str(asset.get("path", "")),
                    "bytes": int(asset.get("size_in_byte", 0)),
                }
            )

    repo_object_bytes = None
    for row in (_data(volume_response) or []):
        if row.get("slug") == repo:
            repo_object_bytes = int(row.get("volume", 0))
            break
    if repo_object_bytes is None:
        raise RuntimeError(f"CNB object-volume response did not contain repository: {repo}")
    repo_git_bytes = None
    for row in (_data(git_volume_response) or []):
        if row.get("slug") == repo:
            repo_git_bytes = int(row.get("volume", 0))
            break
    if repo_git_bytes is None:
        raise RuntimeError(f"CNB git-volume response did not contain repository: {repo}")
    quota_data = _data(quota_response) or {}
    group_volume_data = _data(group_volume_response) or {}
    quota_total = int((quota_data.get("object_in_byte") or {}).get("total", 0))
    git_quota_total = int((quota_data.get("git_in_byte") or {}).get("total", 0))
    group_object_used = int(group_volume_data.get("object_in_byte", 0))
    group_git_used = int(group_volume_data.get("git_in_byte", 0))
    group_object_free = quota_total - group_object_used
    group_git_free = git_quota_total - group_git_used

    tag_data = _data(tags_response) or {}
    runtime_tags = [str(row.get("name", "")) for row in tag_data.get(runtime["package_type"], [])]
    runtime_present = str(runtime["tag"]) in runtime_tags
    threshold = int(policy["clean_repo_object_bytes_max"])
    git_threshold = int(policy["clean_repo_git_bytes_max"])
    minimum_group_free = int(policy["minimum_group_object_free_bytes"])
    minimum_group_git_free = int(policy["minimum_group_git_free_bytes"])
    lfs_storage_clean = repo_object_bytes <= threshold and group_object_free >= minimum_group_free
    git_storage_ready = repo_git_bytes <= git_threshold and group_git_free >= minimum_group_git_free
    checks = {
        "required_runtime_present": runtime_present,
        "no_campaign_branches": not cleanup_branches,
        "no_active_campaign_workspaces": not active_campaign_branches,
        "no_temporary_assets": not cleanup_assets,
        "repo_object_bytes_within_clean_limit": repo_object_bytes <= threshold,
        "repo_git_bytes_within_clean_limit": repo_git_bytes <= git_threshold,
        "group_object_free_bytes_sufficient": group_object_free >= minimum_group_free,
        "group_git_free_bytes_sufficient": git_storage_ready,
        "no_unclassified_branches": not unexpected_branches,
    }
    transport_ready = lfs_storage_clean if transport == "lfs" else git_storage_ready
    common_clean = (
        runtime_present
        and not cleanup_branches
        and not active_campaign_branches
        and not cleanup_assets
        and not unexpected_branches
    )
    return {
        "action": "inspect",
        "transport": transport,
        "repository": repo,
        "organization": org,
        "clean": common_clean and transport_ready,
        "checks": checks,
        "repo_object_bytes": repo_object_bytes,
        "clean_repo_object_bytes_max": threshold,
        "clean_repo_git_bytes_max": git_threshold,
        "object_quota_total_bytes": quota_total,
        "group_object_used_bytes": group_object_used,
        "group_object_free_bytes": group_object_free,
        "minimum_group_object_free_bytes": minimum_group_free,
        "repo_git_bytes": repo_git_bytes,
        "git_quota_total_bytes": git_quota_total,
        "group_git_used_bytes": group_git_used,
        "group_git_free_bytes": group_git_free,
        "minimum_group_git_free_bytes": minimum_group_git_free,
        "required_runtime": {
            "package": runtime["package_name"],
            "tag": runtime["tag"],
            "present": runtime_present,
        },
        "cleanup_branches": cleanup_branches,
        "active_campaign_branches": active_campaign_branches,
        "unexpected_branches": unexpected_branches,
        "cleanup_assets": cleanup_assets,
        "cleanup_asset_bytes": sum(item["bytes"] for item in cleanup_assets),
        "lfs_reclamation_verified": repo_object_bytes <= threshold,
        "lfs_storage_capacity_verified": lfs_storage_clean,
        "git_storage_capacity_verified": git_storage_ready,
        "storage_capacity_verified": transport_ready,
    }


def cleanup(
    policy: dict[str, Any],
    *,
    confirm: bool,
    runner: Runner = run_cnb,
    transport: str = "lfs",
) -> dict[str, Any]:
    before = inspect(policy, runner, transport=transport)
    plan = {
        "branches": before["cleanup_branches"],
        "assets": before["cleanup_assets"],
    }
    if not confirm:
        return {"action": "cleanup", "dry_run": True, "before": before, "plan": plan}

    repo = str(policy["repository_slug"])
    deleted_branches: list[str] = []
    deleted_assets: list[str] = []
    failures: list[dict[str, str]] = []
    for branch in plan["branches"]:
        if branch in policy["protected_branches"] or _matches(branch, policy["preserved_branch_patterns"]):
            raise RuntimeError(f"refusing to delete protected/preserved branch: {branch}")
        try:
            runner(["cnb", "git", "delete-branch", "--repo", repo, "--branch", branch, "--verbose"])
            deleted_branches.append(branch)
        except Exception as exc:
            failures.append({"kind": "branch", "id": branch, "error": str(exc)})
    for asset in plan["assets"]:
        try:
            runner(["cnb", "assets", "delete-asset", "--repo", repo, "--assetID", asset["id"], "--verbose"])
            deleted_assets.append(asset["id"])
        except Exception as exc:
            failures.append({"kind": "asset", "id": asset["id"], "error": str(exc)})

    after = inspect(policy, runner, transport=transport)
    return {
        "action": "cleanup",
        "dry_run": False,
        "before": before,
        "plan": plan,
        "deleted_branches": deleted_branches,
        "deleted_asset_ids": deleted_assets,
        "failures": failures,
        "after": after,
        "visible_cleanup_complete": not after["cleanup_branches"] and not after["cleanup_assets"],
        "lfs_reclamation_verified": after["lfs_reclamation_verified"],
        "clean": after["clean"] and not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "cleanup"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--transport", choices=("lfs", "git-objects"), default="lfs")
    parser.add_argument("--confirm-cleanup", action="store_true")
    args = parser.parse_args()
    policy = load_policy(args.policy.expanduser().resolve())
    result = (
        inspect(policy, transport=args.transport)
        if args.action == "inspect"
        else cleanup(policy, confirm=args.confirm_cleanup, transport=args.transport)
    )
    print(json.dumps(result, ensure_ascii=False))
    if args.action == "inspect":
        return 0 if result["clean"] else 3
    if args.confirm_cleanup:
        return 0 if result["clean"] else 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
