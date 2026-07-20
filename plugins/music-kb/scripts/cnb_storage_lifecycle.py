#!/usr/bin/env python3
"""Inspect and clean disposable CNB runtime storage residue.

GitHub is the source of truth for runner code. CNB keeps only the protected
code mirror and the required runtime image; campaign inputs, result branches,
ledgers, temporary assets, and explicitly allowlisted completed repositories
are disposable after their outputs have been exported and verified locally.
Normal campaigns use Git LFS and require CNB's authoritative object counter to
fall below the policy limit. A narrowly bounded ``git-objects`` transport is
available only when orphan LFS has not been reclaimed: it uses the separately
metered ordinary Git storage counter, while retaining the same cleanup rules.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence


Runner = Callable[[Sequence[str]], dict[str, Any]]
GitHubRunner = Callable[[Sequence[str]], dict[str, Any]]


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
        "disposable_repositories",
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
    disposable_repositories = value.get("disposable_repositories")
    if not isinstance(disposable_repositories, list):
        raise ValueError("disposable_repositories must be an array")
    seen_disposable: set[str] = set()
    protected_repository = str(value["repository_slug"])
    for target in disposable_repositories:
        if not isinstance(target, dict):
            raise ValueError("each disposable repository must be an object")
        required_target_fields = {
            "repository_slug",
            "github_source_repository",
            "reason",
            "retained_source",
            "allow_repository_delete",
            "current_workflow_dependency",
            "evidence",
        }
        missing_target_fields = sorted(required_target_fields - set(target))
        if missing_target_fields:
            raise ValueError(
                "disposable repository is missing fields: " + ", ".join(missing_target_fields)
            )
        target_repo = str(target["repository_slug"]).strip()
        if not target_repo or target_repo == protected_repository:
            raise ValueError("disposable repository cannot be the protected runtime repository")
        if target_repo in seen_disposable:
            raise ValueError(f"duplicate disposable repository: {target_repo}")
        seen_disposable.add(target_repo)
        for field in ("github_source_repository", "reason", "retained_source"):
            if not str(target[field]).strip():
                raise ValueError(f"disposable repository {target_repo} has empty {field}")
        if target["allow_repository_delete"] is not True:
            raise ValueError(f"disposable repository {target_repo} is not explicitly deletable")
        if target["current_workflow_dependency"] is not False:
            raise ValueError(
                f"disposable repository {target_repo} must declare current_workflow_dependency=false"
            )
        evidence = target["evidence"]
        if not isinstance(evidence, dict):
            raise ValueError(f"disposable repository {target_repo} evidence must be an object")
        if not str(evidence.get("verified_at", "")).strip():
            raise ValueError(f"disposable repository {target_repo} evidence needs verified_at")
        for count_field in ("completed_result_count", "failed_result_count"):
            try:
                count = int(evidence[count_field])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"disposable repository {target_repo} evidence needs integer {count_field}"
                ) from exc
            if count < 0:
                raise ValueError(
                    f"disposable repository {target_repo} evidence {count_field} must be non-negative"
                )
    server_gc = value.setdefault("server_gc", {})
    if not isinstance(server_gc, dict):
        raise ValueError("server_gc must be an object")
    try:
        grace_days = int(server_gc.setdefault("unreferenced_object_grace_days", 7))
    except (TypeError, ValueError) as exc:
        raise ValueError("server_gc.unreferenced_object_grace_days must be an integer") from exc
    if grace_days <= 0:
        raise ValueError("server_gc.unreferenced_object_grace_days must be positive")
    server_gc.setdefault("support_issue", "cnb/feedback#4551")
    server_gc.setdefault("manual_gc_api", False)
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
    if not completed.stdout.strip() and completed.returncode == 0:
        # Some destructive CNB endpoints legitimately return HTTP 204 with an
        # empty body. Keep the response shape stable for receipt generation.
        return {"status": 204, "data": None}
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CNB command returned invalid JSON: {' '.join(command)}") from exc
    if not isinstance(value, dict) or value.get("status") not in (200, 201, 204):
        raise RuntimeError(f"CNB command returned an unsuccessful response: {value}")
    return value


def run_github(command: Sequence[str]) -> dict[str, Any]:
    """Run a read-only GitHub CLI query used by destructive cleanup gates."""

    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"GitHub command failed with exit={completed.returncode}: {' '.join(command)}\n"
            f"{completed.stderr.strip()}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub command returned invalid JSON: {' '.join(command)}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"GitHub command returned a non-object response: {' '.join(command)}")
    return value


def _data(response: dict[str, Any]) -> Any:
    return response.get("data")


def _is_not_found_response(response: dict[str, Any]) -> bool:
    status = response.get("status")
    if status == 404 or str(status) == "404":
        return True
    data = response.get("data")
    return isinstance(data, dict) and data.get("errcode") == 5


def _looks_like_not_found_error(error: Exception) -> bool:
    message = str(error).lower()
    return "404" in message or "resource not found" in message or "not found" in message


def _optional_cnb(
    command: Sequence[str],
    runner: Runner,
) -> tuple[dict[str, Any] | None, bool]:
    """Return (response, absent) while treating CNB 404 as an idempotent state."""

    try:
        response = runner(command)
    except Exception as exc:
        if _looks_like_not_found_error(exc):
            return None, True
        raise
    if _is_not_found_response(response):
        return None, True
    return response, False


def _repo_object_volume(
    organization: str,
    repository: str,
    runner: Runner,
    *,
    allow_missing: bool = False,
) -> int:
    response, absent = _optional_cnb(
        [
            "cnb",
            "charge",
            "get-repos-volume",
            "--slug",
            organization,
            "--type",
            "charge_type_object",
            "--page",
            "1",
            "--page-size",
            "100",
            "--verbose",
        ],
        runner,
    )
    if absent or response is None:
        return 0
    for row in (_data(response) or []):
        if row.get("slug") == repository:
            return int(row.get("volume", 0))
    if allow_missing:
        return 0
    raise RuntimeError(f"CNB object-volume response did not contain repository: {repository}")


def _group_object_usage(organization: str, runner: Runner) -> int:
    response = runner(["cnb", "charge", "get-volume", "--slug", organization, "--verbose"])
    data = _data(response) or {}
    return int(data.get("object_in_byte", 0))


def _repo_exists(repository: str, runner: Runner) -> bool:
    _, absent = _optional_cnb(
        ["cnb", "repositories", "get-by-id", "--repo", repository, "--verbose"], runner
    )
    return not absent


def _running_workspace_branches(repository: str, runner: Runner) -> list[str]:
    response, absent = _optional_cnb(
        [
            "cnb",
            "workspace",
            "list-workspaces",
            "--slug",
            repository,
            "--status",
            "running",
            "--page",
            "1",
            "--page-size",
            "100",
            "--verbose",
        ],
        runner,
    )
    if absent or response is None:
        return []
    data = _data(response) or {}
    rows = data.get("list", []) if isinstance(data, dict) else []
    return sorted({str(row.get("branch", "")) for row in rows if row.get("branch")})


def _github_source_metadata(
    source_repository: str,
    github_runner: GitHubRunner,
) -> dict[str, Any]:
    response = github_runner(
        [
            "gh",
            "repo",
            "view",
            source_repository,
            "--json",
            "nameWithOwner,defaultBranchRef",
        ]
    )
    name = str(response.get("nameWithOwner", "")).strip()
    if name != source_repository:
        raise RuntimeError(
            f"GitHub source repository mismatch: expected {source_repository}, got {name or '<empty>'}"
        )
    default_branch = response.get("defaultBranchRef")
    branch_name = default_branch.get("name") if isinstance(default_branch, dict) else None
    if not branch_name:
        raise RuntimeError(f"GitHub source repository has no default branch: {source_repository}")
    return {"name_with_owner": name, "default_branch": str(branch_name)}


def _protected_runtime_status(policy: dict[str, Any], runner: Runner) -> dict[str, Any]:
    """Verify the protected Music Flamingo repo/main/runtime survived cleanup."""

    repository = str(policy["repository_slug"])
    if not _repo_exists(repository, runner):
        return {
            "repository_present": False,
            "main_present": False,
            "runtime_present": False,
            "protected": False,
        }
    branches_response = runner(
        [
            "cnb",
            "git",
            "list-branches",
            "--repo",
            repository,
            "--page",
            "1",
            "--page-size",
            "100",
            "--verbose",
        ]
    )
    branches = {str(row.get("name", "")) for row in (_data(branches_response) or [])}
    runtime = policy["required_runtime"]
    tags_response = runner(
        [
            "cnb",
            "registries",
            "list-package-tags",
            "--slug",
            str(runtime["registry_slug"]),
            "--type",
            str(runtime["package_type"]),
            "--name",
            str(runtime["package_name"]),
            "--page",
            "1",
            "--page-size",
            "100",
            "--verbose",
        ]
    )
    tag_data = _data(tags_response) or {}
    tags = {str(row.get("name", "")) for row in tag_data.get(runtime["package_type"], [])}
    runtime_present = str(runtime["tag"]) in tags
    return {
        "repository_present": True,
        "main_present": "main" in branches,
        "runtime_present": runtime_present,
        "protected": "main" in branches and runtime_present,
    }


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
        "group_git_free_bytes_sufficient": group_git_free >= minimum_group_git_free,
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
    server_gc = policy.get("server_gc", {})
    server_gc_pending = common_clean and not lfs_storage_clean
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
        "server_gc": {
            "unreferenced_object_grace_days": int(server_gc.get("unreferenced_object_grace_days", 7)),
            "support_issue": str(server_gc.get("support_issue", "cnb/feedback#4551")),
            "manual_gc_api": bool(server_gc.get("manual_gc_api", False)),
        },
        "server_gc_pending": server_gc_pending,
    }


def _disposable_target_plan(
    policy: dict[str, Any],
    runner: Runner,
    github_runner: GitHubRunner,
) -> list[dict[str, Any]]:
    """Build a read-only plan for the exact destructive-repository allowlist."""

    protected = str(policy["repository_slug"])
    plan: list[dict[str, Any]] = []
    for target in policy.get("disposable_repositories", []):
        repository = str(target["repository_slug"])
        if repository == protected:
            raise RuntimeError(f"refusing to classify protected runtime as disposable: {repository}")
        source_metadata = _github_source_metadata(
            str(target["github_source_repository"]), github_runner
        )
        present = _repo_exists(repository, runner)
        workspaces = _running_workspace_branches(repository, runner) if present else []
        plan.append(
            {
                "repository": repository,
                "github_source_repository": str(target["github_source_repository"]),
                "reason": str(target["reason"]),
                "retained_source": str(target["retained_source"]),
                "evidence": dict(target["evidence"]),
                "allow_repository_delete": bool(target["allow_repository_delete"]),
                "current_workflow_dependency": bool(target["current_workflow_dependency"]),
                "present": present,
                "status": "present" if present else "already_absent",
                "running_workspace_branches": workspaces,
                "github_source": source_metadata,
                "object_bytes_before": _repo_object_volume(
                    str(policy["organization_slug"]), repository, runner
                )
                if present
                else 0,
            }
        )
    return plan


def delete_disposable_repositories(
    policy: dict[str, Any],
    *,
    confirm: bool,
    runner: Runner = run_cnb,
    github_runner: GitHubRunner = run_github,
) -> dict[str, Any]:
    """Delete only explicitly allowlisted, completed CNB repositories.

    This is intentionally separate from ordinary branch/asset cleanup. A
    successful destructive run is proven by the target repository becoming
    404/zero-volume, a real organization-volume decrease when bytes existed,
    and the protected Music Flamingo repository/runtime remaining present.
    """

    organization = str(policy["organization_slug"])
    try:
        plan = _disposable_target_plan(policy, runner, github_runner)
        group_before = _group_object_usage(organization, runner)
    except Exception as exc:
        return {
            "action": "delete-disposable-repositories",
            "dry_run": not confirm,
            "confirmed": confirm,
            "plan": [],
            "deleted_repositories": [],
            "failures": [{"kind": "preflight", "repository": "", "error": str(exc)}],
            "repository_cleanup_complete": False,
            "clean": False,
        }
    if not confirm:
        return {
            "action": "delete-disposable-repositories",
            "dry_run": True,
            "confirmed": False,
            "plan": plan,
            "group_object_used_bytes_before": group_before,
            "repository_cleanup_complete": not any(item["present"] for item in plan),
            "failures": [],
        }

    failures: list[dict[str, str]] = []
    # Complete all safety checks before the first destructive request.
    for item in plan:
        if not item["allow_repository_delete"]:
            failures.append(
                {
                    "kind": "policy",
                    "repository": item["repository"],
                    "error": "repository is not explicitly allowlisted for deletion",
                }
            )
        if item["current_workflow_dependency"]:
            failures.append(
                {
                    "kind": "workflow-dependency",
                    "repository": item["repository"],
                    "error": "policy declares a current workflow dependency",
                }
            )
        if item["running_workspace_branches"]:
            failures.append(
                {
                    "kind": "workspace",
                    "repository": item["repository"],
                    "error": "running workspaces: " + ", ".join(item["running_workspace_branches"]),
                }
            )
    try:
        protected_before = _protected_runtime_status(policy, runner)
    except Exception as exc:
        protected_before = {
            "repository_present": False,
            "main_present": False,
            "runtime_present": False,
            "protected": False,
            "error": str(exc),
        }
    if not protected_before["protected"]:
        failures.append(
            {
                "kind": "protected-runtime",
                "repository": str(policy["repository_slug"]),
                "error": "protected Music Flamingo repository/main/runtime is not healthy before deletion",
            }
        )
    if failures:
        return {
            "action": "delete-disposable-repositories",
            "dry_run": False,
            "confirmed": True,
            "plan": plan,
            "deleted_repositories": [],
            "failures": failures,
            "protected_runtime_before": protected_before,
            "group_object_used_bytes_before": group_before,
            "repository_cleanup_complete": False,
        }

    deleted: list[str] = []
    for item in plan:
        if not item["present"]:
            continue
        repository = item["repository"]
        try:
            runner(["cnb", "repositories", "delete-repo", "--repo", repository, "--verbose"])
            deleted.append(repository)
        except Exception as exc:
            failures.append({"kind": "repository", "repository": repository, "error": str(exc)})

    # Re-read every target and the organization counter. Never infer success
    # from a successful delete HTTP response alone.
    try:
        group_after: int | None = _group_object_usage(organization, runner)
    except Exception as exc:
        group_after = None
        failures.append(
            {
                "kind": "organization-charge-verification",
                "repository": organization,
                "error": str(exc),
            }
        )
    verification: list[dict[str, Any]] = []
    for item in plan:
        repository = item["repository"]
        try:
            present_after = _repo_exists(repository, runner)
            object_after = _repo_object_volume(
                organization,
                repository,
                runner,
                allow_missing=True,
            )
        except Exception as exc:
            failures.append(
                {
                    "kind": "repository-verification",
                    "repository": repository,
                    "error": str(exc),
                }
            )
            verification.append(
                {
                    "repository": repository,
                    "status": "verification_error",
                    "present_after": None,
                    "object_bytes_before": int(item["object_bytes_before"]),
                    "object_bytes_after": None,
                    "verified_absent": False,
                }
            )
            continue
        verified_absent = not present_after and object_after == 0
        if not item["present"]:
            status = "already_absent"
        elif verified_absent:
            status = "deleted"
        else:
            status = "verification_failed"
        verification_item = {
            "repository": repository,
            "status": status,
            "present_after": present_after,
            "object_bytes_before": int(item["object_bytes_before"]),
            "object_bytes_after": object_after,
            "verified_absent": verified_absent,
        }
        verification.append(verification_item)
        if not verification_item["verified_absent"]:
            failures.append(
                {
                    "kind": "repository-verification",
                    "repository": repository,
                    "error": (
                        f"repository still present={present_after} or object volume is non-zero="
                        f"{object_after}"
                    ),
                }
            )
    try:
        protected_after = _protected_runtime_status(policy, runner)
    except Exception as exc:
        protected_after = {
            "repository_present": False,
            "main_present": False,
            "runtime_present": False,
            "protected": False,
            "error": str(exc),
        }
    if not protected_after["protected"]:
        failures.append(
            {
                "kind": "protected-runtime-verification",
                "repository": str(policy["repository_slug"]),
                "error": "protected Music Flamingo repository/main/runtime is missing after deletion",
            }
        )
    deleted_with_bytes = any(
        item["present"] and int(item["object_bytes_before"]) > 0 for item in plan
    )
    group_decreased = group_after is not None and group_after < group_before
    group_usage_verified = (
        group_decreased
        if deleted_with_bytes
        else group_after is not None and group_after <= group_before
    )
    if group_after is not None and not group_usage_verified:
        failures.append(
            {
                "kind": "organization-charge-verification",
                "repository": organization,
                "error": f"organization object usage did not decrease: {group_before} -> {group_after}",
            }
        )
    complete = (
        not failures
        and all(item["verified_absent"] for item in verification)
        and group_usage_verified
    )
    return {
        "action": "delete-disposable-repositories",
        "dry_run": False,
        "confirmed": True,
        "plan": plan,
        "deleted_repositories": deleted,
        "failures": failures,
        "verification": verification,
        "protected_runtime_before": protected_before,
        "protected_runtime_after": protected_after,
        "group_object_used_bytes_before": group_before,
        "group_object_used_bytes_after": group_after,
        "group_object_usage_decreased": group_decreased,
        "group_object_usage_verified": group_usage_verified,
        "repository_cleanup_complete": complete,
        "clean": complete,
    }


def cleanup(
    policy: dict[str, Any],
    *,
    confirm: bool,
    runner: Runner = run_cnb,
    transport: str = "lfs",
    confirm_delete_repositories: bool = False,
    github_runner: GitHubRunner = run_github,
) -> dict[str, Any]:
    before = inspect(policy, runner, transport=transport)
    plan = {
        "branches": before["cleanup_branches"],
        "assets": before["cleanup_assets"],
    }
    if not confirm:
        repository_plan = delete_disposable_repositories(
            policy,
            confirm=False,
            runner=runner,
            github_runner=github_runner,
        )
        return {
            "action": "cleanup",
            "dry_run": True,
            "before": before,
            "plan": plan,
            "repository_cleanup": repository_plan,
            "destructive_repository_cleanup_complete": repository_plan[
                "repository_cleanup_complete"
            ],
        }

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
    repository_cleanup: dict[str, Any]
    if confirm_delete_repositories and not failures:
        repository_cleanup = delete_disposable_repositories(
            policy,
            confirm=True,
            runner=runner,
            github_runner=github_runner,
        )
    else:
        repository_cleanup = delete_disposable_repositories(
            policy,
            confirm=False,
            runner=runner,
            github_runner=github_runner,
        )
        if confirm_delete_repositories and failures:
            repository_cleanup = {
                **repository_cleanup,
                "dry_run": False,
                "confirmed": True,
                "blocked_by_visible_cleanup_failures": True,
                "repository_cleanup_complete": False,
            }
    visible_cleanup_complete = not after["cleanup_branches"] and not after["cleanup_assets"]
    destructive_complete = bool(repository_cleanup.get("repository_cleanup_complete"))
    return {
        "action": "cleanup",
        "dry_run": False,
        "before": before,
        "plan": plan,
        "deleted_branches": deleted_branches,
        "deleted_asset_ids": deleted_assets,
        "failures": failures,
        "after": after,
        "visible_cleanup_complete": visible_cleanup_complete,
        "lfs_reclamation_verified": after["lfs_reclamation_verified"],
        "server_gc_pending": after["server_gc_pending"],
        "repository_cleanup": repository_cleanup,
        "repository_cleanup_required": bool(policy.get("disposable_repositories")),
        "destructive_repository_cleanup_confirmed": confirm_delete_repositories,
        "destructive_repository_cleanup_complete": destructive_complete,
        "clean": after["clean"] and not failures and destructive_complete,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("inspect", "cleanup", "delete-disposable-repositories"),
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--transport", choices=("lfs", "git-objects"), default="lfs")
    parser.add_argument("--confirm-cleanup", action="store_true")
    parser.add_argument(
        "--confirm-delete-cnb-repositories",
        action="store_true",
        help="Explicitly authorize destructive deletion of policy-allowlisted disposable CNB repositories",
    )
    args = parser.parse_args()
    policy = load_policy(args.policy.expanduser().resolve())
    if args.action == "inspect":
        result = inspect(policy, transport=args.transport)
    elif args.action == "cleanup":
        result = cleanup(
            policy,
            confirm=args.confirm_cleanup,
            transport=args.transport,
            confirm_delete_repositories=args.confirm_delete_cnb_repositories,
        )
    else:
        result = delete_disposable_repositories(
            policy,
            confirm=args.confirm_delete_cnb_repositories,
        )
    print(json.dumps(result, ensure_ascii=False))
    if args.action == "inspect":
        return 0 if result["clean"] else 3
    if args.action == "delete-disposable-repositories":
        if not args.confirm_delete_cnb_repositories:
            return 0
        return 0 if result["repository_cleanup_complete"] else 4
    if args.confirm_cleanup:
        return 0 if result["clean"] else 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
