from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "cnb_storage_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("cnb_storage_lifecycle", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def policy() -> dict:
    return {
        "schema_version": 2,
        "organization_slug": "org",
        "repository_slug": "org/repo",
        "clean_repo_object_bytes_max": 25_000_000_000,
        "clean_repo_git_bytes_max": 500_000_000,
        "minimum_group_object_free_bytes": 10_000_000_000,
        "minimum_group_git_free_bytes": 10_000_000_000,
        "server_gc": {
            "unreferenced_object_grace_days": 7,
            "support_issue": "cnb/feedback#4551",
            "manual_gc_api": False,
        },
        "required_runtime": {
            "registry_slug": "org/repo",
            "package_type": "docker",
            "package_name": "runner",
            "tag": "runtime",
        },
        "protected_branches": ["main"],
        "preserved_branch_patterns": [],
        "cleanup_branch_patterns": ["^campaign-", "^campaign-results/"],
        "cleanup_asset_name_patterns": ["^run\\.tar\\.gz$"],
        "disposable_repositories": [],
    }


def fixture_runner(
    *,
    object_bytes: int,
    campaign: bool,
    asset: bool,
    group_bytes: int | None = None,
    git_bytes: int = 1_000_000_000,
    git_group_bytes: int | None = None,
    active_campaign: bool = False,
    result: bool = True,
):
    def run(command):
        joined = " ".join(command)
        if "list-workspaces" in joined:
            rows = [{"branch": "campaign-old", "status": "running"}] if active_campaign else []
            return {"status": 200, "data": {"total": len(rows), "list": rows}}
        if "list-branches" in joined:
            rows = [{"name": "main"}]
            if result:
                rows.append({"name": "campaign-results/old"})
            if campaign:
                rows.append({"name": "campaign-old"})
            return {"status": 200, "data": rows}
        if "list-assets" in joined:
            rows = [{"id": "1", "path": "x/run.tar.gz", "size_in_byte": 10}] if asset else []
            return {"status": 200, "data": rows}
        if "get-repos-volume" in joined and "charge_type_git" in joined:
            return {"status": 200, "data": [{"slug": "org/repo", "volume": str(git_bytes)}]}
        if "get-repos-volume" in joined:
            return {"status": 200, "data": [{"slug": "org/repo", "volume": str(object_bytes)}]}
        if "get-quota" in joined:
            return {
                "status": 200,
                "data": {
                    "object_in_byte": {"total": 100_000_000_000},
                    "git_in_byte": {"total": 100_000_000_000},
                },
            }
        if "get-volume" in joined:
            return {
                "status": 200,
                "data": {
                    "object_in_byte": group_bytes if group_bytes is not None else object_bytes,
                    "git_in_byte": git_group_bytes if git_group_bytes is not None else git_bytes,
                },
            }
        if "list-package-tags" in joined:
            return {"status": 200, "data": {"docker": [{"name": "runtime"}]}}
        raise AssertionError(command)

    return run


def disposable_policy() -> dict:
    value = policy()
    value["disposable_repositories"] = [
        {
            "repository_slug": "org/disposable",
            "github_source_repository": "owner/source",
            "reason": "completed isolated run",
            "retained_source": "GitHub main",
            "allow_repository_delete": True,
            "current_workflow_dependency": False,
            "evidence": {
                "completed_result_count": 10,
                "failed_result_count": 0,
                "verified_at": "2026-07-20",
            },
        }
    ]
    return value


def github_source_runner(command):
    assert command[:4] == ["gh", "repo", "view", "owner/source"]
    return {"nameWithOwner": "owner/source", "defaultBranchRef": {"name": "main"}}


def destructive_runner_state(
    *,
    target_present: bool = True,
    target_volume: int = 8_000,
    group_volume: int = 20_000,
    running_workspace: bool = False,
    runtime_present: bool = True,
    reclaim_on_delete: bool = True,
):
    state = {
        "target_present": target_present,
        "target_volume": target_volume,
        "group_volume": group_volume,
        "running_workspace": running_workspace,
        "runtime_present": runtime_present,
        "commands": [],
    }

    def run(command):
        state["commands"].append(list(command))
        joined = " ".join(command)
        if "repositories get-by-id" in joined:
            repository = command[command.index("--repo") + 1]
            if repository == "org/repo":
                return {"status": 200, "data": {"path": repository}}
            if repository == "org/disposable" and state["target_present"]:
                return {"status": 200, "data": {"path": repository}}
            return {"status": 404, "data": {"errcode": 5, "errmsg": "Resource not found."}}
        if "list-workspaces" in joined:
            repository = command[command.index("--slug") + 1]
            rows = []
            if repository == "org/disposable" and state["running_workspace"]:
                rows = [{"branch": "campaign-live", "status": "running"}]
            return {"status": 200, "data": {"total": len(rows), "list": rows}}
        if "get-repos-volume" in joined:
            return {
                "status": 200,
                "data": [
                    {"slug": "org/repo", "volume": "1000"},
                    {"slug": "org/disposable", "volume": str(state["target_volume"])},
                ],
            }
        if "charge get-volume" in joined:
            return {"status": 200, "data": {"object_in_byte": state["group_volume"]}}
        if "repositories delete-repo" in joined:
            assert command[command.index("--repo") + 1] == "org/disposable"
            old_volume = state["target_volume"]
            state["target_present"] = False
            if reclaim_on_delete:
                state["target_volume"] = 0
                state["group_volume"] -= old_volume
            return {"status": 200, "data": {}}
        if "list-branches" in joined:
            return {"status": 200, "data": [{"name": "main"}]}
        if "list-package-tags" in joined:
            tags = [{"name": "runtime"}] if state["runtime_present"] else []
            return {"status": 200, "data": {"docker": tags}}
        raise AssertionError(command)

    return state, run


def test_inspect_blocks_stale_campaign_assets_and_lfs_volume() -> None:
    result = MODULE.inspect(
        policy(), fixture_runner(object_bytes=64_000_000_000, campaign=True, asset=True)
    )
    assert result["clean"] is False
    assert result["cleanup_branches"] == ["campaign-old", "campaign-results/old"]
    assert result["cleanup_assets"][0]["id"] == "1"
    assert result["lfs_reclamation_verified"] is False


def test_inspect_accepts_only_runtime_branch_under_limit() -> None:
    result = MODULE.inspect(
        policy(), fixture_runner(object_bytes=23_000_000_000, campaign=False, asset=False, result=False)
    )
    assert result["clean"] is True
    assert result["required_runtime"]["present"] is True
    assert result["lfs_reclamation_verified"] is True


def test_inspect_plans_result_branch_for_disposal() -> None:
    result = MODULE.inspect(
        policy(), fixture_runner(object_bytes=23_000_000_000, campaign=False, asset=False)
    )
    assert result["clean"] is False
    assert result["cleanup_branches"] == ["campaign-results/old"]


def test_inspect_blocks_when_group_quota_has_no_campaign_headroom() -> None:
    result = MODULE.inspect(
        policy(),
        fixture_runner(
            object_bytes=23_000_000_000,
            group_bytes=95_000_000_000,
            campaign=False,
            asset=False,
            result=False,
        ),
    )
    assert result["clean"] is False
    assert result["checks"]["repo_object_bytes_within_clean_limit"] is True
    assert result["checks"]["group_object_free_bytes_sufficient"] is False


def test_cleanup_is_dry_run_without_confirmation() -> None:
    result = MODULE.cleanup(
        policy(), confirm=False, runner=fixture_runner(object_bytes=64_000_000_000, campaign=True, asset=True)
    )
    assert result["dry_run"] is True
    assert result["plan"]["branches"] == ["campaign-old", "campaign-results/old"]


def test_inspect_never_plans_to_delete_a_running_campaign_workspace_branch() -> None:
    result = MODULE.inspect(
        policy(),
        fixture_runner(object_bytes=23_000_000_000, campaign=True, asset=False, active_campaign=True),
        transport="git-objects",
    )
    assert result["clean"] is False
    assert result["active_campaign_branches"] == ["campaign-old"]
    assert result["cleanup_branches"] == ["campaign-results/old"]


def test_git_object_transport_allows_pending_orphan_lfs_only_with_git_headroom() -> None:
    result = MODULE.inspect(
        policy(),
        fixture_runner(
            object_bytes=64_000_000_000,
            group_bytes=95_000_000_000,
            git_bytes=200_000_000,
            git_group_bytes=5_000_000_000,
            campaign=False,
            asset=False,
            result=False,
        ),
        transport="git-objects",
    )
    assert result["clean"] is True
    assert result["transport"] == "git-objects"
    assert result["lfs_reclamation_verified"] is False
    assert result["git_storage_capacity_verified"] is True


def test_git_object_transport_rejects_polluted_repository_even_with_group_headroom() -> None:
    result = MODULE.inspect(
        policy(),
        fixture_runner(
            object_bytes=64_000_000_000,
            group_bytes=95_000_000_000,
            git_bytes=3_300_000_000,
            git_group_bytes=5_000_000_000,
            campaign=False,
            asset=False,
        ),
        transport="git-objects",
    )
    assert result["clean"] is False
    assert result["checks"]["repo_git_bytes_within_clean_limit"] is False


def test_git_group_headroom_check_is_independent_of_repo_git_limit() -> None:
    result = MODULE.inspect(
        policy(),
        fixture_runner(
            object_bytes=64_000_000_000,
            group_bytes=95_000_000_000,
            git_bytes=3_300_000_000,
            git_group_bytes=5_000_000_000,
            campaign=False,
            asset=False,
        ),
        transport="git-objects",
    )
    assert result["checks"]["repo_git_bytes_within_clean_limit"] is False
    assert result["checks"]["group_git_free_bytes_sufficient"] is True


def test_inspect_marks_server_gc_pending_after_visible_refs_are_clean() -> None:
    result = MODULE.inspect(
        policy(),
        fixture_runner(
            object_bytes=64_000_000_000,
            campaign=False,
            asset=False,
            result=False,
        ),
        transport="git-objects",
    )
    assert result["server_gc_pending"] is True
    assert result["server_gc"] == {
        "unreferenced_object_grace_days": 7,
        "support_issue": "cnb/feedback#4551",
        "manual_gc_api": False,
    }


def test_disposable_repository_cleanup_is_read_only_without_separate_confirmation() -> None:
    state, runner = destructive_runner_state()
    result = MODULE.delete_disposable_repositories(
        disposable_policy(),
        confirm=False,
        runner=runner,
        github_runner=github_source_runner,
    )
    assert result["dry_run"] is True
    assert result["repository_cleanup_complete"] is False
    assert result["plan"][0]["status"] == "present"
    assert not any("delete-repo" in " ".join(command) for command in state["commands"])


def test_disposable_repository_cleanup_records_missing_github_source_as_failure() -> None:
    state, runner = destructive_runner_state()

    def missing_source(command):
        raise RuntimeError("GitHub source repository not found")

    result = MODULE.delete_disposable_repositories(
        disposable_policy(),
        confirm=True,
        runner=runner,
        github_runner=missing_source,
    )
    assert result["repository_cleanup_complete"] is False
    assert result["failures"][0]["kind"] == "preflight"
    assert "not found" in result["failures"][0]["error"]
    assert not any("delete-repo" in " ".join(command) for command in state["commands"])


def test_disposable_repository_cleanup_refuses_a_running_workspace() -> None:
    state, runner = destructive_runner_state(running_workspace=True)
    result = MODULE.delete_disposable_repositories(
        disposable_policy(),
        confirm=True,
        runner=runner,
        github_runner=github_source_runner,
    )
    assert result["repository_cleanup_complete"] is False
    assert result["failures"][0]["kind"] == "workspace"
    assert state["target_present"] is True
    assert not any("delete-repo" in " ".join(command) for command in state["commands"])


def test_disposable_repository_cleanup_requires_healthy_protected_runtime() -> None:
    state, runner = destructive_runner_state(runtime_present=False)
    result = MODULE.delete_disposable_repositories(
        disposable_policy(),
        confirm=True,
        runner=runner,
        github_runner=github_source_runner,
    )
    assert result["repository_cleanup_complete"] is False
    assert result["failures"][0]["kind"] == "protected-runtime"
    assert state["target_present"] is True


def test_disposable_repository_cleanup_deletes_and_verifies_real_charge_drop() -> None:
    state, runner = destructive_runner_state()
    result = MODULE.delete_disposable_repositories(
        disposable_policy(),
        confirm=True,
        runner=runner,
        github_runner=github_source_runner,
    )
    assert result["repository_cleanup_complete"] is True
    assert result["deleted_repositories"] == ["org/disposable"]
    assert result["verification"] == [
        {
            "repository": "org/disposable",
            "status": "deleted",
            "present_after": False,
            "object_bytes_before": 8000,
            "object_bytes_after": 0,
            "verified_absent": True,
        }
    ]
    assert result["group_object_used_bytes_before"] == 20_000
    assert result["group_object_used_bytes_after"] == 12_000
    assert result["group_object_usage_decreased"] is True
    assert result["group_object_usage_verified"] is True
    assert result["protected_runtime_after"]["protected"] is True
    assert state["target_present"] is False


def test_disposable_repository_cleanup_is_idempotent_when_target_is_already_absent() -> None:
    state, runner = destructive_runner_state(
        target_present=False,
        target_volume=0,
        group_volume=12_000,
    )
    result = MODULE.delete_disposable_repositories(
        disposable_policy(),
        confirm=True,
        runner=runner,
        github_runner=github_source_runner,
    )
    assert result["repository_cleanup_complete"] is True
    assert result["plan"][0]["status"] == "already_absent"
    assert result["verification"][0]["status"] == "already_absent"
    assert result["deleted_repositories"] == []
    assert result["group_object_used_bytes_before"] == result["group_object_used_bytes_after"]
    assert result["group_object_usage_decreased"] is False
    assert result["group_object_usage_verified"] is True
    assert not any("delete-repo" in " ".join(command) for command in state["commands"])


def test_disposable_repository_cleanup_does_not_claim_success_until_charge_is_zero() -> None:
    _, runner = destructive_runner_state(reclaim_on_delete=False)
    result = MODULE.delete_disposable_repositories(
        disposable_policy(),
        confirm=True,
        runner=runner,
        github_runner=github_source_runner,
    )
    assert result["repository_cleanup_complete"] is False
    assert {failure["kind"] for failure in result["failures"]} == {
        "repository-verification",
        "organization-charge-verification",
    }
    assert result["verification"][0]["present_after"] is False
    assert result["verification"][0]["object_bytes_after"] == 8000


def test_policy_refuses_to_allowlist_the_protected_runtime_repository(tmp_path: Path) -> None:
    value = disposable_policy()
    value["disposable_repositories"][0]["repository_slug"] = value["repository_slug"]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    try:
        MODULE.load_policy(policy_path)
    except ValueError as exc:
        assert "protected runtime" in str(exc)
    else:
        raise AssertionError("protected runtime repository was accepted as disposable")


def test_production_policy_classifies_only_the_migrated_issue27_code_branches() -> None:
    policy_path = SCRIPT.parents[1] / "references" / "cnb-storage-policy.json"
    production = json.loads(policy_path.read_text(encoding="utf-8"))
    migrated = {
        "codex/issue-27-canonical-direct-delivery",
        "codex/issue-27-direct-quality-gate",
        "codex/issue-27-sharded-ledger-index-fix",
        "codex/issue-27-zero-pending-fix",
    }
    for branch in migrated:
        assert MODULE._matches(branch, production["cleanup_branch_patterns"])
    assert not MODULE._matches("codex/issue-27-unreviewed-future-work", production["cleanup_branch_patterns"])
    assert production["repository_slug"] == "wuyoumusic/moss-music-runner"
    assert [row["repository_slug"] for row in production["disposable_repositories"]] == [
        "wuyoumusic/guohang-asr-benchmark"
    ]
    assert all(
        row["repository_slug"] != production["repository_slug"]
        for row in production["disposable_repositories"]
    )
