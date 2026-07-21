#!/usr/bin/env python3
"""Create, submit, and retire one disposable CNB Music Flamingo campaign.

The publisher owns the orchestration and GitHub remains the only source of
runner code.  This adapter creates a short-lived CNB repository containing a
pinned, code-only runner export plus one campaign's manifest/audio.  It never
falls back to another repository name after a failure: the receipt and exact
slug are the recovery boundary.

The module is intentionally usable both as a CLI and as a small testable
library.  ``prepare_campaign_repository`` is the repository atom,
``submit_campaign`` is the build/ledger atom, and
``cleanup_campaign_repository`` is the destructive post-release atom.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

try:
    from music_kb.operation_context import load_validated_operations, now_iso, sha256_file
except ModuleNotFoundError:  # Allow the documented direct ``python script.py`` invocation.
    _PLUGIN_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_PLUGIN_ROOT / "src"))
    from music_kb.operation_context import load_validated_operations, now_iso, sha256_file


RECEIPT_SCHEMA_VERSION = 1
SAFE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_CAMPAIGN_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_BUILD_STATES = {"success", "error", "cancel", "cancelled", "failed", "skipped"}
DEFAULT_PROMPT = (
    "Describe this track in full detail - tell me the genre, tempo, broad tonal center, "
    "rhythmic feel, instrumentation, vocal character, production style, structure, and "
    "overall mood and atmosphere it creates. Focus only on audible musical and performance "
    "details. Ignore lyrical content entirely and do not mention, summarize, quote, or "
    "transcribe any lyrics."
)
REQUIRED_CAMPAIGN_RUNTIME_FILES = (
    "scripts/run_music_flamingo_campaign.sh",
    "scripts/campaign_ledger_git.sh",
    "scripts/music_flamingo_run_context.py",
    "scripts/prepare_kugou_campaign_shard.sh",
    "scripts/run_music_flamingo_batch.py",
    "scripts/package_music_flamingo_run.sh",
    "scripts/build_kugou_canonical_delivery.py",
)

JsonRunner = Callable[[Sequence[str]], dict[str, Any]]


class CampaignRepositoryError(RuntimeError):
    """Raised when a campaign repository safety or lifecycle check fails."""


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignRepositoryError(f"receipt/policy is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignRepositoryError(f"expected a JSON object: {path}")
    return value


def _run(command: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CampaignRepositoryError(
            f"command failed ({completed.returncode}): {' '.join(command)}{': ' + detail if detail else ''}"
        )
    return completed


def _run_json(command: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None) -> dict[str, Any]:
    completed = _run(command, cwd=cwd, timeout=timeout)
    output = completed.stdout.strip()
    if not output:
        raise CampaignRepositoryError(f"command returned no JSON: {' '.join(command)}")
    value: Any = None
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        # ``cnb --verbose`` emits pretty-printed JSON, while some versions
        # prepend a short human-readable line.  Decode the object that consumes
        # the furthest suffix rather than assuming the last physical line is a
        # complete JSON value.
        decoder = json.JSONDecoder()
        best_end = -1
        for index, character in enumerate(output):
            if character not in "[{":
                continue
            try:
                candidate, end = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            if end > best_end:
                value, best_end = candidate, end
        if best_end < 0:
            raise CampaignRepositoryError(f"command returned invalid JSON: {' '.join(command)}")
    if not isinstance(value, dict):
        raise CampaignRepositoryError(f"command JSON is not an object: {' '.join(command)}")
    return value


def run_cnb(command: Sequence[str]) -> dict[str, Any]:
    """Run a CNB CLI command and return its JSON response."""

    return _run_json(command)


def _response_data(response: Mapping[str, Any]) -> Any:
    return response.get("data")


def _is_not_found(response: Mapping[str, Any]) -> bool:
    if str(response.get("status")) == "404":
        return True
    data = response.get("data")
    return isinstance(data, Mapping) and str(data.get("errcode")) == "5"


def _cnb_optional(command: Sequence[str], runner: JsonRunner) -> tuple[dict[str, Any] | None, bool]:
    try:
        response = runner(command)
    except Exception as exc:  # pragma: no cover - exercised by integration failures
        text = str(exc).lower()
        if "404" in text or "not found" in text or "resource not found" in text:
            return None, True
        raise
    if _is_not_found(response):
        return None, True
    return response, False


def load_campaign_policy(path: str | Path) -> dict[str, Any]:
    """Load and validate the campaign-specific extension of storage policy."""

    policy = _read_json(Path(path).expanduser().resolve())
    if policy.get("schema_version") != 2:
        raise CampaignRepositoryError("CNB storage policy schema must be 2")
    required = {
        "organization_slug",
        "repository_slug",
        "protected_runtime_repository_slug",
        "campaign_repository_prefix",
        "verified_runtime_image_digest",
        "campaign_repository",
    }
    missing = sorted(required - set(policy))
    if missing:
        raise CampaignRepositoryError(f"CNB policy is missing campaign fields: {missing}")
    protected = str(policy["protected_runtime_repository_slug"]).strip()
    if protected != str(policy["repository_slug"]).strip():
        raise CampaignRepositoryError("protected_runtime_repository_slug must equal repository_slug")
    prefix = str(policy["campaign_repository_prefix"]).strip()
    if not prefix or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,59}-", prefix):
        raise CampaignRepositoryError("campaign_repository_prefix must be a lowercase slug prefix ending in '-'")
    image = str(policy["verified_runtime_image_digest"]).strip()
    match = re.fullmatch(r"(.+)@sha256:([0-9a-f]{64})", image)
    if not match:
        raise CampaignRepositoryError("verified_runtime_image_digest must be image@sha256:<64 lowercase hex>")
    campaign = policy["campaign_repository"]
    if not isinstance(campaign, dict):
        raise CampaignRepositoryError("campaign_repository must be an object")
    for key in ("visibility", "branch", "event", "transport", "runner_tag", "ledger_branch_template", "runtime_image"):
        if not str(campaign.get(key, "")).strip():
            raise CampaignRepositoryError(f"campaign_repository.{key} is required")
    if campaign["runtime_image"] != image:
        raise CampaignRepositoryError("campaign_repository.runtime_image must equal verified_runtime_image_digest")
    if campaign["transport"] not in {"lfs", "git-objects"}:
        raise CampaignRepositoryError("campaign_repository.transport must be lfs or git-objects")
    if not str(campaign["event"]).startswith("api_trigger_"):
        raise CampaignRepositoryError("campaign_repository.event must start with api_trigger_")
    shard_count = campaign.get("shard_count")
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or shard_count < 1:
        raise CampaignRepositoryError("campaign_repository.shard_count must be a positive integer")
    for key in ("max_new_tokens", "max_git_object_bytes", "max_git_object_file_bytes"):
        value = campaign.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CampaignRepositoryError(f"campaign_repository.{key} must be a positive integer")
    if float(campaign.get("audio_clip_seconds", 0)) <= 0:
        raise CampaignRepositoryError("campaign_repository.audio_clip_seconds must be positive")
    return policy


def _policy_with_transport(policy: Mapping[str, Any], transport: str | None) -> dict[str, Any]:
    value = copy.deepcopy(dict(policy))
    if transport is not None:
        if transport not in {"lfs", "git-objects"}:
            raise CampaignRepositoryError("campaign transport override must be lfs or git-objects")
        value["campaign_repository"]["transport"] = transport
    return value


def validate_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not SAFE_RUN_ID.fullmatch(value) or value in {"main", "master", "runtime", "latest"}:
        raise CampaignRepositoryError(
            "run_id must be a lowercase slug-safe value (a-z, 0-9, '.', '_' or '-') and not a reserved name"
        )
    return value


def campaign_repository_name(policy: Mapping[str, Any], run_id: str) -> str:
    value = validate_run_id(run_id)
    prefix = str(policy["campaign_repository_prefix"])
    if value.startswith(prefix):
        raise CampaignRepositoryError("run_id must not already contain campaign_repository_prefix")
    name = f"{prefix}{value}"
    if not SAFE_CAMPAIGN_NAME.fullmatch(name):
        raise CampaignRepositoryError("generated campaign repository name is unsafe or too long")
    return name


def _full_repository_slug(policy: Mapping[str, Any], run_id: str) -> tuple[str, str]:
    name = campaign_repository_name(policy, run_id)
    organization = str(policy["organization_slug"]).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,59}", organization):
        raise CampaignRepositoryError(f"unsafe organization slug: {organization!r}")
    return name, f"{organization}/{name}"


def _validate_commit(repository_root: Path, commit: str, *, allow_unpublished: bool = False) -> str:
    value = str(commit).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CampaignRepositoryError("github_commit must be a full 40-character lowercase SHA")
    _run(["git", "-C", str(repository_root), "cat-file", "-e", f"{value}^{{commit}}"])
    remote = _run(["git", "-C", str(repository_root), "remote", "get-url", "origin"]).stdout.strip()
    canonical = remote.removesuffix(".git").rstrip("/")
    if canonical.startswith("git@github.com:"):
        canonical = "https://github.com/" + canonical.removeprefix("git@github.com:")
    if canonical != "https://github.com/chen-da-pang/music-analysis-kb":
        raise CampaignRepositoryError(f"origin must be GitHub source repository, got {remote!r}")
    if not allow_unpublished:
        probe = subprocess.run(
            ["git", "-C", str(repository_root), "merge-base", "--is-ancestor", value, "refs/remotes/origin/main"],
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            raise CampaignRepositoryError(f"GitHub commit is not reachable from origin/main: {value}")
    return value


def _read_manifest(staging: Path, *, expected_count: int | None = None) -> dict[str, Any]:
    manifest = staging / "manifest.jsonl"
    if not manifest.is_file():
        raise CampaignRepositoryError(f"campaign staging is missing manifest.jsonl: {manifest}")
    try:
        raw = manifest.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CampaignRepositoryError(f"campaign manifest is unreadable: {manifest}: {exc}") from exc
    if b"\r" in raw:
        raise CampaignRepositoryError("campaign manifest must use physical LF line endings")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    campaign_id: str | None = None
    for line_number, line in enumerate(text.split("\n"), 1):
        if not line:
            if line_number == len(text.split("\n")):
                continue
            raise CampaignRepositoryError(f"campaign manifest has an empty line at {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CampaignRepositoryError(f"campaign manifest JSON error at line {line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise CampaignRepositoryError(f"campaign manifest row {line_number} is not an object")
        item_id = str(row.get("id", "")).strip()
        if not item_id or item_id in seen or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item_id):
            raise CampaignRepositoryError(f"campaign manifest row {line_number} has missing/duplicate/unsafe id")
        seen.add(item_id)
        row_campaign_id = str(row.get("campaign_id", "")).strip()
        if not row_campaign_id:
            raise CampaignRepositoryError(f"campaign manifest row {line_number} has no campaign_id")
        if campaign_id is None:
            campaign_id = row_campaign_id
        elif row_campaign_id != campaign_id:
            raise CampaignRepositoryError("campaign manifest mixes campaign_id values")
        relative = str(row.get("relative_audio_path", "")).strip()
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != "audio":
            raise CampaignRepositoryError(f"campaign manifest row {line_number} has unsafe relative_audio_path")
        source = (staging / Path(*pure.parts)).resolve()
        try:
            source.relative_to(staging.resolve())
        except ValueError as exc:
            raise CampaignRepositoryError(f"campaign manifest row {line_number} escapes staging") from exc
        if not source.is_file():
            raise CampaignRepositoryError(f"campaign audio is missing for {item_id}: {source}")
        source_bytes = row.get("source_bytes")
        if isinstance(source_bytes, bool) or not isinstance(source_bytes, int) or source_bytes <= 0:
            raise CampaignRepositoryError(f"campaign manifest row {line_number} has invalid source_bytes")
        actual_bytes = source.stat().st_size
        if actual_bytes != source_bytes:
            raise CampaignRepositoryError(f"campaign audio byte mismatch for {item_id}: {actual_bytes} != {source_bytes}")
        expected_sha = str(row.get("sha256", "")).strip().lower()
        if not SHA256.fullmatch(expected_sha) or sha256_file(source) != expected_sha:
            raise CampaignRepositoryError(f"campaign audio sha256 mismatch for {item_id}")
        source_url = row.get("source_url")
        if source_url:
            parsed = urlsplit(str(source_url))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CampaignRepositoryError(f"campaign source_url is unsafe for {item_id}")
        rows.append(row)
    if expected_count is not None and len(rows) != expected_count:
        raise CampaignRepositoryError(f"campaign manifest count {len(rows)} != expected {expected_count}")
    if not rows:
        raise CampaignRepositoryError("campaign manifest is empty")
    return {
        "path": str(manifest.resolve()),
        "sha256": sha256_file(manifest),
        "item_count": len(rows),
        "source_bytes": sum(int(row["source_bytes"]) for row in rows),
        "source_links": sum(1 for row in rows if row.get("source_url")),
        "campaign_id": campaign_id,
    }


def _manifest_binding_fields(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable manifest fields that bind a retry to its input."""

    return {
        "sha256": str(summary.get("sha256", "")),
        "item_count": int(summary.get("item_count", 0)),
        "source_bytes": int(summary.get("source_bytes", 0)),
        "source_links": int(summary.get("source_links", 0)),
        "campaign_id": str(summary.get("campaign_id", "")),
    }


def campaign_receipt_binding_errors(
    policy: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    run_id: str | None = None,
    github_commit: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    transport: str | None = None,
    operations_sha256: str | None = None,
) -> list[str]:
    """Validate the immutable identity of a campaign receipt.

    This is deliberately separate from the destructive validator below.  It
    is used before a retry to prove that a receipt belongs to the exact run,
    source commit, runtime, transport and manifest being resumed.
    """

    errors: list[str] = []
    receipt_run_id = str(receipt.get("run_id", "")).strip()
    expected_run_id = str(run_id if run_id is not None else receipt_run_id).strip()
    try:
        _, expected_repository = _full_repository_slug(policy, expected_run_id)
    except CampaignRepositoryError as exc:
        errors.append(f"invalid run_id: {exc}")
        expected_repository = ""
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        errors.append("receipt schema_version does not match")
    if receipt.get("atom") != "cnb_campaign_repository":
        errors.append("receipt atom is not cnb_campaign_repository")
    if operations_sha256 is not None and receipt.get("operations_sha256") != operations_sha256:
        errors.append("receipt operations_sha256 does not match the validated operations file")
    if not receipt_run_id or receipt_run_id != expected_run_id:
        errors.append("receipt run_id does not match the requested run")
    if expected_repository and receipt.get("repository") != expected_repository:
        errors.append("receipt repository does not match the strict run-id/prefix mapping")
    expected_prefix = str(policy.get("campaign_repository_prefix", ""))
    if not str(receipt.get("repository_prefix", "")) == expected_prefix:
        errors.append("receipt repository_prefix does not match policy")
    if receipt.get("organization") != policy.get("organization_slug"):
        errors.append("receipt organization does not match policy")
    if receipt.get("github_repository") != "chen-da-pang/music-analysis-kb":
        errors.append("receipt GitHub source repository is not the canonical repository")
    if github_commit is not None and receipt.get("github_commit") != github_commit:
        errors.append("receipt github_commit does not match the requested source commit")
    expected_image = str(policy.get("verified_runtime_image_digest", ""))
    if receipt.get("runtime_image") != expected_image:
        errors.append("receipt runtime_image does not match policy")
    expected_digest = expected_image.split("@", 1)[-1]
    if receipt.get("runtime_digest") != expected_digest:
        errors.append("receipt runtime_digest does not match policy")
    expected_transport = transport or str(policy.get("campaign_repository", {}).get("transport", ""))
    if receipt.get("transport") != expected_transport:
        errors.append("receipt transport does not match policy")
    receipt_manifest = receipt.get("manifest")
    if not isinstance(receipt_manifest, Mapping):
        errors.append("receipt manifest is missing")
    elif manifest is not None:
        actual = _manifest_binding_fields(receipt_manifest)
        expected = _manifest_binding_fields(manifest)
        if actual != expected:
            errors.append("receipt manifest hash/count/bytes/source-link binding does not match")
    return errors


def validate_campaign_receipt_binding(
    policy: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    run_id: str | None = None,
    github_commit: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    transport: str | None = None,
    operations_sha256: str | None = None,
) -> dict[str, Any]:
    errors = campaign_receipt_binding_errors(
        policy,
        receipt,
        run_id=run_id,
        github_commit=github_commit,
        manifest=manifest,
        transport=transport,
        operations_sha256=operations_sha256,
    )
    return {
        "valid": not errors,
        "errors": errors,
        "run_id": receipt.get("run_id"),
        "repository": receipt.get("repository"),
    }


def _repo_exists(repository: str, runner: JsonRunner) -> bool:
    _, absent = _cnb_optional(["cnb", "repositories", "get-by-id", "--repo", repository, "--verbose"], runner)
    return not absent


def _repo_volume(organization: str, repository: str, runner: JsonRunner) -> int:
    response, absent = _cnb_optional(
        [
            "cnb", "charge", "get-repos-volume", "--slug", organization, "--type", "charge_type_object",
            "--page", "1", "--page-size", "100", "--verbose",
        ],
        runner,
    )
    if absent or response is None:
        return 0
    for row in (_response_data(response) or []):
        if row.get("slug") == repository:
            return int(row.get("volume", 0))
    return 0


def _group_volume(organization: str, runner: JsonRunner) -> dict[str, int]:
    response = runner(["cnb", "charge", "get-volume", "--slug", organization, "--verbose"])
    data = _response_data(response) or {}
    return {"object": int(data.get("object_in_byte", 0)), "git": int(data.get("git_in_byte", 0))}


def _group_quota(organization: str, runner: JsonRunner) -> dict[str, int]:
    response = runner(["cnb", "charge", "get-quota", "--slug", organization, "--verbose"])
    data = _response_data(response) or {}
    return {
        "object": int((data.get("object_in_byte") or {}).get("total", 0)),
        "git": int((data.get("git_in_byte") or {}).get("total", 0)),
    }


def _protected_runtime_status(policy: Mapping[str, Any], runner: JsonRunner) -> dict[str, Any]:
    repository = str(policy["protected_runtime_repository_slug"])
    present = _repo_exists(repository, runner)
    if not present:
        return {"repository_present": False, "main_present": False, "runtime_present": False, "digest_match": False, "protected": False}
    branches_response = runner(["cnb", "git", "list-branches", "--repo", repository, "--page", "1", "--page-size", "100", "--verbose"])
    branches = {str(row.get("name", "")) for row in (_response_data(branches_response) or [])}
    campaign = policy["campaign_repository"]
    runtime_response = runner(
        [
            "cnb", "registries", "get-package-tag-detail", "--slug", repository, "--type", "docker",
            "--name", "moss-music-runner", "--tag", str(policy["required_runtime"]["tag"]),
            "--arch", "linux/amd64", "--verbose",
        ]
    )
    runtime_data = _response_data(runtime_response) or {}
    image = (runtime_data.get("docker") or {}).get("image") or {}
    actual_digest = str(image.get("digest", ""))
    expected_digest = str(policy["verified_runtime_image_digest"]).split("@", 1)[-1]
    digest_match = actual_digest == expected_digest
    runtime_present = bool(actual_digest)
    return {
        "repository_present": True,
        "main_present": "main" in branches,
        "runtime_present": runtime_present,
        "digest_match": digest_match,
        "expected_digest": expected_digest,
        "actual_digest": actual_digest,
        "protected": "main" in branches and runtime_present and digest_match,
    }


def _campaign_repositories(policy: Mapping[str, Any], runner: JsonRunner) -> list[str]:
    organization = str(policy["organization_slug"])
    prefix = str(policy["campaign_repository_prefix"])
    response = runner(
        [
            "cnb", "repositories", "get-group-sub-repos", "--slug", organization, "--page", "1",
            "--page-size", "100", "--status", "active", "--search", prefix, "--descendant", "all", "--verbose",
        ]
    )
    data = _response_data(response) or {}
    rows = data.get("list", data) if isinstance(data, dict) else data
    names: list[str] = []
    for row in rows or []:
        slug = str(row.get("path") or row.get("slug") or row.get("nameWithOwner") or "")
        if slug.startswith(f"{organization}/{prefix}"):
            names.append(slug)
    return sorted(set(names))


def campaign_preflight(
    policy: Mapping[str, Any],
    *,
    runner: JsonRunner = run_cnb,
    estimated_bytes: int = 0,
    target_repository: str | None = None,
    resume_repository: str | None = None,
    largest_file_bytes: int | None = None,
) -> dict[str, Any]:
    """Read-only preflight for the disposable campaign route."""

    if estimated_bytes < 0:
        raise CampaignRepositoryError("estimated_bytes must be non-negative")
    protected = _protected_runtime_status(policy, runner)
    organization = str(policy["organization_slug"])
    volume = _group_volume(organization, runner)
    quota = _group_quota(organization, runner)
    campaign_repositories = _campaign_repositories(policy, runner)
    if resume_repository is not None:
        # A resume may reuse only the exact repository recorded by the same
        # receipt.  Every other campaign repository remains a hard blocker.
        organization = str(policy["organization_slug"])
        expected_prefix = f"{organization}/{policy['campaign_repository_prefix']}"
        if not resume_repository.startswith(expected_prefix):
            raise CampaignRepositoryError(
                f"resume repository does not match the strict campaign prefix: {resume_repository}"
            )
        resume_name = resume_repository.split("/", 1)[-1]
        resume_run_id = resume_name.removeprefix(str(policy["campaign_repository_prefix"]))
        try:
            if campaign_repository_name(policy, resume_run_id) != resume_name:
                raise CampaignRepositoryError("resume repository name does not map to a safe run_id")
        except CampaignRepositoryError as exc:
            raise CampaignRepositoryError(f"resume repository is not an exact campaign slug: {resume_repository}") from exc
    other_campaign_repositories = [
        value for value in campaign_repositories if value != resume_repository
    ]
    target_present = bool(target_repository and _repo_exists(target_repository, runner))
    object_free = quota["object"] - volume["object"]
    git_free = quota["git"] - volume["git"]
    campaign = policy["campaign_repository"]
    minimum_object_free = int(policy["minimum_group_object_free_bytes"])
    minimum_git_free = int(policy["minimum_group_git_free_bytes"])
    transport = str(campaign["transport"])
    checks: dict[str, bool] = {
        "protected_runtime": bool(protected["protected"]),
        "no_existing_campaign_repositories": not other_campaign_repositories,
        "git_headroom": git_free >= minimum_git_free,
        "runtime_digest_policy_consistent": str(campaign["runtime_image"]) == str(policy["verified_runtime_image_digest"]),
    }
    if resume_repository is None:
        checks["target_repository_absent"] = not target_present
    else:
        # A failed create/push may leave the target absent; the receipt-bound
        # recovery path is allowed to create it again.  An existing target is
        # also safe because it is the exact receipt slug.
        checks["target_repository_bound"] = True
    if transport == "lfs":
        checks["object_headroom"] = object_free >= minimum_object_free + estimated_bytes
    else:
        checks["git_headroom_for_transport"] = git_free >= minimum_git_free + estimated_bytes
        checks["campaign_total_cap"] = estimated_bytes <= int(campaign["max_git_object_bytes"])
        if largest_file_bytes is not None:
            if largest_file_bytes < 0:
                raise CampaignRepositoryError("largest_file_bytes must be non-negative")
            checks["campaign_file_cap"] = largest_file_bytes <= int(campaign["max_git_object_file_bytes"])
    return {
        "action": "campaign-preflight",
        "transport": transport,
        "clean": all(checks.values()),
        "checks": checks,
        "protected_runtime": protected,
        "existing_campaign_repositories": campaign_repositories,
        "other_campaign_repositories": other_campaign_repositories,
        "target_repository_present": target_present,
        "object_used_bytes": volume["object"],
        "object_free_bytes": object_free,
        "git_used_bytes": volume["git"],
        "git_free_bytes": git_free,
        "estimated_campaign_bytes": estimated_bytes,
        "target_repository": target_repository,
        "resume_repository": resume_repository,
        "largest_file_bytes": largest_file_bytes,
    }


def _copy_tree_with_links(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise CampaignRepositoryError(f"campaign staging directory does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=False)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(item, target)
        except OSError:
            shutil.copy2(item, target)


def _yaml_string(value: object) -> str:
    # JSON double-quoted strings are valid YAML scalars and safely escape all
    # campaign-controlled values without a PyYAML runtime dependency.
    return json.dumps(str(value), ensure_ascii=False)


def generate_campaign_config(
    policy: Mapping[str, Any],
    *,
    campaign_id: str,
    repository_slug: str,
    item_count: int,
    source_manifest_sha256: str,
) -> str:
    campaign = policy["campaign_repository"]
    image = str(policy["verified_runtime_image_digest"])
    transport = str(campaign["transport"])
    shard_count = int(campaign["shard_count"])
    ledger_branch = str(campaign["ledger_branch_template"]).format(campaign_id=campaign_id)
    input_root = f"data/input/{campaign_id}"
    manifest_path = f"{input_root}/manifest.jsonl"
    output_name = f"campaign_{campaign_id}"
    lines = [
        "# Generated by cnb_campaign_repository.py; do not edit in CNB.",
        "$:",
        f"  {campaign['event']}:",
        "  - git:",
        "      lfs: false",
        "    docker:",
        f"      image: {image}",
        "      volumes:",
        "      - music-flamingo-output:/workspace/cache/output:read-write",
        "    runner:",
        f"      tags: {campaign['runner_tag']}",
        "    env:",
        "      PYTHONUNBUFFERED: '1'",
        "      HF_HUB_ENABLE_HF_TRANSFER: '1'",
        "      HF_HOME: /opt/huggingface",
        "      MUSIC_FLAMINGO_MODEL: nvidia/music-flamingo-think-2601-hf",
        "      MUSIC_FLAMINGO_REVISION: 1ea2109",
        "      MUSIC_FLAMINGO_MODEL_DIR: /opt/models/music-flamingo-think-2601-hf",
        f"      CNB_RUNTIME_IMAGE: {image}",
        f"      MUSIC_FLAMINGO_MAX_NEW_TOKENS: '{int(campaign['max_new_tokens'])}'",
        f"      MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS: '{campaign['audio_clip_seconds']}'",
        f"      MUSIC_FLAMINGO_PROMPT: {_yaml_string(DEFAULT_PROMPT)}",
        "      WORK_DIR: /workspace/cache/output/music_flamingo_pipeline",
        "      MUSIC_FLAMINGO_RUN_ID: ${CNB_BUILD_ID}",
        f"      MUSIC_FLAMINGO_OUTPUT_NAME: {_yaml_string(output_name)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_ID: {_yaml_string(campaign_id)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_SOURCE_MANIFEST: {_yaml_string(manifest_path)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT: {_yaml_string(input_root)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_TRANSPORT: {_yaml_string(transport)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_GIT_OBJECTS_MAX_BYTES: '{int(campaign['max_git_object_bytes'])}'",
        f"      MUSIC_FLAMINGO_CAMPAIGN_GIT_OBJECTS_MAX_FILE_BYTES: '{int(campaign['max_git_object_file_bytes'])}'",
        f"      MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT: '{item_count}'",
        "      MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX: '1'",
        f"      MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT: '{shard_count}'",
        f"      MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID: {_yaml_string(campaign_id + '-s1')}",
        "      MUSIC_FLAMINGO_CAMPAIGN_PREFLIGHT_ONLY: '0'",
        "      MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS: '0'",
        "      MUSIC_FLAMINGO_CONTINUE_ON_ERROR: '1'",
        "      MUSIC_FLAMINGO_EXECUTION_PROFILE: nvidia-l40/full_precision/bfloat16",
        "      MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED: '1'",
        "      MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY: '5'",
        f"      MUSIC_FLAMINGO_LEDGER_REPO_URL: {_yaml_string('https://cnb.cool/' + repository_slug + '.git')}",
        f"      MUSIC_FLAMINGO_LEDGER_BRANCH: {_yaml_string(ledger_branch)}",
        "      MUSIC_FLAMINGO_LEDGER_GIT_USER_NAME: CNB Music Campaign Ledger",
        "      MUSIC_FLAMINGO_LEDGER_GIT_USER_EMAIL: cnb-ledger@wuyoumusic.invalid",
        f"      MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256: {_yaml_string(source_manifest_sha256)}",
        "      MUSIC_FLAMINGO_DETAILED_CUDA_TELEMETRY: '0'",
        "    stages:",
        "    - name: Run disposable Music Flamingo campaign shard",
        "      timeout: 4h",
        "      script: bash scripts/run_music_flamingo_campaign.sh",
        "    lock:",
        f"      key: {_yaml_string('music-flamingo-' + campaign_id + '-ledger-writer')}",
        "      wait: true",
        "      timeout: 15000",
        "      expires: 18000",
        "",
    ]
    return "\n".join(lines)


def _write_gitattributes(checkout: Path, campaign_id: str, transport: str) -> None:
    path = checkout / ".gitattributes"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"data/input/{campaign_id}/audio/** filter=lfs diff=lfs merge=lfs -text"
    if transport == "lfs" and line not in existing.splitlines():
        path.write_text(existing.rstrip("\n") + ("\n" if existing else "") + line + "\n", encoding="utf-8")


def _git_push_environment() -> tuple[dict[str, str], Path | None]:
    token = os.environ.get("CNB_TOKEN", "")
    if not token:
        raise CampaignRepositoryError("CNB_TOKEN is required for an executable campaign repository push")
    askpass = Path(tempfile.mkstemp(prefix="music-kb-cnb-askpass.")[1])
    askpass.write_text(
        "#!/bin/sh\ncase \"$1\" in\n  *Username*|*username*) printf '%s\\n' cnb ;;\n  *Password*|*password*) printf '%s\\n' \"$CNB_TOKEN\" ;;\n  *) exit 1 ;;\nesac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    env = os.environ.copy()
    env.update({"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0"})
    return env, askpass


def _export_runtime(
    repository_root: Path,
    github_commit: str,
    output: Path,
    *,
    allow_unpublished: bool = False,
) -> dict[str, Any]:
    exporter = repository_root / "runners" / "cnb-music-flamingo" / "tools" / "export_cnb_runtime.py"
    if not exporter.is_file():
        raise CampaignRepositoryError(f"GitHub runner exporter is missing: {exporter}")
    command = [sys.executable, str(exporter), "--github-commit", github_commit, "--output", str(output)]
    if allow_unpublished:
        command.append("--allow-unpublished")
    _run(command, cwd=repository_root, timeout=600)
    provenance_path = output / ".github-source.json"
    if not provenance_path.is_file():
        raise CampaignRepositoryError("runtime exporter did not write .github-source.json")
    return _read_json(provenance_path)


def _validate_exported_runtime(output: Path, provenance: Mapping[str, Any], github_commit: str) -> dict[str, Any]:
    """Prove that the pinned export contains every campaign execution entry point."""

    if provenance.get("source_commit") != github_commit:
        raise CampaignRepositoryError("runtime export provenance does not match the requested GitHub commit")
    raw_files = provenance.get("files")
    if not isinstance(raw_files, list):
        raise CampaignRepositoryError("runtime export provenance has no file inventory")
    inventory: dict[str, Mapping[str, Any]] = {}
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            raise CampaignRepositoryError("runtime export provenance contains a malformed file record")
        relative = str(raw.get("path", ""))
        if not relative or relative in inventory:
            raise CampaignRepositoryError("runtime export provenance contains a missing or duplicate path")
        inventory[relative] = raw

    missing = [relative for relative in REQUIRED_CAMPAIGN_RUNTIME_FILES if relative not in inventory]
    if missing:
        raise CampaignRepositoryError(
            "runtime export provenance is missing required campaign scripts: " + ", ".join(missing)
        )

    verified: list[dict[str, Any]] = []
    for relative in REQUIRED_CAMPAIGN_RUNTIME_FILES:
        path = output / Path(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            raise CampaignRepositoryError(f"runtime export is missing required regular file: {relative}")
        expected_sha = str(inventory[relative].get("sha256", "")).lower()
        expected_bytes = inventory[relative].get("bytes")
        if not SHA256.fullmatch(expected_sha):
            raise CampaignRepositoryError(f"runtime export provenance has invalid sha256 for {relative}")
        if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 1:
            raise CampaignRepositoryError(f"runtime export provenance has invalid byte count for {relative}")
        actual_bytes = path.stat().st_size
        actual_sha = sha256_file(path)
        if actual_bytes != expected_bytes or actual_sha != expected_sha:
            raise CampaignRepositoryError(f"runtime export content does not match provenance for {relative}")
        verified.append({"path": relative, "bytes": actual_bytes, "sha256": actual_sha})
    return {"validated": True, "required_files": verified}


def _workspace_stage_complete(
    workspace: Path,
    *,
    run_id: str,
    manifest_summary: Mapping[str, Any],
    github_commit: str,
) -> bool:
    """Whether a receipt-bound local campaign workspace can be reused."""

    checkout = workspace / "repo"
    provenance_path = workspace / "runtime-export" / ".github-source.json"
    metadata_path = checkout / "campaign-input.json"
    staged_manifest = checkout / "data" / "input" / run_id / "manifest.jsonl"
    config = checkout / ".cnb.yml"
    if not all(path.is_file() for path in (provenance_path, metadata_path, staged_manifest, config)):
        return False
    try:
        provenance = _read_json(provenance_path)
        if provenance.get("source_commit") != github_commit:
            return False
        metadata = _read_json(metadata_path)
        if metadata.get("run_id") != run_id or metadata.get("github_commit") != github_commit:
            return False
        staged_sha = sha256_file(staged_manifest)
        return (
            staged_sha == manifest_summary.get("sha256")
            and metadata.get("manifest", {}).get("sha256") == manifest_summary.get("sha256")
            and metadata.get("manifest", {}).get("item_count") == manifest_summary.get("item_count")
        )
    except (CampaignRepositoryError, OSError, TypeError, ValueError):
        return False


def _workspace_is_receipt_bound(workspace: Path, receipt: Mapping[str, Any]) -> bool:
    return str(receipt.get("workspace", "")).strip() == str(workspace)


def _largest_manifest_file_bytes(staging: Path) -> int:
    rows = [
        json.loads(line)
        for line in (staging / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return max(int(row["source_bytes"]) for row in rows)


def prepare_campaign_repository(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    repository_root: str | Path,
    run_id: str,
    staging: str | Path,
    run_dir: str | Path,
    github_commit: str,
    expected_count: int | None = None,
    execute: bool = False,
    runner: JsonRunner = run_cnb,
    receipt_path: str | Path | None = None,
    work_dir: str | Path | None = None,
    transport: str | None = None,
    allow_unpublished: bool = False,
) -> dict[str, Any]:
    """Prepare and optionally push one exact disposable repository."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_repository")
    operations_sha256 = sha256_file(operations)
    policy = _policy_with_transport(load_campaign_policy(policy_path), transport)
    if allow_unpublished and execute:
        raise CampaignRepositoryError("--allow-unpublished is permitted only for a non-executing dry-run")
    root = Path(repository_root).expanduser().resolve()
    run_dir_path = Path(run_dir).expanduser().resolve()
    run_dir_path.mkdir(parents=True, exist_ok=True)
    run_id = validate_run_id(run_id)
    name, repository = _full_repository_slug(policy, run_id)
    commit = _validate_commit(root, github_commit, allow_unpublished=allow_unpublished)
    staging_path = Path(staging).expanduser().resolve()
    manifest_summary = _read_manifest(staging_path, expected_count=expected_count)
    if manifest_summary["campaign_id"] != run_id:
        raise CampaignRepositoryError(
            f"campaign staging campaign_id {manifest_summary['campaign_id']!r} != run_id {run_id!r}"
        )
    receipt_file = (
        Path(receipt_path).expanduser().resolve()
        if receipt_path
        else run_dir_path / "cnb" / "campaign-receipt.json"
    )
    existing_receipt: dict[str, Any] | None = None
    resume = receipt_file.is_file()
    if resume:
        existing_receipt = _read_json(receipt_file)
        binding = validate_campaign_receipt_binding(
            policy,
            existing_receipt,
            run_id=run_id,
            github_commit=commit,
            manifest=manifest_summary,
            transport=str(policy["campaign_repository"]["transport"]),
            operations_sha256=operations_sha256,
        )
        if not binding["valid"]:
            raise CampaignRepositoryError(
                "same-run campaign receipt cannot be resumed: " + "; ".join(binding["errors"])
            )
        if existing_receipt.get("allow_unpublished") is not None and bool(existing_receipt.get("allow_unpublished")) != bool(allow_unpublished):
            raise CampaignRepositoryError("same-run campaign receipt allow_unpublished mode does not match")
        if existing_receipt.get("status") == "completed":
            result = dict(existing_receipt)
            result["receipt"] = str(receipt_file)
            return result

    campaign = policy["campaign_repository"]
    largest = _largest_manifest_file_bytes(staging_path)
    if campaign["transport"] == "git-objects":
        if manifest_summary["source_bytes"] > int(campaign["max_git_object_bytes"]):
            raise CampaignRepositoryError("campaign exceeds policy git-object total cap")
        if largest > int(campaign["max_git_object_file_bytes"]):
            raise CampaignRepositoryError("campaign exceeds policy git-object per-file cap")
    preflight = campaign_preflight(
        policy,
        runner=runner,
        estimated_bytes=int(manifest_summary["source_bytes"]),
        target_repository=repository,
        resume_repository=repository if resume else None,
        largest_file_bytes=largest,
    )
    if execute and not preflight["clean"]:
        raise CampaignRepositoryError(f"campaign preflight failed: {preflight}")
    if preflight["other_campaign_repositories"]:
        raise CampaignRepositoryError(
            "existing disposable campaign repositories require the same-receipt cleanup before a new run: "
            + ", ".join(preflight["other_campaign_repositories"])
        )
    if not resume and preflight["target_repository_present"]:
        raise CampaignRepositoryError(
            f"target campaign repository already exists; resume or clean the same receipt: {repository}"
        )
    workspace = (
        Path(str(existing_receipt["workspace"])).expanduser().resolve()
        if resume and existing_receipt and existing_receipt.get("workspace")
        else (Path(work_dir).expanduser().resolve() if work_dir else run_dir_path / "cnb" / "campaign-repository")
    )
    if work_dir is not None and resume and existing_receipt and str(Path(work_dir).expanduser().resolve()) != str(workspace):
        raise CampaignRepositoryError("retry work directory does not match the receipt-bound workspace")
    if resume and work_dir is None:
        try:
            workspace.relative_to(run_dir_path)
        except ValueError as exc:
            raise CampaignRepositoryError(
                "receipt-bound workspace is outside the run directory; repeat the retry with the exact --work-dir"
            ) from exc
    if workspace.exists() and not resume:
        raise CampaignRepositoryError(f"campaign work directory already exists; refusing to overwrite: {workspace}")
    if workspace.exists() and resume:
        if not existing_receipt or not _workspace_is_receipt_bound(workspace, existing_receipt):
            raise CampaignRepositoryError(f"existing campaign work directory is not receipt-bound: {workspace}")
        if not _workspace_stage_complete(workspace, run_id=run_id, manifest_summary=manifest_summary, github_commit=commit):
            # Only a workspace named by the exact receipt may be discarded;
            # this is the recovery path for an interrupted export/stage.
            shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    export_dir = workspace / "runtime-export"
    checkout = workspace / "repo"
    receipt: dict[str, Any] = dict(existing_receipt or {})
    receipt.update(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "atom": "cnb_campaign_repository",
            "status": "planned" if not execute else "preparing",
            "run_id": run_id,
            "repository": repository,
            "repository_name": name,
            "organization": str(policy["organization_slug"]),
            "repository_prefix": str(policy["campaign_repository_prefix"]),
            "github_repository": "chen-da-pang/music-analysis-kb",
            "operations_sha256": operations_sha256,
            "github_commit": commit,
            "allow_unpublished": allow_unpublished,
            "runtime_image": str(policy["verified_runtime_image_digest"]),
            "runtime_digest": str(policy["verified_runtime_image_digest"]).split("@", 1)[-1],
            "transport": campaign["transport"],
            "manifest": manifest_summary,
            "campaign_repository_config": str((checkout / ".cnb.yml").resolve()),
            "workspace": str(workspace),
            "checkout": str(checkout),
            "preflight": preflight,
            "updated_at": now_iso(),
            "builds": receipt.get("builds") if isinstance(receipt.get("builds"), list) else [],
            "delivery": receipt.get("delivery"),
            "failure": None,
            "repository_created": bool(receipt.get("repository_created", False)),
            "repository_pushed": bool(receipt.get("repository_pushed", False)),
        }
    )
    receipt.setdefault("created_at", now_iso())
    _atomic_write_json(receipt_file, receipt)
    try:
        stage_complete = _workspace_stage_complete(
            workspace, run_id=run_id, manifest_summary=manifest_summary, github_commit=commit
        )
        if stage_complete:
            provenance = _read_json(export_dir / ".github-source.json")
            runtime_export = _validate_exported_runtime(export_dir, provenance, commit)
        else:
            provenance = _export_runtime(root, commit, export_dir, allow_unpublished=allow_unpublished)
            runtime_export = _validate_exported_runtime(export_dir, provenance, commit)
            shutil.copytree(export_dir, checkout)
            input_destination = checkout / "data" / "input" / run_id
            _copy_tree_with_links(staging_path, input_destination)
            config = generate_campaign_config(
                policy,
                campaign_id=run_id,
                repository_slug=repository,
                item_count=int(manifest_summary["item_count"]),
                source_manifest_sha256=str(manifest_summary["sha256"]),
            )
            (checkout / ".cnb.yml").write_text(config, encoding="utf-8")
            _write_gitattributes(checkout, run_id, str(campaign["transport"]))
            local_metadata = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": run_id,
                "repository": repository,
                "github_commit": commit,
                "allow_unpublished": allow_unpublished,
                "runtime_image": str(policy["verified_runtime_image_digest"]),
                "manifest": manifest_summary,
                "provenance": provenance,
                "runtime_export": runtime_export,
                "transport": campaign["transport"],
            }
            (checkout / "campaign-input.json").write_text(
                json.dumps(local_metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        receipt["runtime_export"] = runtime_export
        receipt["status"] = "planned" if not execute else "preparing"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["failure"] = {"message": str(exc), "phase": "export_or_stage"}
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
        raise
    try:
        if execute:
            target_present = _repo_exists(repository, runner)
            if receipt["repository_created"] and not target_present:
                raise CampaignRepositoryError(
                    f"receipt says repository was created, but CNB no longer exposes it: {repository}"
                )
            if not receipt["repository_created"]:
                if target_present:
                    raise CampaignRepositoryError(
                        "target campaign repository exists without receipt-bound creation evidence; refusing to claim it"
                    )
                create_response = runner(
                    [
                        "cnb", "repositories", "create-repo", "--slug", str(policy["organization_slug"]),
                        "--name", name, "--visibility", str(campaign["visibility"]), "--verbose",
                    ]
                )
                if _is_not_found(create_response):
                    raise CampaignRepositoryError(f"CNB repository creation returned 404: {repository}")
                if _repo_exists(repository, runner) is False:
                    raise CampaignRepositoryError(f"CNB repository was not visible after creation: {repository}")
                receipt["repository_created"] = True
                receipt["updated_at"] = now_iso()
                _atomic_write_json(receipt_file, receipt)
            if not receipt["repository_pushed"]:
                env, askpass = _git_push_environment()
                try:
                    _run_authenticated_git_init(checkout, repository, env, transport=str(campaign["transport"]))
                finally:
                    if askpass is not None:
                        askpass.unlink(missing_ok=True)
                receipt["repository_pushed"] = True
                receipt["status"] = "created_and_pushed"
                receipt["campaign_commit"] = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
            else:
                receipt["status"] = "created_and_pushed"
        else:
            receipt["repository_created"] = bool(receipt.get("repository_created", False))
            receipt["repository_pushed"] = bool(receipt.get("repository_pushed", False))
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
        receipt["receipt"] = str(receipt_file)
        return receipt
    except Exception as exc:
        # A create call can succeed while the subsequent push fails.  Query the
        # exact slug once and preserve that fact so the next invocation resumes
        # instead of attempting a second repository creation.
        try:
            if _repo_exists(repository, runner):
                receipt["repository_created"] = True
        except Exception:
            pass
        receipt["repository_pushed"] = bool(receipt.get("repository_pushed", False)) and not bool(receipt.get("failure"))
        receipt["status"] = "failed"
        receipt["failure"] = {"message": str(exc), "phase": "create_or_push"}
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
        raise


def _run_authenticated_git_init(checkout: Path, repository: str, env: Mapping[str, str], *, transport: str) -> None:
    if not (checkout / ".git").is_dir():
        _run(["git", "init", "-q"], cwd=checkout)
    _run(["git", "config", "user.name", "Music KB Campaign"], cwd=checkout)
    _run(["git", "config", "user.email", "music-kb-campaign@wuyoumusic.invalid"], cwd=checkout)
    _run(["git", "config", "commit.gpgSign", "false"], cwd=checkout)
    if transport == "lfs":
        _run(["git", "lfs", "install", "--local", "--force"], cwd=checkout)
    expected_remote = f"https://cnb.cool/{repository}.git"
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=str(checkout), text=True,
        capture_output=True, check=False,
    )
    if remote.returncode == 0:
        if remote.stdout.strip() != expected_remote:
            raise CampaignRepositoryError("receipt-bound checkout origin does not match campaign repository")
    else:
        _run(["git", "remote", "add", "origin", expected_remote], cwd=checkout)
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=str(checkout), text=True,
        capture_output=True, check=False,
    )
    if head.returncode != 0:
        _run(["git", "add", "-A"], cwd=checkout)
        _run(["git", "add", "-f", "data", "campaign-input.json"], cwd=checkout)
        _run(["git", "commit", "-qm", f"campaign input {repository.rsplit('/', 1)[-1]}"], cwd=checkout)
    _run_git_authenticated(["git", "push", "-u", "origin", "HEAD:main"], cwd=checkout, env=env)


def _run_git_authenticated(command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None:
    completed = subprocess.run(list(command), cwd=str(cwd), env=dict(env), text=True, capture_output=True, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CampaignRepositoryError(f"git push failed ({completed.returncode}): {detail}")


def _extract_build_sn(response: Mapping[str, Any]) -> str:
    def visit(value: Any) -> str | None:
        if isinstance(value, Mapping):
            for key in ("sn", "buildSn", "build_sn"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
            for child in value.values():
                found = visit(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found:
                    return found
        return None

    found = visit(response)
    if not found:
        raise CampaignRepositoryError(f"CNB start-build response has no build SN: {response}")
    return found


def _build_status(repository: str, sn: str, runner: JsonRunner) -> str:
    response = runner(["cnb", "build", "get-build-status", "--repo", repository, "--sn", sn, "--verbose"])
    data = _response_data(response) or {}
    status = str(data.get("status") or response.get("status") or "").lower()
    if status in {"200", "201"}:
        status = ""
    pipelines = data.get("pipelinesStatus", {}) if isinstance(data, Mapping) else {}
    pipeline_states = [str(item.get("status", "")).lower() for item in pipelines.values()] if isinstance(pipelines, Mapping) else []
    if status in TERMINAL_BUILD_STATES:
        return status
    if pipeline_states and all(item in TERMINAL_BUILD_STATES for item in pipeline_states):
        return "success" if all(item == "success" for item in pipeline_states) else next(item for item in pipeline_states if item != "success")
    return status or "running"


def _authenticated_clone(repository: str, branch: str, destination: Path) -> None:
    env, askpass = _git_push_environment()
    try:
        _run_git_authenticated(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", branch, f"https://cnb.cool/{repository}.git", str(destination)],
            cwd=destination.parent,
            env=env,
        )
    finally:
        if askpass is not None:
            askpass.unlink(missing_ok=True)


def _ledger_clone_is_bound(ledger_dir: Path, repository: str) -> bool:
    expected = f"https://cnb.cool/{repository}.git"
    remote = subprocess.run(
        ["git", "-C", str(ledger_dir), "config", "--get", "remote.origin.url"],
        text=True,
        capture_output=True,
        check=False,
    )
    return remote.returncode == 0 and remote.stdout.strip() == expected


def _recover_delivery(receipt: dict[str, Any], *, run_dir: Path, require_source_url: bool = False) -> dict[str, Any]:
    checkout = Path(str(receipt["checkout"])).resolve()
    campaign_id = str(receipt["run_id"])
    ledger_branch = str(receipt["ledger_branch"])
    ledger_dir = run_dir / "cnb" / "ledger-recovery"
    if ledger_dir.exists():
        # A previous builder attempt may have failed after cloning.  Reuse the
        # receipt-bound clone when it still contains the expected ledger; never
        # reject a same-run retry merely because that durable recovery folder
        # remains on disk.
        if (
            not (ledger_dir / ".git").is_dir()
            or not (ledger_dir / "campaign_ledger.jsonl").is_file()
            or not _ledger_clone_is_bound(ledger_dir, str(receipt["repository"]))
        ):
            shutil.rmtree(ledger_dir)
    if not ledger_dir.exists():
        ledger_dir.parent.mkdir(parents=True, exist_ok=True)
        _authenticated_clone(str(receipt["repository"]), ledger_branch, ledger_dir)
    ledger = ledger_dir / "campaign_ledger.jsonl"
    if not ledger.is_file():
        raise CampaignRepositoryError(f"durable ledger branch has no campaign_ledger.jsonl: {ledger_branch}")
    builder = checkout / "scripts" / "build_kugou_canonical_delivery.py"
    if not builder.is_file():
        raise CampaignRepositoryError(f"canonical delivery builder missing from pinned runner: {builder}")
    output = run_dir / "cnb" / "canonical_delivery.jsonl"
    state = run_dir / "cnb" / "canonical_delivery_state.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(builder),
        "--source-manifest",
        str(checkout / "data" / "input" / campaign_id / "manifest.jsonl"),
        "--campaign-ledger",
        str(ledger),
        "--output-manifest",
        str(output),
        "--output-state",
        str(state),
        "--expected-count",
        str(receipt["manifest"]["item_count"]),
        "--expected-campaign-id",
        campaign_id,
    ]
    if require_source_url:
        command.append("--require-source-url")
    _run(command, cwd=builder.parent, timeout=3600)
    rows = [line for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != int(receipt["manifest"]["item_count"]):
        raise CampaignRepositoryError("recovered canonical delivery count does not match campaign manifest")
    return {
        "path": str(output.resolve()),
        "state": str(state.resolve()),
        "sha256": sha256_file(output),
        "count": len(rows),
        "ledger_branch": ledger_branch,
        "ledger": str(ledger.resolve()),
    }


def submit_campaign(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    receipt_path: str | Path,
    run_dir: str | Path,
    execute: bool = False,
    wait: bool = True,
    timeout_seconds: float = 86_400,
    poll_seconds: float = 10.0,
    runner: JsonRunner = run_cnb,
    require_source_url: bool = False,
) -> dict[str, Any]:
    """Trigger all shards for an existing receipt and recover delivery."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_submit")
    operations_sha256 = sha256_file(operations)
    policy = load_campaign_policy(policy_path)
    receipt_file = Path(receipt_path).expanduser().resolve()
    receipt = _read_json(receipt_file)
    binding = validate_campaign_receipt_binding(policy, receipt, operations_sha256=operations_sha256)
    if not binding["valid"]:
        raise CampaignRepositoryError("campaign receipt binding is invalid: " + "; ".join(binding["errors"]))
    _, repository = _full_repository_slug(policy, str(receipt.get("run_id", "")))
    if receipt.get("repository_created") is not True and execute:
        raise CampaignRepositoryError("cannot submit a repository that was not created and pushed")
    if execute and receipt.get("repository_pushed") is not True:
        raise CampaignRepositoryError("cannot submit a campaign repository whose initial push is incomplete")
    receipt["ledger_branch"] = str(policy["campaign_repository"]["ledger_branch_template"]).format(campaign_id=receipt["run_id"])
    receipt["policy_path"] = str(Path(policy_path).expanduser().resolve())
    receipt["operations_sha256"] = operations_sha256
    if not execute:
        receipt["status"] = "submit_planned"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
        receipt["receipt"] = str(receipt_file)
        return receipt
    if not wait:
        raise CampaignRepositoryError("executable campaign submission must wait for every shard")
    if not _repo_exists(repository, runner):
        raise CampaignRepositoryError(f"campaign repository is no longer present; keep the same receipt for recovery: {repository}")
    campaign = policy["campaign_repository"]
    shard_count = int(campaign["shard_count"])
    existing_builds = receipt.get("builds")
    if not isinstance(existing_builds, list):
        existing_builds = []
    builds_by_index: dict[int, dict[str, Any]] = {}
    retry_history: dict[int, list[dict[str, Any]]] = {}
    for raw_build in existing_builds:
        if not isinstance(raw_build, dict):
            raise CampaignRepositoryError("campaign receipt contains a malformed build record")
        try:
            index = int(raw_build.get("index"))
        except (TypeError, ValueError) as exc:
            raise CampaignRepositoryError("campaign receipt build index is invalid") from exc
        if not 1 <= index <= shard_count or index in builds_by_index:
            raise CampaignRepositoryError("campaign receipt contains a duplicate/out-of-range shard index")
        status = str(raw_build.get("status", "")).lower()
        if not str(raw_build.get("sn", "")).strip():
            raise CampaignRepositoryError(f"campaign receipt shard {index} has no build SN")
        if status in {"error", "failed", "cancel", "cancelled", "skipped"}:
            retry_history.setdefault(index, []).append(dict(raw_build))
            continue
        builds_by_index[index] = dict(raw_build)
    builds: list[dict[str, Any]] = [builds_by_index[index] for index in sorted(builds_by_index)]
    started = time.monotonic()
    try:
        for index in range(1, shard_count + 1):
            if index in builds_by_index:
                continue
            shard_id = f"{receipt['run_id']}-s{index}"
            env = {
                "MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX": str(index),
                "MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT": str(shard_count),
                "MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID": shard_id,
                "MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT": str(receipt["manifest"]["item_count"]),
            }
            body = json.dumps(
                {
                    "branch": str(campaign["branch"]),
                    "event": str(campaign["event"]),
                    "env": env,
                    "sync": "false",
                    "title": f"Music Flamingo disposable campaign {receipt['run_id']} shard {index}/{shard_count}",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            response = runner(["cnb", "build", "start-build", "--repo", repository, "--data", body, "--verbose"])
            sn = _extract_build_sn(response)
            attempts = len(retry_history.get(index, [])) + 1
            builds.append(
                {
                    "index": index,
                    "id": shard_id,
                    "sn": sn,
                    "status": "submitted",
                    "env": env,
                    "attempt": attempts,
                    "previous_failures": retry_history.get(index, []),
                }
            )
            builds_by_index[index] = builds[-1]
            receipt["builds"] = builds
            receipt["updated_at"] = now_iso()
            _atomic_write_json(receipt_file, receipt)
        for build in builds:
            while True:
                if time.monotonic() - started > timeout_seconds:
                    raise CampaignRepositoryError(f"campaign build timed out: shard {build['index']} ({build['sn']})")
                status = _build_status(repository, str(build["sn"]), runner)
                build["status"] = status
                receipt["builds"] = builds
                receipt["updated_at"] = now_iso()
                _atomic_write_json(receipt_file, receipt)
                if status in TERMINAL_BUILD_STATES:
                    if status != "success":
                        raise CampaignRepositoryError(f"campaign shard {build['index']} reached terminal status {status}")
                    break
                time.sleep(max(0.1, poll_seconds))
        delivery = _recover_delivery(receipt, run_dir=Path(run_dir).expanduser().resolve(), require_source_url=require_source_url)
        receipt["delivery"] = delivery
        receipt["status"] = "completed"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
        receipt["receipt"] = str(receipt_file)
        return receipt
    except Exception as exc:
        receipt["builds"] = builds
        receipt["status"] = "failed"
        receipt["failure"] = {"message": str(exc), "phase": "submit_or_recover"}
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
        raise


def cleanup_receipt_validation_errors(
    policy: Mapping[str, Any], receipt: Mapping[str, Any], *, operations_sha256: str | None = None
) -> list[str]:
    """Return every missing/inconsistent proof that would make deletion unsafe."""

    errors = campaign_receipt_binding_errors(policy, receipt, operations_sha256=operations_sha256)
    if receipt.get("status") != "completed":
        errors.append("campaign receipt status is not completed")
    if receipt.get("repository_created") is not True:
        errors.append("repository_created proof is missing")
    if receipt.get("repository_pushed") is not True:
        errors.append("repository_pushed proof is missing")
    commit = str(receipt.get("github_commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        errors.append("github_commit provenance is missing or invalid")
    manifest = receipt.get("manifest")
    if isinstance(manifest, Mapping):
        item_count = manifest.get("item_count")
        source_links = manifest.get("source_links")
        manifest_sha = str(manifest.get("sha256", ""))
        if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count <= 0:
            errors.append("manifest item_count is missing or invalid")
        if isinstance(source_links, bool) or not isinstance(source_links, int) or source_links != item_count:
            errors.append("manifest source_links does not equal item_count")
        if not SHA256.fullmatch(manifest_sha):
            errors.append("manifest sha256 is missing or invalid")
        if str(manifest.get("campaign_id", "")) != str(receipt.get("run_id", "")):
            errors.append("manifest campaign_id does not match run_id")
        manifest_path = Path(str(manifest.get("path", ""))).expanduser()
        if not manifest_path.is_file():
            errors.append("source manifest file is missing for provenance verification")
        elif SHA256.fullmatch(manifest_sha) and sha256_file(manifest_path) != manifest_sha:
            errors.append("source manifest sha256 does not match the receipt")
        elif manifest_path.is_file():
            try:
                physical_rows = [line for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                if len(physical_rows) != item_count:
                    errors.append("source manifest physical row count does not match item_count")
            except (OSError, UnicodeDecodeError):
                errors.append("source manifest cannot be decoded for provenance verification")
    runtime_export = receipt.get("runtime_export")
    if not isinstance(runtime_export, Mapping) or runtime_export.get("validated") is not True:
        errors.append("validated runtime export provenance is missing")
    else:
        required = runtime_export.get("required_files")
        paths = {str(item.get("path")) for item in required if isinstance(item, Mapping)} if isinstance(required, list) else set()
        if paths != set(REQUIRED_CAMPAIGN_RUNTIME_FILES):
            errors.append("runtime export required-file provenance is incomplete")
        if isinstance(required, list):
            for item in required:
                if not isinstance(item, Mapping):
                    continue
                item_path = str(item.get("path", ""))
                if not SHA256.fullmatch(str(item.get("sha256", ""))) or not isinstance(item.get("bytes"), int) or int(item.get("bytes", 0)) <= 0:
                    errors.append(f"runtime export hash/byte provenance is incomplete for {item_path}")
    builds = receipt.get("builds")
    expected_shards = int(policy["campaign_repository"]["shard_count"])
    if not isinstance(builds, list):
        errors.append("builds list is missing")
    else:
        indexes: list[int] = []
        for raw in builds:
            if not isinstance(raw, Mapping):
                errors.append("builds contains a malformed shard record")
                continue
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                errors.append("build shard index is invalid")
                continue
            indexes.append(index)
            if str(raw.get("status", "")).lower() != "success":
                errors.append(f"shard {index} is not successful")
            if not str(raw.get("sn", "")).strip() or not str(raw.get("id", "")).strip():
                errors.append(f"shard {index} lacks durable id/SN")
        if sorted(indexes) != list(range(1, expected_shards + 1)):
            errors.append("build shard index set is incomplete or duplicated")
    delivery = receipt.get("delivery")
    if not isinstance(delivery, Mapping):
        errors.append("canonical delivery proof is missing")
    else:
        manifest_count = int(manifest.get("item_count", 0)) if isinstance(manifest, Mapping) else 0
        if delivery.get("count") != manifest_count:
            errors.append("delivery count does not match manifest item_count")
        delivery_sha = str(delivery.get("sha256", ""))
        delivery_path = Path(str(delivery.get("path", ""))).expanduser()
        if not SHA256.fullmatch(delivery_sha):
            errors.append("delivery sha256 is missing or invalid")
        elif not delivery_path.is_file():
            errors.append("delivery file is missing for hash verification")
        elif sha256_file(delivery_path) != delivery_sha:
            errors.append("delivery sha256 does not match the canonical delivery file")
        if not str(delivery.get("ledger_branch", "")).strip():
            errors.append("delivery ledger_branch proof is missing")
    for key in ("campaign_repository_config", "workspace", "checkout"):
        if not str(receipt.get(key, "")).strip():
            errors.append(f"receipt {key} provenance is missing")
    return sorted(set(errors))


def cleanup_campaign_repository(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    receipt_path: str | Path,
    confirm: bool,
    release_verified: bool,
    peer_gate: bool,
    runner: JsonRunner = run_cnb,
) -> dict[str, Any]:
    """Delete and verify one receipt-bound disposable repository."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_cleanup")
    operations_sha256 = sha256_file(operations)
    policy = load_campaign_policy(policy_path)
    receipt_file = Path(receipt_path).expanduser().resolve()
    receipt = _read_json(receipt_file)
    try:
        _, repository = _full_repository_slug(policy, str(receipt.get("run_id", "")))
    except CampaignRepositoryError:
        repository = str(receipt.get("repository", ""))
    result: dict[str, Any] = {
        "action": "cnb_campaign_cleanup",
        "repository": repository,
        "run_id": receipt.get("run_id"),
        "confirmed": confirm,
        "release_verified": release_verified,
        "peer_gate": peer_gate,
        "deleted": False,
        "failures": [],
    }

    def record_result() -> dict[str, Any]:
        # A dry-run or safety-gate stop is still an auditable atom outcome.  Keep
        # it on the same receipt so a later resume cannot mistake an unrecorded
        # return for a clean campaign.
        receipt["cleanup"] = copy.deepcopy(result)
        receipt["updated_at"] = now_iso()
        _atomic_write_json(receipt_file, receipt)
        result["receipt"] = str(receipt_file)
        return result

    if not confirm:
        present = None
        if repository:
            try:
                present = _repo_exists(repository, runner)
            except Exception as exc:
                result["failures"].append({"kind": "presence", "error": str(exc)})
        result.update({"status": "dry_run", "present": present, "clean": False})
        return record_result()
    for error in cleanup_receipt_validation_errors(policy, receipt, operations_sha256=operations_sha256):
        result["failures"].append({"kind": "receipt", "error": error})
    if not release_verified:
        result["failures"].append({"kind": "release-gate", "error": "verified local release is required"})
    if not peer_gate:
        result["failures"].append({"kind": "peer-gate", "error": "peer gate or explicit peer skip is required"})
    if result["failures"]:
        result["status"] = "blocked"
        result["clean"] = False
        return record_result()
    protected_before = _protected_runtime_status(policy, runner)
    result["protected_runtime_before"] = protected_before
    if not protected_before["protected"]:
        result["failures"].append({"kind": "protected-runtime", "error": "protected runtime/main/digest is unhealthy"})
    if result["failures"]:
        result["status"] = "blocked"
        result["clean"] = False
        return record_result()
    workspaces_response, absent = _cnb_optional(
        ["cnb", "workspace", "list-workspaces", "--slug", repository, "--status", "running", "--page", "1", "--page-size", "100", "--verbose"],
        runner,
    )
    workspace_rows = ((_response_data(workspaces_response) or {}).get("list", []) if not absent and workspaces_response else [])
    if workspace_rows:
        result["failures"].append({"kind": "workspace", "error": "running workspace exists"})
        result["status"] = "blocked"
        result["clean"] = False
        return record_result()
    organization = str(policy["organization_slug"])
    before_group = _group_volume(organization, runner)["object"]
    before_repo = _repo_volume(organization, repository, runner)
    result.update({"group_object_used_bytes_before": before_group, "repository_object_bytes_before": before_repo})
    if _repo_exists(repository, runner):
        try:
            runner(["cnb", "repositories", "delete-repo", "--repo", repository, "--verbose"])
            result["deleted"] = True
        except Exception as exc:
            result["failures"].append({"kind": "delete", "error": str(exc)})
    present_after = _repo_exists(repository, runner)
    after_repo = _repo_volume(organization, repository, runner)
    after_group = _group_volume(organization, runner)["object"]
    protected_after = _protected_runtime_status(policy, runner)
    result.update({
        "repository_present_after": present_after,
        "repository_object_bytes_after": after_repo,
        "group_object_used_bytes_after": after_group,
        "group_object_usage_decreased": after_group < before_group,
        "protected_runtime_after": protected_after,
    })
    if present_after or after_repo != 0:
        result["failures"].append({"kind": "repository-verification", "error": "repository is not 404/zero-volume"})
    if before_repo > 0 and after_group >= before_group:
        result["failures"].append({"kind": "organization-charge-verification", "error": "organization object usage did not decrease"})
    if not protected_after["protected"]:
        result["failures"].append({"kind": "protected-runtime-verification", "error": "protected runtime/main/digest missing after deletion"})
    result["status"] = "succeeded" if not result["failures"] else "failed"
    result["clean"] = result["status"] == "succeeded"
    return record_result()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "prepare", "submit", "cleanup"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--operations-file", type=Path, default=Path(__file__).resolve().parents[1] / "references" / "validated-operations.json")
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--github-commit")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--transport", choices=("lfs", "git-objects"))
    parser.add_argument("--resume-repository", help="Exact receipt-bound campaign repository allowed during resume")
    parser.add_argument("--execute", action="store_true", help="Allow CNB repository/build/delete side effects")
    parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="Local dry-run only: export a commit not yet reachable from origin/main",
    )
    parser.add_argument("--wait", action="store_true", default=False)
    parser.add_argument("--timeout-seconds", type=float, default=86_400)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--require-source-url", action="store_true")
    parser.add_argument("--confirm-delete-cnb-repositories", action="store_true")
    parser.add_argument("--release-verified", action="store_true")
    parser.add_argument("--peer-gate", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "preflight":
            operations = args.operations_file.expanduser().resolve()
            load_validated_operations(operations, required_atom="cnb_campaign_repository")
            policy = _policy_with_transport(load_campaign_policy(args.policy), args.transport)
            result = campaign_preflight(
                policy,
                runner=run_cnb,
                resume_repository=args.resume_repository,
            )
        elif args.action == "prepare":
            if not args.run_id or not args.staging or not args.run_dir or not args.github_commit:
                raise CampaignRepositoryError("prepare requires --run-id, --staging, --run-dir, and --github-commit")
            if args.allow_unpublished and args.execute:
                raise CampaignRepositoryError("--allow-unpublished cannot be combined with --execute")
            result = prepare_campaign_repository(
                policy_path=args.policy,
                operations_path=args.operations_file,
                repository_root=args.repository_root,
                run_id=args.run_id,
                staging=args.staging,
                run_dir=args.run_dir,
                github_commit=args.github_commit,
                expected_count=args.expected_count,
                execute=args.execute,
                receipt_path=args.receipt,
                work_dir=args.work_dir,
                transport=args.transport,
                allow_unpublished=args.allow_unpublished,
            )
        elif args.action == "submit":
            if not args.receipt or not args.run_dir:
                raise CampaignRepositoryError("submit requires --receipt and --run-dir")
            result = submit_campaign(
                policy_path=args.policy,
                operations_path=args.operations_file,
                receipt_path=args.receipt,
                run_dir=args.run_dir,
                execute=args.execute,
                wait=args.wait,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                require_source_url=args.require_source_url,
            )
        else:
            if not args.receipt:
                raise CampaignRepositoryError("cleanup requires --receipt")
            result = cleanup_campaign_repository(
                policy_path=args.policy,
                operations_path=args.operations_file,
                receipt_path=args.receipt,
                confirm=args.confirm_delete_cnb_repositories,
                release_verified=args.release_verified,
                peer_gate=args.peer_gate,
            )
        print(json.dumps(result, ensure_ascii=False))
        if args.action == "preflight":
            return 0 if result.get("clean") else 3
        if args.action == "cleanup" and args.confirm_delete_cnb_repositories:
            return 0 if result.get("clean") else 4
        return 0
    except (CampaignRepositoryError, OSError, ValueError) as exc:
        print(json.dumps({"action": args.action, "status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
