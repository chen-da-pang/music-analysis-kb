from __future__ import annotations

import importlib.util
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
