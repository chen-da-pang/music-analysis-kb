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
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

try:
    from music_kb.campaign_delivery import load_campaign_delivery_file
    from music_kb.operation_context import load_validated_operations, now_iso, sha256_file
    from music_kb.snapshot import verify_snapshot
except ModuleNotFoundError:  # Allow the documented direct ``python script.py`` invocation.
    _PLUGIN_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_PLUGIN_ROOT / "src"))
    from music_kb.campaign_delivery import load_campaign_delivery_file
    from music_kb.operation_context import load_validated_operations, now_iso, sha256_file
    from music_kb.snapshot import verify_snapshot


RECEIPT_SCHEMA_VERSION = 1
EXTERNAL_DELIVERY_RECONCILIATION_SCHEMA_VERSION = 1
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
    "scripts/check_manual_gpu_gate.py",
    "scripts/devgpu_run_batch.sh",
    "scripts/devgpu_batch_supervisor.py",
)

# The recovery overlay is deliberately narrow.  A recovery normally reuses the
# pinned campaign runner, but an operational gate repair must be able to reach
# a fresh GitHub-main export without changing the model or analysis code.  Any
# broader runner change remains an immutable-campaign boundary and is rejected.
REQUIRED_DEVGPU_RECOVERY_RUNTIME_FILES = (
    "scripts/check_manual_gpu_gate.py",
    "scripts/campaign_ledger_git.sh",
    "scripts/music_flamingo_run_context.py",
    "scripts/prepare_kugou_campaign_shard.sh",
    "scripts/run_music_flamingo_batch.py",
    "scripts/devgpu_run_batch.sh",
    "scripts/devgpu_batch_supervisor.py",
    "scripts/build_kugou_canonical_delivery.py",
)
DEVGPU_RECOVERY_REFRESHABLE_RUNTIME_FILES = (
    "scripts/check_manual_gpu_gate.py",
)
DEVGPU_RECOVERY_REFRESH_METADATA = "devgpu-runner-refresh.json"
# The generated DevGPU stage can run a full 408-track campaign.  Keep the
# client-side poll deadline aligned with that stage instead of allowing an
# orphaned workspace to outlive the receipt-bound execution window.
DEVGPU_WORKSPACE_MAX_SECONDS = 8 * 60 * 60
# CNB documents that a workspace not entered in VS Code is recycled after ten
# minutes.  Keep model hydration behind a longer stage-local dwell: if that
# policy applies to an unattended launch, the campaign stops before inference.
DEVGPU_WORKSPACE_LIFECYCLE_GUARD_SECONDS = 11 * 60

JsonRunner = Callable[[Sequence[str]], dict[str, Any]]


class CampaignRepositoryError(RuntimeError):
    """Raised when a campaign repository safety or lifecycle check fails."""


def resolve_devgpu_recovery_profile(policy: Mapping[str, Any], profile_name: str = "L40") -> dict[str, Any]:
    """Return one explicit, fail-closed Dev GPU recovery profile."""

    profiles = policy.get("devgpu_recovery_profiles")
    if not isinstance(profiles, Mapping):
        raise CampaignRepositoryError("CNB policy must define devgpu_recovery_profiles")
    name = str(profile_name).strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,31}", name):
        raise CampaignRepositoryError(f"unsafe Dev GPU recovery profile: {profile_name!r}")
    raw = profiles.get(name)
    if not isinstance(raw, Mapping):
        available = ", ".join(sorted(str(key) for key in profiles))
        raise CampaignRepositoryError(
            f"unknown Dev GPU recovery profile {name!r}; configured profiles: {available or '(none)'}"
        )
    runner_tag = str(raw.get("runner_tag", "")).strip()
    expected_gpu = str(raw.get("expected_gpu", "")).strip()
    execution_profile = str(raw.get("execution_profile", "")).strip()
    required_history_review_action = raw.get("required_history_review_action")
    minimum_free_mib = raw.get("minimum_free_mib")
    if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,31}", expected_gpu):
        raise CampaignRepositoryError(f"Dev GPU profile {name} has an unsafe expected_gpu")
    if runner_tag != f"cnb:arch:amd64:gpu:{expected_gpu}":
        raise CampaignRepositoryError(f"Dev GPU profile {name} runner_tag must select expected_gpu")
    if execution_profile != f"nvidia-{expected_gpu.lower()}/full_precision/bfloat16":
        raise CampaignRepositoryError(f"Dev GPU profile {name} has an invalid execution_profile")
    if isinstance(minimum_free_mib, bool) or not isinstance(minimum_free_mib, int) or minimum_free_mib <= 0:
        raise CampaignRepositoryError(f"Dev GPU profile {name} minimum_free_mib must be a positive integer")
    if required_history_review_action is not None:
        if not isinstance(required_history_review_action, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{2,127}", required_history_review_action
        ):
            raise CampaignRepositoryError(
                f"Dev GPU profile {name} required_history_review_action must be a safe action name"
            )
    resolved = {
        "name": name,
        "runner_tag": runner_tag,
        "expected_gpu": expected_gpu,
        "minimum_free_mib": minimum_free_mib,
        "execution_profile": execution_profile,
    }
    if required_history_review_action is not None:
        resolved["required_history_review_action"] = required_history_review_action
    return resolved


def resolve_devgpu_capacity_preflight(policy: Mapping[str, Any]) -> dict[str, int]:
    """Return the versioned, fail-closed quota estimate for a Dev GPU resume."""

    raw = policy.get("devgpu_capacity_preflight")
    if not isinstance(raw, Mapping):
        raise CampaignRepositoryError("CNB policy must define devgpu_capacity_preflight")
    resolved: dict[str, int] = {}
    for field in ("per_pending_item_estimate_seconds", "fixed_headroom_seconds"):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CampaignRepositoryError(
                f"devgpu_capacity_preflight.{field} must be a positive integer"
            )
        resolved[field] = value
    return resolved


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


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=dict(env) if env is not None else None,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CampaignRepositoryError(
            f"command failed ({completed.returncode}): {' '.join(command)}{': ' + detail if detail else ''}"
        )
    return completed


def _run_json(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    completed = _run(command, cwd=cwd, timeout=timeout, env=env)
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
    """Run a CNB CLI command and reject a non-success OpenAPI response.

    CNB's CLI can exit zero after printing an OpenAPI error object.  Process
    success is therefore not authorization or mutation success; callers may
    only act on a 2xx response.  Optional 404 probes remain compatible because
    ``_cnb_optional`` recognizes the raised status and converts it to absence.
    """

    cli_env = os.environ.copy()
    cli_env.pop("CNB_TOKEN", None)
    cli_env.pop("MUSIC_KB_CNB_GIT_TOKEN", None)
    response = _run_json(command, env=cli_env)
    raw_status = response.get("status")
    try:
        status = int(raw_status)
    except (TypeError, ValueError) as exc:
        raise CampaignRepositoryError(
            f"CNB response has no valid API status: {' '.join(command)}"
        ) from exc
    if 200 <= status < 300:
        return response
    data = response.get("data")
    if isinstance(data, Mapping):
        detail = str(data.get("errmsg") or data.get("message") or data.get("errcode") or "")
    else:
        detail = str(data or "")
    suffix = f": {detail}" if detail else ""
    raise CampaignRepositoryError(f"CNB API request failed with status {status}{suffix}")


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
        "devgpu_recovery_profiles",
        "devgpu_capacity_preflight",
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
    profiles = policy.get("devgpu_recovery_profiles")
    if not isinstance(profiles, Mapping) or not profiles:
        raise CampaignRepositoryError("CNB policy must define at least one Dev GPU recovery profile")
    for profile_name in profiles:
        if not isinstance(profile_name, str):
            raise CampaignRepositoryError("Dev GPU recovery profile names must be strings")
        resolve_devgpu_recovery_profile(policy, profile_name)
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


def _read_manifest_contract(
    manifest_path: str | Path, *, expected_count: int | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read immutable manifest evidence after its staging audio was purged.

    A completed campaign may deliberately remove local/CNB staging audio after
    delivery and release.  Reconciliation must still bind the preserved JSONL
    to the original receipt, but it must not pretend that missing disposable
    audio can be re-hashed.  This parser validates the manifest's own strict
    contract and leaves actual-file validation to the earlier materialization
    and delivery evidence.
    """

    manifest = Path(manifest_path).expanduser().resolve()
    if not manifest.is_file():
        raise CampaignRepositoryError(f"campaign source manifest is missing: {manifest}")
    try:
        raw = manifest.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CampaignRepositoryError(f"campaign source manifest is unreadable: {manifest}: {exc}") from exc
    if not raw or b"\r" in raw or not raw.endswith(b"\n"):
        raise CampaignRepositoryError("campaign source manifest must be non-empty UTF-8 JSONL with LF line endings")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    campaign_id: str | None = None
    physical_lines = text.split("\n")
    if physical_lines[-1] != "":
        raise CampaignRepositoryError("campaign source manifest must end with one LF")
    for line_number, line in enumerate(physical_lines[:-1], 1):
        if not line:
            raise CampaignRepositoryError(f"campaign source manifest has an empty line at {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CampaignRepositoryError(
                f"campaign source manifest JSON error at line {line_number}: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise CampaignRepositoryError(f"campaign source manifest row {line_number} is not an object")
        item_id = str(row.get("id", "")).strip()
        if not item_id or item_id in seen or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item_id):
            raise CampaignRepositoryError(
                f"campaign source manifest row {line_number} has missing/duplicate/unsafe id"
            )
        seen.add(item_id)
        row_campaign_id = str(row.get("campaign_id", "")).strip()
        if not row_campaign_id:
            raise CampaignRepositoryError(f"campaign source manifest row {line_number} has no campaign_id")
        if campaign_id is None:
            campaign_id = row_campaign_id
        elif row_campaign_id != campaign_id:
            raise CampaignRepositoryError("campaign source manifest mixes campaign_id values")
        relative = str(row.get("relative_audio_path", "")).strip()
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != "audio":
            raise CampaignRepositoryError(
                f"campaign source manifest row {line_number} has unsafe relative_audio_path"
            )
        source_bytes = row.get("source_bytes")
        if isinstance(source_bytes, bool) or not isinstance(source_bytes, int) or source_bytes <= 0:
            raise CampaignRepositoryError(
                f"campaign source manifest row {line_number} has invalid source_bytes"
            )
        source_sha256 = str(row.get("sha256", "")).strip().lower()
        if not SHA256.fullmatch(source_sha256):
            raise CampaignRepositoryError(
                f"campaign source manifest row {line_number} has invalid sha256"
            )
        if not str(row.get("title", "")).strip() or not str(row.get("artist", "")).strip():
            raise CampaignRepositoryError(
                f"campaign source manifest row {line_number} has no title or artist"
            )
        source_url = str(row.get("source_url", "")).strip()
        parsed = urlsplit(source_url)
        if not source_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CampaignRepositoryError(
                f"campaign source manifest row {line_number} has missing or unsafe source_url"
            )
        rows.append(dict(row))
    if expected_count is not None and len(rows) != expected_count:
        raise CampaignRepositoryError(
            f"campaign source manifest count {len(rows)} != expected {expected_count}"
        )
    if not rows or campaign_id is None:
        raise CampaignRepositoryError("campaign source manifest is empty")
    return (
        {
            "path": str(manifest),
            "sha256": sha256_file(manifest),
            "item_count": len(rows),
            "source_bytes": sum(int(row["source_bytes"]) for row in rows),
            "source_links": len(rows),
            "campaign_id": campaign_id,
        },
        rows,
    )


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
    if expected_repository and receipt.get("repository_name") != expected_repository.split("/", 1)[-1]:
        errors.append("receipt repository_name does not match the strict repository slug")
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


def _required_nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignRepositoryError(f"official CNB {field} is missing, invalid, or negative")
    return value


def devgpu_capacity_preflight(
    policy: Mapping[str, Any], *, pending_item_count: int, runner: JsonRunner = run_cnb
) -> dict[str, Any]:
    """Calculate usable official Dev GPU quota for one receipt-bound recovery.

    CNB publishes the total in ``get-quota`` and the used/frozen values in
    ``get-volume``.  A successful workspace-start response is never treated as
    capacity evidence; the exact arithmetic and policy estimate are retained
    before that request is issued.
    """

    result: dict[str, Any] = {
        "action": "devgpu-capacity-preflight",
        "clean": False,
        "pending_item_count": pending_item_count,
        "per_pending_item_estimate_seconds": None,
        "fixed_headroom_seconds": None,
        "quota_total_dev_gpu_seconds": None,
        "volume_used_dev_gpu_seconds": None,
        "volume_frozen_dev_gpu_seconds": None,
        "freeze_dev_gpu_seconds_defaulted_to_zero": False,
        "available_dev_gpu_seconds": None,
        "campaign_estimate_seconds": None,
        "required_dev_gpu_seconds": None,
        "errors": [],
    }
    try:
        if isinstance(pending_item_count, bool) or not isinstance(pending_item_count, int) or pending_item_count <= 0:
            raise CampaignRepositoryError("receipt-bound pending item count must be a positive integer")
        estimate_policy = resolve_devgpu_capacity_preflight(policy)
        result.update(estimate_policy)
        organization = str(policy["organization_slug"])
        quota_data = _response_data(
            runner(["cnb", "charge", "get-quota", "--slug", organization, "--verbose"])
        )
        volume_data = _response_data(
            runner(["cnb", "charge", "get-volume", "--slug", organization, "--verbose"])
        )
        if not isinstance(quota_data, Mapping) or not isinstance(volume_data, Mapping):
            raise CampaignRepositoryError("official CNB quota or volume response is not an object")
        dev_gpu_quota = quota_data.get("dev_gpu_in_sec")
        if not isinstance(dev_gpu_quota, Mapping):
            raise CampaignRepositoryError("official CNB dev_gpu_in_sec quota field is missing or invalid")
        total = _required_nonnegative_integer(dev_gpu_quota.get("total"), "dev_gpu_in_sec.total")
        used = _required_nonnegative_integer(volume_data.get("dev_gpu_in_sec"), "dev_gpu_in_sec")
        if "freeze_dev_gpu_in_sec" not in volume_data:
            # CNB's live volume response omits this field when no Dev GPU quota
            # is frozen.  Preserve that official zero convention explicitly in
            # the receipt; a present malformed or negative value still fails.
            frozen = 0
            result["freeze_dev_gpu_seconds_defaulted_to_zero"] = True
        else:
            frozen = _required_nonnegative_integer(
                volume_data.get("freeze_dev_gpu_in_sec"), "freeze_dev_gpu_in_sec"
            )
        available = total - used - frozen
        campaign_estimate = pending_item_count * estimate_policy["per_pending_item_estimate_seconds"]
        required = campaign_estimate + estimate_policy["fixed_headroom_seconds"]
        result.update(
            {
                "quota_total_dev_gpu_seconds": total,
                "volume_used_dev_gpu_seconds": used,
                "volume_frozen_dev_gpu_seconds": frozen,
                "available_dev_gpu_seconds": available,
                "campaign_estimate_seconds": campaign_estimate,
                "required_dev_gpu_seconds": required,
            }
        )
        if available < 0:
            raise CampaignRepositoryError("official CNB available_dev_gpu_seconds is negative")
        if available < required:
            raise CampaignRepositoryError(
                "official CNB usable Dev GPU capacity cannot cover the receipt-bound estimate plus headroom"
            )
        result["clean"] = True
    except Exception as exc:
        result["errors"].append(str(exc))
    return result


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


def _running_workspaces(
    repository: str,
    runner: JsonRunner,
    *,
    branch: str | None = None,
) -> list[dict[str, Any]]:
    """Return visible running workspaces for one disposable repository.

    A 404 is an audited absence here.  Any other API error still reaches the
    caller as a hard preflight failure through ``run_cnb``.
    """

    command = [
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
    ]
    if branch:
        command.extend(["--branch", branch])
    response, absent = _cnb_optional(command, runner)
    if absent or response is None:
        return []
    payload = _response_data(response) or {}
    rows = payload.get("list", []) if isinstance(payload, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _workspace_preflight(repository: str, runner: JsonRunner) -> dict[str, Any]:
    """Record and reject any workspace visible before a new DevGPU launch."""

    visible = _running_workspaces(repository, runner)
    return {
        "status": "clean" if not visible else "blocked",
        "running_workspaces": visible,
    }


def _start_receipt_bound_workspace(
    *, repository: str, branch: str, runner: JsonRunner
) -> tuple[str, bool]:
    """Start a workspace, resolving CNB's URL-only existing-workspace response safely."""

    response = runner(
        [
            "cnb", "workspace", "start-workspace", "--repo", repository,
            "--branch", branch, "--verbose",
        ]
    )
    try:
        return _extract_build_sn(response), False
    except CampaignRepositoryError as start_error:
        # CNB documents that start-workspace returns only a WebIDE URL when the
        # environment already exists.  That URL is not execution evidence: only
        # a single running workspace on the exact receipt-bound branch is safe.
        candidates = _running_workspaces(repository, runner, branch=branch)
        candidate_sns = sorted(
            {
                str(item["sn"])
                for item in candidates
                if isinstance(item.get("sn"), str) and str(item["sn"]).strip()
            }
        )
        if len(candidate_sns) != 1:
            raise CampaignRepositoryError(
                "workspace start returned no SN and did not resolve exactly one receipt-bound running workspace"
            ) from start_error
        return candidate_sns[0], True


def retained_campaign_receipt_validation_errors(
    policy: Mapping[str, Any], receipt: Mapping[str, Any], *, repository: str
) -> list[str]:
    """Validate the deliberately narrow fresh-run retained-repository exception."""

    errors: list[str] = []
    expected_prefix = f"{policy['organization_slug']}/{policy['campaign_repository_prefix']}"
    if not repository.startswith(expected_prefix):
        errors.append("retained repository does not match the strict campaign prefix")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        errors.append("retained receipt schema_version does not match")
    if receipt.get("atom") != "cnb_campaign_repository":
        errors.append("retained receipt atom is not cnb_campaign_repository")
    if receipt.get("status") not in {"failed", "interrupted"}:
        errors.append("retained receipt status is not failed or interrupted")
    receipt_run_id = str(receipt.get("run_id", "")).strip()
    try:
        _, expected_repository = _full_repository_slug(policy, receipt_run_id)
    except CampaignRepositoryError:
        expected_repository = ""
        errors.append("retained receipt run_id is not a safe campaign identity")
    if receipt.get("repository") != repository:
        errors.append("retained receipt repository does not match the explicit retained repository")
    if expected_repository and repository != expected_repository:
        errors.append("retained receipt repository does not match its strict run-id/prefix mapping")
    if receipt.get("repository_name") != repository.split("/", 1)[-1]:
        errors.append("retained receipt repository_name does not match the explicit retained repository")
    if receipt.get("repository_prefix") != policy.get("campaign_repository_prefix"):
        errors.append("retained receipt repository_prefix does not match policy")
    if receipt.get("organization") != policy.get("organization_slug"):
        errors.append("retained receipt organization does not match policy")
    if receipt.get("repository_created") is not True:
        errors.append("retained receipt does not prove repository creation")
    if receipt.get("repository_pushed") is not True:
        errors.append("retained receipt does not prove repository push")
    if receipt.get("delivery") is not None:
        errors.append("retained receipt already has a canonical delivery")
    if not str(receipt.get("workspace", "")).strip():
        errors.append("retained receipt lacks receipt-bound workspace provenance")
    return sorted(set(errors))


def campaign_preflight(
    policy: Mapping[str, Any],
    *,
    runner: JsonRunner = run_cnb,
    estimated_bytes: int = 0,
    target_repository: str | None = None,
    resume_repository: str | None = None,
    retained_campaign_receipt: Mapping[str, Any] | None = None,
    retained_campaign_receipt_path: str | None = None,
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
    if retained_campaign_receipt is not None and resume_repository is not None:
        raise CampaignRepositoryError("retained campaign receipt override is only valid for a fresh campaign")
    retained_repository: str | None = None
    retained_receipt_errors: list[str] = []
    retained_running_workspaces: list[dict[str, Any]] = []
    if retained_campaign_receipt is not None:
        retained_repository = str(retained_campaign_receipt.get("repository", "")).strip()
        retained_receipt_errors = retained_campaign_receipt_validation_errors(
            policy, retained_campaign_receipt, repository=retained_repository
        )
        if not retained_receipt_errors:
            retained_running_workspaces = _running_workspaces(retained_repository, runner)
    allowed_campaign_repository = resume_repository or retained_repository
    other_campaign_repositories = [
        value for value in campaign_repositories if value != allowed_campaign_repository
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
    if retained_campaign_receipt is not None:
        checks.update(
            {
                "retained_campaign_receipt_bound": not retained_receipt_errors,
                "retained_campaign_repository_present": retained_repository in campaign_repositories,
                "retained_campaign_workspace_stopped": not retained_running_workspaces,
            }
        )
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
        "retained_campaign_repository": retained_repository,
        "retained_campaign_receipt_path": retained_campaign_receipt_path,
        "retained_campaign_receipt_sha256": (
            sha256_file(Path(retained_campaign_receipt_path))
            if retained_campaign_receipt_path is not None
            else None
        ),
        "retained_campaign_receipt_errors": retained_receipt_errors,
        "retained_campaign_running_workspaces": retained_running_workspaces,
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


def generate_campaign_devgpu_config(
    policy: Mapping[str, Any],
    *,
    campaign_id: str,
    repository_slug: str,
    item_count: int,
    source_manifest_sha256: str,
    devgpu_profile: str = "L40",
) -> str:
    """Add one receipt-bound full-resume workspace to the generated config."""

    base = generate_campaign_config(
        policy,
        campaign_id=campaign_id,
        repository_slug=repository_slug,
        item_count=item_count,
        source_manifest_sha256=source_manifest_sha256,
    ).rstrip()
    campaign = policy["campaign_repository"]
    profile = resolve_devgpu_recovery_profile(policy, devgpu_profile)
    image = str(policy["verified_runtime_image_digest"])
    shard_count = int(campaign["shard_count"])
    input_root = f"data/input/{campaign_id}"
    manifest_path = f"{input_root}/manifest.jsonl"
    ledger_branch = str(campaign["ledger_branch_template"]).format(campaign_id=campaign_id)
    lines = [
        base,
        "  vscode:",
        "  - git:",
        "      lfs: false",
        "    docker:",
        f"      image: {image}",
        "      volumes:",
        "      - music-flamingo-output:/workspace/cache/output:read-write",
        "    services:",
        "    - vscode",
        "    runner:",
        f"      tags: {profile['runner_tag']}",
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
        f"      MUSIC_FLAMINGO_CAMPAIGN_ID: {_yaml_string(campaign_id)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_SOURCE_MANIFEST: {_yaml_string(manifest_path)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT: {_yaml_string(input_root)}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_TRANSPORT: {_yaml_string(campaign['transport'])}",
        f"      MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT: '{item_count}'",
        f"      MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT: '{shard_count}'",
        "      MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS: '0'",
        "      MUSIC_FLAMINGO_CONTINUE_ON_ERROR: '1'",
        f"      MUSIC_FLAMINGO_EXECUTION_PROFILE: {profile['execution_profile']}",
        f"      MUSIC_FLAMINGO_DEVGPU_RECOVERY_PROFILE: {profile['name']}",
        "      MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED: '1'",
        "      MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY: '5'",
        f"      MUSIC_FLAMINGO_LEDGER_REPO_URL: {_yaml_string('https://cnb.cool/' + repository_slug + '.git')}",
        f"      MUSIC_FLAMINGO_LEDGER_BRANCH: {_yaml_string(ledger_branch)}",
        "      MUSIC_FLAMINGO_LEDGER_GIT_USER_NAME: CNB Music Campaign Ledger",
        "      MUSIC_FLAMINGO_LEDGER_GIT_USER_EMAIL: cnb-ledger@wuyoumusic.invalid",
        f"      MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256: {_yaml_string(source_manifest_sha256)}",
        "      MUSIC_FLAMINGO_DETAILED_CUDA_TELEMETRY: '0'",
        "    stages:",
        "    - name: Run receipt-bound Dev GPU full resume",
        "      timeout: 8h",
        "      script: |",
        "        set -eu",
        "        gate_root=\"/workspace/cache/output/music_flamingo_pipeline/devgpu-recovery-${CNB_BUILD_ID}\"",
        "        mkdir -p \"$gate_root\"",
        "        # CNB recycles a workspace that has not entered VS Code after ten minutes.",
        "        # Do not hydrate the model until this unattended-workspace guard has passed.",
        f"        sleep {DEVGPU_WORKSPACE_LIFECYCLE_GUARD_SECONDS}",
        f"        printf '%s\\n' \"[music-flamingo-workspace-lifecycle-guard] passed guard_seconds={DEVGPU_WORKSPACE_LIFECYCLE_GUARD_SECONDS}\"",
        f"        python scripts/check_manual_gpu_gate.py --phase before_hydrate --expected-gpu {profile['expected_gpu']} --minimum-free-mib {profile['minimum_free_mib']} --max-utilization-percent 0 --receipt \"$gate_root/gpu-before-hydrate.json\"",
        "        sleep 60",
        f"        python scripts/check_manual_gpu_gate.py --phase stable_before_hydrate --expected-gpu {profile['expected_gpu']} --minimum-free-mib {profile['minimum_free_mib']} --max-utilization-percent 0 --receipt \"$gate_root/gpu-stable-before-hydrate.json\"",
        "        wait_for_clean_gpu() {",
        "          release_phase=\"$1\"",
        "          release_attempt=1",
        "          while [ \"$release_attempt\" -le 20 ]; do",
        f"            if python scripts/check_manual_gpu_gate.py --phase \"${{release_phase}}_attempt_${{release_attempt}}\" --expected-gpu {profile['expected_gpu']} --minimum-free-mib {profile['minimum_free_mib']} --max-utilization-percent 0 --receipt \"$gate_root/gpu-${{release_phase}}-${{release_attempt}}.json\"; then",
        "              return 0",
        "            fi",
        "            if [ \"$release_attempt\" -eq 20 ]; then",
        "              printf '%s\\n' \"GPU did not return to the required clean state after ${release_phase}\" >&2",
        "              return 2",
        "            fi",
        "            sleep 15",
        "            release_attempt=$((release_attempt + 1))",
        "          done",
        "        }",
        f"        for shard_index in $(seq 1 {shard_count}); do",
        "          export MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX=\"$shard_index\"",
        "          export MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID=\"${MUSIC_FLAMINGO_CAMPAIGN_ID}-s${shard_index}\"",
        "          export MUSIC_FLAMINGO_RUN_ID=\"${CNB_BUILD_ID}-s${shard_index}\"",
        "          export MUSIC_FLAMINGO_OUTPUT_NAME=\"campaign_${MUSIC_FLAMINGO_CAMPAIGN_ID}_devgpu_s${shard_index}\"",
        "          run_dir=\"$(python scripts/music_flamingo_run_context.py print-dir)\"",
        "          mkdir -p \"$run_dir\"",
        "          bash scripts/campaign_ledger_git.sh restore \"$run_dir/campaign_ledger.jsonl\"",
        "          bash scripts/prepare_kugou_campaign_shard.sh",
        "          pending_count=\"$(python - \"$run_dir/campaign_shard_plan.json\" <<'PYTHON'",
        "        import json",
        "        import sys",
        "        print(json.load(open(sys.argv[1], encoding=\"utf-8\"))[\"pending_item_count\"])",
        "        PYTHON",
        "          )\"",
        "          export MUSIC_FLAMINGO_INPUT_MANIFEST=\"$run_dir/campaign_shard_manifest.jsonl\"",
        "          export MUSIC_FLAMINGO_INPUT_AUDIO_ROOT=\"$PWD/${MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT}\"",
        "          if [ \"$pending_count\" -eq 0 ]; then",
        "            python - \"$run_dir\" <<'PYTHON'",
        "        import json",
        "        import sys",
        "        from pathlib import Path",
        "        run_dir = Path(sys.argv[1])",
        "        report = {",
        "            \"schema_version\": 1,",
        "            \"status\": \"success\",",
        "            \"campaign_status\": \"already_complete_for_selected_shard\",",
        "            \"reason\": \"static_shard_has_no_pending_items\",",
        "            \"items\": [],",
        "            \"campaign\": {",
        "                \"campaign_status\": \"already_complete_for_selected_shard\",",
        "                \"selected_item_count\": 0,",
        "                \"new_success_count\": 0,",
        "                \"reused_success_count\": 0,",
        "                \"error_item_count\": 0,",
        "                \"deferred_item_count\": 0,",
        "            },",
        "        }",
        "        payload = json.dumps(report, ensure_ascii=False, sort_keys=True) + \"\\n\"",
        "        (run_dir / \"batch_report.json\").write_text(payload, encoding=\"utf-8\")",
        "        (run_dir / \"campaign_report.json\").write_text(payload, encoding=\"utf-8\")",
        "        PYTHON",
        "            printf '%s\\n' 0 > \"$run_dir/campaign_runner_exit_code.txt\"",
        "          else",
        f"            python scripts/check_manual_gpu_gate.py --phase \"pre_model_s${{shard_index}}\" --expected-gpu {profile['expected_gpu']} --minimum-free-mib {profile['minimum_free_mib']} --max-utilization-percent 0 --receipt \"$gate_root/gpu-pre-model-s${{shard_index}}.json\"",
        "            export MUSIC_FLAMINGO_EXPECTED_ITEM_COUNT=\"$pending_count\"",
        "            set +e",
        "            bash scripts/devgpu_run_batch.sh",
        "            runner_rc=$?",
        "            set -e",
        "            printf '%s\\n' \"$runner_rc\" > \"$run_dir/campaign_runner_exit_code.txt\"",
        "            test \"$runner_rc\" -eq 0",
        "          fi",
        "          test -s \"$run_dir/batch_report.json\"",
        "          if [ ! -s \"$run_dir/campaign_report.json\" ]; then cp \"$run_dir/batch_report.json\" \"$run_dir/campaign_report.json\"; fi",
        "          test -s \"$run_dir/campaign_ledger.jsonl\"",
        "          python - \"$run_dir\" \"$pending_count\" <<'PYTHON'",
        "        import json",
        "        import sys",
        "        from pathlib import Path",
        "        run_dir = Path(sys.argv[1])",
        "        expected_selected = int(sys.argv[2])",
        "        report = json.loads((run_dir / \"campaign_report.json\").read_text(encoding=\"utf-8\"))",
        "        if report.get(\"status\") != \"success\":",
        "            raise SystemExit(f\"campaign batch status is not success: {report.get('status')!r}\")",
        "        campaign = report.get(\"campaign\")",
        "        if not isinstance(campaign, dict):",
        "            raise SystemExit(\"campaign report has no campaign accounting\")",
        "        campaign_status = campaign.get(\"campaign_status\", report.get(\"campaign_status\"))",
        "        if campaign_status not in {\"success\", \"already_complete_for_selected_shard\"}:",
        "            raise SystemExit(f\"campaign status is not complete: {campaign_status!r}\")",
        "        selected = int(campaign.get(\"selected_item_count\", -1))",
        "        new_success = int(campaign.get(\"new_success_count\", sum(item.get(\"status\") == \"success\" for item in report.get(\"items\", []))))",
        "        reused_success = int(campaign.get(\"reused_success_count\", 0))",
        "        errors = int(campaign.get(\"error_item_count\", 0))",
        "        deferred = int(campaign.get(\"deferred_item_count\", 0))",
        "        if selected != expected_selected or new_success + reused_success != selected or errors or deferred:",
        "            raise SystemExit(\"campaign report does not prove full zero-error shard coverage\")",
        "        PYTHON",
        "          wait_for_clean_gpu \"post_shard_s${shard_index}_release\"",
        "        done",
        "        ledger_verify_dir=\"$gate_root/durable-ledger\"",
        "        mkdir -p \"$ledger_verify_dir\"",
        "        bash scripts/campaign_ledger_git.sh restore \"$ledger_verify_dir/campaign_ledger.jsonl\"",
        "        python scripts/build_kugou_canonical_delivery.py --source-manifest \"$MUSIC_FLAMINGO_CAMPAIGN_SOURCE_MANIFEST\" --campaign-ledger \"$ledger_verify_dir/campaign_ledger.jsonl\" --output-manifest \"$ledger_verify_dir/canonical_delivery.jsonl\" --output-state \"$ledger_verify_dir/canonical_delivery_state.json\" --expected-count \"$MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT\" --expected-campaign-id \"$MUSIC_FLAMINGO_CAMPAIGN_ID\" --require-source-url",
        "        test -s \"$ledger_verify_dir/canonical_delivery.jsonl\"",
        "        test -s \"$ledger_verify_dir/canonical_delivery_state.json\"",
        "    lock:",
        f"      key: {_yaml_string('music-flamingo-' + campaign_id + '-ledger-writer')}",
        "      wait: true",
        "      timeout: 15000",
        "      expires: 32400",
        "",
    ]
    return "\n".join(lines)


def _campaign_config_with_shard(config_path: Path, env: Mapping[str, str]) -> str:
    config = config_path.read_text(encoding="utf-8")
    for key, value in env.items():
        pattern = re.compile(rf"^(\s+{re.escape(key)}:\s*).+$", re.MULTILINE)
        config, count = pattern.subn(
            lambda match: match.group(1) + _yaml_string(value), config, count=1
        )
        if count != 1:
            raise CampaignRepositoryError(
                f"generated campaign config has {count} fields for required override {key}"
            )
    return config


def _write_gitattributes(checkout: Path, campaign_id: str, transport: str) -> None:
    path = checkout / ".gitattributes"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"data/input/{campaign_id}/audio/** filter=lfs diff=lfs merge=lfs -text"
    if transport == "lfs" and line not in existing.splitlines():
        path.write_text(existing.rstrip("\n") + ("\n" if existing else "") + line + "\n", encoding="utf-8")


def _git_push_environment() -> tuple[dict[str, str], Path | None]:
    token = os.environ.get("MUSIC_KB_CNB_GIT_TOKEN", "") or os.environ.get("CNB_TOKEN", "")
    env = os.environ.copy()
    if token:
        encoded = base64.b64encode(f"cnb:{token}".encode("utf-8")).decode("ascii")
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
            }
        )
        return env, None

    # A publisher authenticated through CNB's administrator OAuth already has
    # an official Git credential helper.  Keep it inside the Git subprocess:
    # an empty first helper removes any stale inherited helper, then the CNB
    # helper supplies the scoped credential on demand.  No token is extracted,
    # stored, or passed to the workspace/OpenAPI subprocesses.
    if shutil.which("cnb") is None:
        raise CampaignRepositoryError(
            "MUSIC_KB_CNB_GIT_TOKEN is unset and the cnb git-credential helper is unavailable"
        )
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "!cnb git-credential",
        }
    )
    return env, None


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


def _prepare_devgpu_runner_refresh(
    *,
    repository_root: Path,
    github_commit: str,
    recovery_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Export the tiny GitHub-owned operational refresh allowed during recovery."""

    export_dir = recovery_dir / "runtime-export"
    if export_dir.exists():
        shutil.rmtree(export_dir)
    provenance = _export_runtime(repository_root, github_commit, export_dir)
    _validate_exported_runtime(export_dir, provenance, github_commit)
    files: list[dict[str, Any]] = []
    for relative in DEVGPU_RECOVERY_REFRESHABLE_RUNTIME_FILES:
        path = export_dir / Path(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            raise CampaignRepositoryError(f"Dev GPU runner refresh is missing regular file: {relative}")
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return export_dir, {
        "schema_version": 1,
        "github_repository": "chen-da-pang/music-analysis-kb",
        "github_commit": github_commit,
        "files": files,
    }


def _refresh_metadata_text(runner_refresh: Mapping[str, Any]) -> str:
    return json.dumps(runner_refresh, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _overlay_has_runner_refresh(
    overlay: Path,
    *,
    runtime_export_dir: Path,
    runner_refresh: Mapping[str, Any],
) -> bool:
    metadata = overlay / DEVGPU_RECOVERY_REFRESH_METADATA
    if not metadata.is_file() or metadata.read_text(encoding="utf-8") != _refresh_metadata_text(runner_refresh):
        return False
    raw_files = runner_refresh.get("files")
    if not isinstance(raw_files, list):
        return False
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            return False
        relative = str(raw.get("path", ""))
        if relative not in DEVGPU_RECOVERY_REFRESHABLE_RUNTIME_FILES:
            return False
        relative_path = Path(*PurePosixPath(relative).parts)
        expected = runtime_export_dir / relative_path
        actual = overlay / relative_path
        if not expected.is_file() or not actual.is_file() or actual.is_symlink():
            return False
        if sha256_file(actual) != sha256_file(expected) or actual.stat().st_size != expected.stat().st_size:
            return False
    return True


def _apply_devgpu_runner_refresh(
    overlay: Path,
    *,
    runtime_export_dir: Path,
    runner_refresh: Mapping[str, Any],
) -> None:
    raw_files = runner_refresh.get("files")
    if not isinstance(raw_files, list):
        raise CampaignRepositoryError("Dev GPU runner refresh has no file inventory")
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            raise CampaignRepositoryError("Dev GPU runner refresh has a malformed file record")
        relative = str(raw.get("path", ""))
        if relative not in DEVGPU_RECOVERY_REFRESHABLE_RUNTIME_FILES:
            raise CampaignRepositoryError(f"Dev GPU runner refresh has forbidden path: {relative!r}")
        relative_path = Path(*PurePosixPath(relative).parts)
        source = runtime_export_dir / relative_path
        destination = overlay / relative_path
        if not source.is_file() or source.is_symlink():
            raise CampaignRepositoryError(f"Dev GPU runner refresh source is invalid: {relative}")
        expected_sha = str(raw.get("sha256", "")).lower()
        expected_bytes = raw.get("bytes")
        if (
            not SHA256.fullmatch(expected_sha)
            or isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 1
            or sha256_file(source) != expected_sha
            or source.stat().st_size != expected_bytes
        ):
            raise CampaignRepositoryError(f"Dev GPU runner refresh provenance is invalid: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (overlay / DEVGPU_RECOVERY_REFRESH_METADATA).write_text(
        _refresh_metadata_text(runner_refresh),
        encoding="utf-8",
    )


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
    retained_campaign_receipt: Mapping[str, Any] | None = None,
    retained_campaign_receipt_path: str | None = None,
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
        retained_campaign_receipt=retained_campaign_receipt,
        retained_campaign_receipt_path=retained_campaign_receipt_path,
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
    # This helper only clones durable-ledger branches.  A campaign repository
    # can still contain LFS audio pointers on that ref, but ledger planning and
    # canonical-delivery recovery need only ordinary Git files.  Never let a
    # local receipt check hydrate campaign audio as a side effect.
    env = dict(env)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    try:
        _run_git_authenticated(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", branch, f"https://cnb.cool/{repository}.git", str(destination)],
            cwd=destination.parent,
            env=env,
        )
    finally:
        if askpass is not None:
            askpass.unlink(missing_ok=True)


def _campaign_config_env_value(config_path: Path, key: str) -> str:
    """Read one JSON/YAML scalar from the receipt-bound campaign config."""

    try:
        config = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CampaignRepositoryError(f"receipt-bound campaign config is unreadable: {config_path}: {exc}") from exc
    matches = re.findall(rf"^\s+{re.escape(key)}:\s*(.+?)\s*$", config, flags=re.MULTILINE)
    if len(matches) != 1:
        raise CampaignRepositoryError(
            f"receipt-bound campaign config has {len(matches)} values for required env {key}"
        )
    raw = matches[0].strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CampaignRepositoryError(
                f"receipt-bound campaign config has invalid JSON scalar for {key}"
            ) from exc
    elif len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        value = raw[1:-1]
    else:
        value = raw
    if not isinstance(value, str) or not value:
        raise CampaignRepositoryError(f"receipt-bound campaign config has an invalid {key} value")
    return value


def _receipt_bound_devgpu_preflight_inputs(
    source_receipt: Mapping[str, Any],
    *,
    execution_profile: str,
) -> dict[str, Any]:
    """Resolve the original runner and input required for a local pending plan."""

    checkout = Path(str(source_receipt.get("checkout", ""))).expanduser().resolve()
    campaign_id = str(source_receipt.get("run_id", ""))
    campaign_commit = str(source_receipt.get("campaign_commit", ""))
    if not checkout.is_dir():
        raise CampaignRepositoryError(f"receipt-bound campaign checkout is missing: {checkout}")
    if not re.fullmatch(r"[0-9a-f]{40}", campaign_commit):
        raise CampaignRepositoryError("source receipt has no valid campaign_commit for local pending preflight")
    actual_head = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
    if actual_head != campaign_commit:
        raise CampaignRepositoryError("receipt-bound checkout HEAD no longer matches campaign_commit for local pending preflight")

    manifest = source_receipt.get("manifest")
    if not isinstance(manifest, Mapping):
        raise CampaignRepositoryError("source receipt has no manifest for local pending preflight")
    expected_count = manifest.get("item_count")
    expected_sha256 = str(manifest.get("sha256", "")).lower()
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
        raise CampaignRepositoryError("source receipt manifest has an invalid item_count for local pending preflight")
    if not SHA256.fullmatch(expected_sha256):
        raise CampaignRepositoryError("source receipt manifest has an invalid sha256 for local pending preflight")
    if not SAFE_RUN_ID.fullmatch(campaign_id):
        raise CampaignRepositoryError("source receipt has an unsafe run_id for local pending preflight")

    source_manifest = checkout / "data" / "input" / campaign_id / "manifest.jsonl"
    input_root = source_manifest.parent
    if not source_manifest.is_file() or not input_root.is_dir():
        raise CampaignRepositoryError("receipt-bound campaign input is missing for local pending preflight")
    if sha256_file(source_manifest) != expected_sha256:
        raise CampaignRepositoryError("receipt-bound campaign manifest no longer matches the source receipt")

    planner = checkout / "scripts" / "prepare_kugou_campaign_shard.py"
    required_scripts = (
        planner,
        checkout / "scripts" / "music_flamingo_campaign.py",
        checkout / "scripts" / "run_music_flamingo_batch.py",
        checkout / "scripts" / "run_one_music_flamingo_smoke.py",
    )
    missing = [str(path) for path in required_scripts if not path.is_file()]
    if missing:
        raise CampaignRepositoryError(
            "receipt-bound runner is incomplete for local pending preflight: " + ", ".join(missing)
        )

    config_path = checkout / ".cnb.yml"
    runtime_image = _campaign_config_env_value(config_path, "CNB_RUNTIME_IMAGE")
    if runtime_image != str(source_receipt.get("runtime_image", "")):
        raise CampaignRepositoryError("receipt-bound campaign config runtime image does not match the source receipt")
    return {
        "checkout": checkout,
        "campaign_id": campaign_id,
        "campaign_commit": campaign_commit,
        "source_manifest": source_manifest,
        "input_root": input_root,
        "expected_count": expected_count,
        "source_manifest_sha256": expected_sha256,
        "planner": planner,
        "config_path": config_path,
        "runtime_image": runtime_image,
        "prompt": _campaign_config_env_value(config_path, "MUSIC_FLAMINGO_PROMPT"),
        "max_new_tokens": _campaign_config_env_value(config_path, "MUSIC_FLAMINGO_MAX_NEW_TOKENS"),
        "audio_clip_seconds": _campaign_config_env_value(config_path, "MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS"),
        "model_id": _campaign_config_env_value(config_path, "MUSIC_FLAMINGO_MODEL"),
        "model_revision": _campaign_config_env_value(config_path, "MUSIC_FLAMINGO_REVISION"),
        "model_dir": _campaign_config_env_value(config_path, "MUSIC_FLAMINGO_MODEL_DIR"),
        "execution_profile": execution_profile,
    }


def _plan_devgpu_pending_preflight(
    source_receipt: Mapping[str, Any],
    *,
    recovery_dir: Path,
    ledger_branch: str,
    execution_profile: str,
    shard_count: int,
) -> dict[str, Any]:
    """Plan every static shard locally before allocating a Dev GPU workspace.

    The planner is the receipt-bound campaign copy of
    ``prepare_kugou_campaign_shard.py``.  It reads only the ordinary-Git
    manifest and durable ledger, so this guard neither hydrates audio nor
    starts a CNB workspace.
    """

    inputs = _receipt_bound_devgpu_preflight_inputs(
        source_receipt,
        execution_profile=execution_profile,
    )
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or shard_count < 1:
        raise CampaignRepositoryError("local pending preflight has an invalid configured static shard count")
    if shard_count > int(inputs["expected_count"]):
        raise CampaignRepositoryError("local pending preflight shard count exceeds the source manifest count")
    preflight_root = recovery_dir / "pending-preflight"
    if preflight_root.exists():
        shutil.rmtree(preflight_root)
    preflight_root.mkdir(parents=True, exist_ok=True)
    ledger_dir = preflight_root / "ledger"
    _authenticated_clone(str(source_receipt["repository"]), ledger_branch, ledger_dir)
    if not _ledger_clone_is_bound(ledger_dir, str(source_receipt["repository"])):
        raise CampaignRepositoryError("local pending preflight ledger clone is not bound to the campaign repository")
    ledger = ledger_dir / "campaign_ledger.jsonl"
    if not ledger.is_file():
        raise CampaignRepositoryError(f"durable ledger branch has no campaign_ledger.jsonl: {ledger_branch}")

    plans: list[dict[str, Any]] = []
    expected_global_count: int | None = None
    expected_global_ids: list[str] | None = None
    expected_contract: str | None = None
    for shard_index in range(1, shard_count + 1):
        shard_dir = preflight_root / f"shard-{shard_index:02d}"
        command = [
            sys.executable,
            str(inputs["planner"]),
            "--source-manifest",
            str(inputs["source_manifest"]),
            "--input-root",
            str(inputs["input_root"]),
            "--repo-root",
            str(inputs["checkout"]),
            "--ledger",
            str(ledger),
            "--run-dir",
            str(shard_dir),
            "--expected-count",
            str(inputs["expected_count"]),
            "--campaign-id",
            str(inputs["campaign_id"]),
            "--shard-index",
            str(shard_index),
            "--shard-count",
            str(shard_count),
            "--runtime-image",
            str(inputs["runtime_image"]),
            "--prompt",
            str(inputs["prompt"]),
            "--max-new-tokens",
            str(inputs["max_new_tokens"]),
            "--audio-clip-seconds",
            str(inputs["audio_clip_seconds"]),
            "--model-id",
            str(inputs["model_id"]),
            "--model-revision",
            str(inputs["model_revision"]),
            "--model-dir",
            str(inputs["model_dir"]),
            "--execution-profile",
            str(inputs["execution_profile"]),
        ]
        completed = _run(command, cwd=Path(inputs["checkout"]), timeout=300)
        try:
            plan = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CampaignRepositoryError("receipt-bound local pending planner returned invalid JSON") from exc
        if not isinstance(plan, dict):
            raise CampaignRepositoryError("receipt-bound local pending planner did not return an object")
        if plan.get("campaign_id") != inputs["campaign_id"] or plan.get("shard_index") != shard_index:
            raise CampaignRepositoryError("receipt-bound local pending planner returned a mismatched shard plan")
        if plan.get("shard_count") != shard_count:
            raise CampaignRepositoryError("receipt-bound local pending planner returned a mismatched shard count")
        if plan.get("source_manifest_sha256") != inputs["source_manifest_sha256"]:
            raise CampaignRepositoryError("receipt-bound local pending planner returned a mismatched manifest hash")
        pending_count = plan.get("pending_item_count")
        global_count = plan.get("global_pending_item_count")
        pending_ids = plan.get("pending_item_ids")
        global_ids = plan.get("global_pending_item_ids")
        contract = plan.get("contract")
        if (
            isinstance(pending_count, bool)
            or not isinstance(pending_count, int)
            or pending_count < 0
            or isinstance(global_count, bool)
            or not isinstance(global_count, int)
            or global_count < 0
            or not isinstance(pending_ids, list)
            or not all(isinstance(item, str) and item for item in pending_ids)
            or len(pending_ids) != pending_count
            or not isinstance(global_ids, list)
            or not all(isinstance(item, str) and item for item in global_ids)
            or len(global_ids) != global_count
            or not isinstance(contract, str)
            or not contract
        ):
            raise CampaignRepositoryError("receipt-bound local pending planner returned an invalid plan")
        if expected_global_count is None:
            expected_global_count = global_count
            expected_global_ids = list(global_ids)
            expected_contract = contract
        elif (
            global_count != expected_global_count
            or global_ids != expected_global_ids
            or contract != expected_contract
        ):
            raise CampaignRepositoryError("receipt-bound local pending planner disagrees across static shards")
        plans.append(
            {
                "index": shard_index,
                "id": f"{inputs['campaign_id']}-s{shard_index}",
                "pending_item_count": pending_count,
                "pending_item_ids": list(pending_ids),
                "static_shard_pending_item_count": plan.get("static_shard_pending_item_count"),
                "source_range": copy.deepcopy(plan.get("source_range")),
            }
        )
    assert expected_global_count is not None and expected_global_ids is not None and expected_contract is not None
    if sum(int(plan["pending_item_count"]) for plan in plans) != expected_global_count:
        raise CampaignRepositoryError("receipt-bound local pending planner has inconsistent global shard coverage")
    return {
        "schema_version": 1,
        "status": "completed",
        "checkout": str(Path(inputs["checkout"]).resolve()),
        "campaign_commit": str(inputs["campaign_commit"]),
        "source_config": str(Path(inputs["config_path"]).resolve()),
        "source_config_sha256": sha256_file(Path(inputs["config_path"])),
        "planner": str(Path(inputs["planner"]).resolve()),
        "ledger_branch": ledger_branch,
        "ledger": str(ledger.resolve()),
        "ledger_sha256": sha256_file(ledger),
        "execution_profile": execution_profile,
        "contract": expected_contract,
        "global_pending_item_count": expected_global_count,
        "global_pending_item_ids": expected_global_ids,
        "shards": plans,
    }


def _ledger_clone_is_bound(ledger_dir: Path, repository: str) -> bool:
    expected = f"https://cnb.cool/{repository}.git"
    remote = subprocess.run(
        ["git", "-C", str(ledger_dir), "config", "--get", "remote.origin.url"],
        text=True,
        capture_output=True,
        check=False,
    )
    return remote.returncode == 0 and remote.stdout.strip() == expected


def _recover_delivery(
    receipt: dict[str, Any],
    *,
    run_dir: Path,
    require_source_url: bool = False,
    fresh_ledger: bool = False,
) -> dict[str, Any]:
    checkout = Path(str(receipt["checkout"])).resolve()
    campaign_id = str(receipt["run_id"])
    ledger_branch = str(receipt["ledger_branch"])
    ledger_dir = run_dir / "cnb" / "ledger-recovery"
    if fresh_ledger and ledger_dir.exists():
        # A Dev GPU recovery has just passed its in-workspace canonical
        # verification.  Re-clone the durable branch instead of accepting a
        # ledger clone left by an earlier failed recovery attempt.
        shutil.rmtree(ledger_dir)
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
    transport: str | None = None,
) -> dict[str, Any]:
    """Trigger all shards for an existing receipt and recover delivery."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_submit")
    operations_sha256 = sha256_file(operations)
    policy = _policy_with_transport(load_campaign_policy(policy_path), transport)
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
            title = f"Music Flamingo disposable campaign {receipt['run_id']} shard {index}/{shard_count}"
            config = _campaign_config_with_shard(Path(str(receipt["campaign_repository_config"])), env)
            response = runner(
                [
                    "cnb", "build", "start-build", "--repo", repository,
                    "--branch", str(campaign["branch"]),
                    "--event", str(campaign["event"]),
                    "--sync", "false",
                    "--title", title,
                    "--config", config,
                    "--verbose",
                ]
            )
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


def _authenticated_git_output(command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> str:
    completed = subprocess.run(list(command), cwd=str(cwd), env=dict(env), text=True, capture_output=True, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CampaignRepositoryError(f"authenticated git command failed ({completed.returncode}): {detail}")
    return completed.stdout.strip()


def _prepare_devgpu_overlay(
    *,
    policy: Mapping[str, Any],
    source_receipt: Mapping[str, Any],
    recovery_dir: Path,
    runtime_export_dir: Path,
    runner_refresh: Mapping[str, Any],
    devgpu_profile: str = "L40",
    overlay_kind: str = "recovery",
) -> dict[str, Any]:
    checkout = Path(str(source_receipt["checkout"])).expanduser().resolve()
    campaign_commit = str(source_receipt.get("campaign_commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", campaign_commit):
        raise CampaignRepositoryError("source receipt has no valid campaign_commit")
    if _run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip() != campaign_commit:
        raise CampaignRepositoryError("source checkout HEAD no longer matches campaign_commit")
    for relative in REQUIRED_DEVGPU_RECOVERY_RUNTIME_FILES:
        if not (checkout / relative).is_file():
            raise CampaignRepositoryError(f"Dev GPU recovery runtime file is missing: {relative}")

    env, askpass = _git_push_environment()
    try:
        remote = f"https://cnb.cool/{source_receipt['repository']}.git"
        remote_main = _authenticated_git_output(
            ["git", "ls-remote", remote, "refs/heads/main"], cwd=checkout, env=env
        ).split()
        if not remote_main or remote_main[0] != campaign_commit:
            raise CampaignRepositoryError("remote campaign main no longer matches the receipt campaign_commit")

        if overlay_kind not in {"recovery", "campaign"}:
            raise CampaignRepositoryError("Dev GPU overlay kind is invalid")
        branch = f"codex/devgpu-{overlay_kind}-{source_receipt['run_id']}"
        config = generate_campaign_devgpu_config(
            policy,
            campaign_id=str(source_receipt["run_id"]),
            repository_slug=str(source_receipt["repository"]),
            item_count=int(source_receipt["manifest"]["item_count"]),
            source_manifest_sha256=str(source_receipt["manifest"]["sha256"]),
            devgpu_profile=devgpu_profile,
        )
        expected_config_sha256 = hashlib.sha256(config.encode("utf-8")).hexdigest()
        remote_overlay = _authenticated_git_output(
            ["git", "ls-remote", remote, f"refs/heads/{branch}"], cwd=checkout, env=env
        ).split()
        overlay = recovery_dir / "overlay"
        if overlay.exists():
            shutil.rmtree(overlay)
        _run(["git", "-C", str(checkout), "worktree", "prune"])
        if remote_overlay:
            previous_overlay_commit = remote_overlay[0]
            _authenticated_git_output(
                ["git", "fetch", "origin", f"refs/heads/{branch}"], cwd=checkout, env=env
            )
            _run(
                ["git", "-C", str(checkout), "worktree", "add", "--force", "--detach", str(overlay), previous_overlay_commit]
            )
            _run(["git", "merge-base", "--is-ancestor", campaign_commit, "HEAD"], cwd=overlay)
            changed = {
                line.strip()
                for line in _run(
                    ["git", "diff", "--name-only", f"{campaign_commit}..HEAD"], cwd=overlay
                ).stdout.splitlines()
                if line.strip()
            }
            allowed_changed_paths = {
                ".cnb.yml",
                DEVGPU_RECOVERY_REFRESH_METADATA,
                *DEVGPU_RECOVERY_REFRESHABLE_RUNTIME_FILES,
            }
            if not changed <= allowed_changed_paths:
                raise CampaignRepositoryError(
                    "existing Dev GPU overlay history contains a non-refresh runner change"
                )
            if (
                sha256_file(overlay / ".cnb.yml") == expected_config_sha256
                and _overlay_has_runner_refresh(
                    overlay,
                    runtime_export_dir=runtime_export_dir,
                    runner_refresh=runner_refresh,
                )
            ):
                return {
                    "branch": branch,
                    "commit": previous_overlay_commit,
                    "parent_campaign_commit": campaign_commit,
                    "config_sha256": expected_config_sha256,
                    "runner_refresh": copy.deepcopy(dict(runner_refresh)),
                    "devgpu_profile": resolve_devgpu_recovery_profile(policy, devgpu_profile),
                    "path": str(overlay),
                    "reused": True,
                }
            _run(["git", "checkout", "-B", branch], cwd=overlay)
            _apply_devgpu_runner_refresh(
                overlay,
                runtime_export_dir=runtime_export_dir,
                runner_refresh=runner_refresh,
            )
            (overlay / ".cnb.yml").write_text(config, encoding="utf-8")
            _run(["git", "config", "user.name", "Music KB Dev GPU Recovery"], cwd=overlay)
            _run(["git", "config", "user.email", "music-kb-devgpu@wuyoumusic.invalid"], cwd=overlay)
            _run(["git", "config", "commit.gpgSign", "false"], cwd=overlay)
            _run(
                [
                    "git", "add", ".cnb.yml", DEVGPU_RECOVERY_REFRESH_METADATA,
                    *DEVGPU_RECOVERY_REFRESHABLE_RUNTIME_FILES,
                ],
                cwd=overlay,
            )
            _run(["git", "commit", "-qm", f"Refresh Dev GPU recovery {source_receipt['run_id']}"], cwd=overlay)
            overlay_commit = _run(["git", "rev-parse", "HEAD"], cwd=overlay).stdout.strip()
            _run_git_authenticated(
                ["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=overlay, env=env
            )
            remote_overlay = _authenticated_git_output(
                ["git", "ls-remote", remote, f"refs/heads/{branch}"], cwd=overlay, env=env
            ).split()
            if not remote_overlay or remote_overlay[0] != overlay_commit:
                raise CampaignRepositoryError("repaired Dev GPU overlay branch did not verify after push")
            return {
                "branch": branch,
                "commit": overlay_commit,
                "previous_commit": previous_overlay_commit,
                "parent_campaign_commit": campaign_commit,
                "config_sha256": expected_config_sha256,
                "runner_refresh": copy.deepcopy(dict(runner_refresh)),
                "devgpu_profile": resolve_devgpu_recovery_profile(policy, devgpu_profile),
                "path": str(overlay),
                "reused": False,
                "updated": True,
            }
        _run(["git", "-C", str(checkout), "worktree", "add", "--force", "--detach", str(overlay), campaign_commit])
        _run(["git", "checkout", "-B", branch], cwd=overlay)
        _apply_devgpu_runner_refresh(
            overlay,
            runtime_export_dir=runtime_export_dir,
            runner_refresh=runner_refresh,
        )
        (overlay / ".cnb.yml").write_text(config, encoding="utf-8")
        _run(["git", "config", "user.name", "Music KB Dev GPU Recovery"], cwd=overlay)
        _run(["git", "config", "user.email", "music-kb-devgpu@wuyoumusic.invalid"], cwd=overlay)
        _run(["git", "config", "commit.gpgSign", "false"], cwd=overlay)
        _run(
            [
                "git", "add", ".cnb.yml", DEVGPU_RECOVERY_REFRESH_METADATA,
                *DEVGPU_RECOVERY_REFRESHABLE_RUNTIME_FILES,
            ],
            cwd=overlay,
        )
        _run(["git", "commit", "-qm", f"Dev GPU recovery {source_receipt['run_id']}"], cwd=overlay)
        overlay_commit = _run(["git", "rev-parse", "HEAD"], cwd=overlay).stdout.strip()
        _run_git_authenticated(
            ["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch}"], cwd=overlay, env=env
        )
        remote_overlay = _authenticated_git_output(
            ["git", "ls-remote", remote, f"refs/heads/{branch}"], cwd=overlay, env=env
        ).split()
        if not remote_overlay or remote_overlay[0] != overlay_commit:
            raise CampaignRepositoryError("Dev GPU overlay branch did not verify after push")
        return {
            "branch": branch,
            "commit": overlay_commit,
            "parent_campaign_commit": campaign_commit,
            "config_sha256": expected_config_sha256,
            "runner_refresh": copy.deepcopy(dict(runner_refresh)),
            "devgpu_profile": resolve_devgpu_recovery_profile(policy, devgpu_profile),
            "path": str(overlay),
            "reused": False,
        }
    finally:
        if askpass is not None:
            askpass.unlink(missing_ok=True)


def _workspace_recovery_stage_status(repository: str, sn: str, runner: JsonRunner) -> tuple[str, str | None]:
    response = runner(["cnb", "build", "get-build-status", "--repo", repository, "--sn", sn, "--verbose"])
    data = _response_data(response) or {}
    pipelines = data.get("pipelinesStatus", {}) if isinstance(data, Mapping) else {}
    for pipeline_id, pipeline in (pipelines.items() if isinstance(pipelines, Mapping) else []):
        for stage in (pipeline.get("stages", []) if isinstance(pipeline, Mapping) else []):
            if stage.get("id") == "stage-0" or stage.get("name") == "Run receipt-bound Dev GPU full resume":
                status = str(stage.get("status", "")).lower()
                if status in TERMINAL_BUILD_STATES:
                    return status, str(pipeline_id)
                return "running", str(pipeline_id)
    overall = str(data.get("status", "")).lower() if isinstance(data, Mapping) else ""
    if overall == "success":
        raise CampaignRepositoryError(
            "Dev GPU workspace reported success without the receipt-bound full-resume stage"
        )
    return (overall if overall in TERMINAL_BUILD_STATES else "running"), None


def _verify_build_gpu_platform_gate(source_receipt: Mapping[str, Any], runner: JsonRunner) -> dict[str, Any]:
    """Prove every submitted shard stopped at the CNB build-GPU prepare quota gate."""

    failure = source_receipt.get("failure")
    if not isinstance(failure, Mapping) or failure.get("phase") != "submit_or_recover":
        raise CampaignRepositoryError("Dev GPU recovery requires a submit_or_recover source failure")
    builds = source_receipt.get("builds")
    if not isinstance(builds, list) or not builds:
        raise CampaignRepositoryError("Dev GPU recovery requires receipt-bound failed build records")
    repository = str(source_receipt["repository"])
    evidence: list[dict[str, Any]] = []
    for build in builds:
        if not isinstance(build, Mapping) or not str(build.get("sn", "")).strip():
            raise CampaignRepositoryError("Dev GPU recovery source contains a build without an SN")
        sn = str(build["sn"])
        status_response = runner(
            ["cnb", "build", "get-build-status", "--repo", repository, "--sn", sn, "--verbose"]
        )
        status_data = _response_data(status_response) or {}
        pipelines = status_data.get("pipelinesStatus", {}) if isinstance(status_data, Mapping) else {}
        matched: dict[str, Any] | None = None
        for pipeline_id, pipeline in (pipelines.items() if isinstance(pipelines, Mapping) else []):
            if not isinstance(pipeline, Mapping):
                continue
            stages = pipeline.get("stages", [])
            prepare = next(
                (
                    stage
                    for stage in stages
                    if isinstance(stage, Mapping)
                    and (stage.get("id") == "prepare" or stage.get("name") == "Prepare")
                ),
                None,
            )
            inference = next(
                (
                    stage
                    for stage in stages
                    if isinstance(stage, Mapping)
                    and (
                        stage.get("id") == "stage-0"
                        or stage.get("name") == "Run disposable Music Flamingo campaign shard"
                    )
                ),
                None,
            )
            if not isinstance(prepare, Mapping) or str(prepare.get("status", "")).lower() not in {"error", "failed"}:
                continue
            if not isinstance(inference, Mapping) or str(inference.get("status", "")).lower() != "skipped":
                continue
            stage_id = str(prepare.get("id") or "prepare")
            stage_response = runner(
                [
                    "cnb", "build", "get-build-stage", "--repo", repository, "--sn", sn,
                    "--pipelineId", str(pipeline_id), "--stageId", stage_id, "--verbose",
                ]
            )
            stage_data = _response_data(stage_response) or {}
            serialized = json.dumps(stage_data, ensure_ascii=False).lower()
            if "gpu core-hours" not in serialized or "pre-freezing" not in serialized:
                continue
            detail = str(stage_data.get("error", "")) if isinstance(stage_data, Mapping) else ""
            matched = {
                "sn": sn,
                "pipeline_id": str(pipeline_id),
                "prepare_stage_id": stage_id,
                "prepare_status": str(prepare.get("status", "")).lower(),
                "inference_status": str(inference.get("status", "")).lower(),
                "classification": "cnb_build_gpu_pre_freezing_quota",
                "detail": detail,
            }
            break
        if matched is None:
            raise CampaignRepositoryError(
                f"build {sn} does not prove the CNB build-GPU pre-freezing quota gate"
            )
        evidence.append(matched)
    return {
        "classification": "cnb_build_gpu_pre_freezing_quota",
        "verified_at": now_iso(),
        "builds": evidence,
    }


def _validated_devgpu_history_review(
    history_review_path: str | Path,
    *,
    source_receipt_path: Path,
    source_receipt: Mapping[str, Any],
    operations_sha256: str,
    required_action: str | None = None,
) -> dict[str, Any]:
    """Bind an operator's history review to the exact recovery attempt.

    CNB host-state failures can look like runner failures in a short status
    response.  An executable recovery therefore needs a small, receipt-bound
    review that proves the operator looked at prior evidence and selected an
    existing operation before allocating another Dev GPU workspace.
    """

    review_file = Path(history_review_path).expanduser().resolve()
    review = _read_json(review_file)
    errors: list[str] = []
    expected = {
        "schema_version": 1,
        "atom": "cnb_campaign_devgpu_recovery",
        "run_id": source_receipt.get("run_id"),
        "repository": source_receipt.get("repository"),
        "source_receipt_sha256": sha256_file(source_receipt_path),
        "operations_sha256": operations_sha256,
    }
    for key, value in expected.items():
        if review.get(key) != value:
            errors.append(f"history review {key} does not bind the current recovery")
    if not isinstance(review.get("reviewed_at"), str) or not str(review["reviewed_at"]).strip():
        errors.append("history review lacks reviewed_at")
    fingerprint = review.get("failure_fingerprint")
    if not isinstance(fingerprint, Mapping):
        errors.append("history review lacks a failure_fingerprint object")
    elif not isinstance(fingerprint.get("classification"), str) or not str(fingerprint["classification"]).strip():
        errors.append("history review failure_fingerprint lacks classification")
    evidence = review.get("history_evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("history review needs at least one historical evidence record")
    else:
        for index, item in enumerate(evidence, start=1):
            if not isinstance(item, Mapping):
                errors.append(f"history review evidence {index} is not an object")
                continue
            for key in ("source", "finding", "reused_operation"):
                if not isinstance(item.get(key), str) or not str(item[key]).strip():
                    errors.append(f"history review evidence {index} lacks {key}")
    decision = review.get("decision")
    if not isinstance(decision, Mapping):
        errors.append("history review lacks a decision object")
    else:
        for key in ("action", "rationale"):
            if not isinstance(decision.get(key), str) or not str(decision[key]).strip():
                errors.append(f"history review decision lacks {key}")
        if required_action is not None and decision.get("action") != required_action:
            errors.append(
                f"history review decision.action must equal profile-required action {required_action!r}"
            )
    if errors:
        raise CampaignRepositoryError("Dev GPU history review is invalid: " + "; ".join(errors))
    return {
        "path": str(review_file),
        "sha256": sha256_file(review_file),
        "failure_fingerprint": copy.deepcopy(dict(fingerprint)),
        "decision": copy.deepcopy(dict(decision)),
    }


def _validated_fresh_devgpu_admission(
    admission_path: str | Path,
    *,
    source_receipt_path: Path,
    source_receipt: Mapping[str, Any],
    operations_sha256: str,
) -> dict[str, Any]:
    """Validate the explicit human admission for a fresh direct Dev GPU run.

    This is intentionally not a recovery history review: it must not invent a
    Build-GPU failure simply to enter the Dev GPU route.
    """

    path = Path(admission_path).expanduser().resolve()
    value = _read_json(path)
    expected = {
        "schema_version": 1,
        "atom": "cnb_campaign_devgpu_launch",
        "run_id": source_receipt.get("run_id"),
        "repository": source_receipt.get("repository"),
        "source_receipt_sha256": sha256_file(source_receipt_path),
        "operations_sha256": operations_sha256,
    }
    errors = [f"fresh Dev GPU admission {key} does not bind the prepared campaign" for key, item in expected.items() if value.get(key) != item]
    manifest = value.get("manifest")
    if not isinstance(manifest, Mapping) or _manifest_binding_fields(manifest) != _manifest_binding_fields(source_receipt.get("manifest", {})):
        errors.append("fresh Dev GPU admission manifest does not match the prepared receipt")
    authorization = value.get("user_authorization")
    if not isinstance(authorization, Mapping) or authorization.get("action") != "user_authorized_fresh_direct_devgpu_campaign":
        errors.append("fresh Dev GPU admission lacks the explicit direct-launch authorization")
    elif not isinstance(authorization.get("authorized_at"), str) or not str(authorization["authorized_at"]).strip():
        errors.append("fresh Dev GPU admission authorization lacks authorized_at")
    snapshot = value.get("quota_snapshot")
    required_snapshot = ("captured_at", "quota_total_dev_gpu_seconds", "volume_used_dev_gpu_seconds", "volume_frozen_dev_gpu_seconds", "available_dev_gpu_seconds")
    if not isinstance(snapshot, Mapping):
        errors.append("fresh Dev GPU admission lacks a quota_snapshot")
    else:
        for field in required_snapshot:
            item = snapshot.get(field)
            if field == "captured_at":
                valid = isinstance(item, str) and bool(item.strip())
            else:
                valid = isinstance(item, int) and not isinstance(item, bool) and item >= 0
            if not valid:
                errors.append(f"fresh Dev GPU admission quota_snapshot.{field} is invalid")
        if not errors and snapshot["available_dev_gpu_seconds"] != (
            snapshot["quota_total_dev_gpu_seconds"] - snapshot["volume_used_dev_gpu_seconds"] - snapshot["volume_frozen_dev_gpu_seconds"]
        ):
            errors.append("fresh Dev GPU admission quota snapshot arithmetic is inconsistent")
    if errors:
        raise CampaignRepositoryError("fresh Dev GPU admission is invalid: " + "; ".join(errors))
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "user_authorization": copy.deepcopy(dict(authorization)),
        "quota_snapshot": copy.deepcopy(dict(snapshot)),
    }


def recover_campaign_with_devgpu(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    source_receipt_path: str | Path,
    recovery_receipt_path: str | Path,
    run_dir: str | Path,
    history_review_path: str | Path | None = None,
    execute: bool = False,
    wait: bool = True,
    timeout_seconds: float = DEVGPU_WORKSPACE_MAX_SECONDS,
    poll_seconds: float = 10.0,
    runner: JsonRunner = run_cnb,
    transport: str | None = None,
    repository_root: str | Path | None = None,
    github_commit: str | None = None,
    devgpu_profile: str = "L40",
) -> dict[str, Any]:
    """Recover a failed build-GPU campaign through one full Dev GPU workspace."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_devgpu_recovery")
    operations_sha256 = sha256_file(operations)
    policy = _policy_with_transport(load_campaign_policy(policy_path), transport)
    selected_profile = resolve_devgpu_recovery_profile(policy, devgpu_profile)
    source_file = Path(source_receipt_path).expanduser().resolve()
    source = _read_json(source_file)
    binding = validate_campaign_receipt_binding(policy, source)
    errors = list(binding["errors"])
    if source.get("status") not in {"failed", "interrupted"}:
        errors.append("Dev GPU recovery requires a failed or interrupted source receipt")
    if source.get("repository_created") is not True or source.get("repository_pushed") is not True:
        errors.append("source receipt lacks repository creation/push proof")
    if source.get("delivery") is not None:
        errors.append("source receipt already has a delivery")
    if errors:
        raise CampaignRepositoryError("Dev GPU recovery source is invalid: " + "; ".join(errors))
    platform_gate = _verify_build_gpu_platform_gate(source, runner)
    if not _repo_exists(str(source["repository"]), runner):
        raise CampaignRepositoryError("receipt-bound campaign repository is missing")

    recovery_file = Path(recovery_receipt_path).expanduser().resolve()
    run_dir_path = Path(run_dir).expanduser().resolve()
    recovery_dir = recovery_file.parent
    recovery_dir.mkdir(parents=True, exist_ok=True)
    existing = _read_json(recovery_file) if recovery_file.is_file() else {}
    if existing.get("status") == "completed":
        return {**existing, "receipt": str(recovery_file)}
    attempts = list(existing.get("attempts", [])) if isinstance(existing.get("attempts"), list) else []
    if existing.get("status") == "failed" and isinstance(existing.get("workspace"), Mapping):
        attempts.append(
            {
                "status": "failed",
                "updated_at": existing.get("updated_at"),
                "overlay": copy.deepcopy(existing.get("overlay")),
                "workspace": copy.deepcopy(existing.get("workspace")),
                "failure": copy.deepcopy(existing.get("failure")),
            }
        )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "atom": "cnb_campaign_devgpu_recovery",
        "status": "planned" if not execute else "preparing",
        "operations_sha256": operations_sha256,
        "source_receipt": str(source_file),
        "source_receipt_sha256": sha256_file(source_file),
        "run_id": source["run_id"],
        "repository": source["repository"],
        "manifest": copy.deepcopy(source["manifest"]),
        "runtime_image": source["runtime_image"],
        "campaign_commit": source["campaign_commit"],
        "devgpu_profile": copy.deepcopy(selected_profile),
        "build_gpu_platform_gate": platform_gate,
        "attempts": attempts,
        "created_at": existing.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "failure": None,
    }
    if not execute:
        _atomic_write_json(recovery_file, receipt)
        receipt["receipt"] = str(recovery_file)
        return receipt
    if not wait:
        raise CampaignRepositoryError("executable Dev GPU recovery must wait for the full workspace stage")
    if history_review_path is None:
        raise CampaignRepositoryError(
            "executable Dev GPU recovery requires --history-review bound to the current receipt"
        )
    if repository_root is None or github_commit is None:
        raise CampaignRepositoryError(
            "executable Dev GPU recovery requires --repository-root and --github-commit for the GitHub runner refresh"
        )
    root = Path(repository_root).expanduser().resolve()
    refresh_commit = _validate_commit(root, github_commit)
    receipt["history_review"] = _validated_devgpu_history_review(
        history_review_path,
        source_receipt_path=source_file,
        source_receipt=source,
        operations_sha256=operations_sha256,
        required_action=selected_profile.get("required_history_review_action"),
    )
    _atomic_write_json(recovery_file, receipt)

    workspace_sn = ""
    try:
        ledger_branch = str(policy["campaign_repository"]["ledger_branch_template"]).format(
            campaign_id=source["run_id"]
        )
        pending_preflight = _plan_devgpu_pending_preflight(
            source,
            recovery_dir=recovery_dir,
            ledger_branch=ledger_branch,
            execution_profile=str(selected_profile["execution_profile"]),
            shard_count=int(policy["campaign_repository"]["shard_count"]),
        )
        receipt["pending_preflight"] = pending_preflight
        receipt["updated_at"] = now_iso()
        _atomic_write_json(recovery_file, receipt)
        delivery_source = dict(source)
        delivery_source["ledger_branch"] = ledger_branch
        if pending_preflight["global_pending_item_count"] == 0:
            receipt["workspace"] = {
                "status": "not_started",
                "reason": "zero_pending_items_confirmed_locally",
            }
            receipt["status"] = "recovering_delivery_without_workspace"
            receipt["updated_at"] = now_iso()
            _atomic_write_json(recovery_file, receipt)
            delivery = _recover_delivery(
                delivery_source,
                run_dir=run_dir_path,
                require_source_url=True,
                fresh_ledger=True,
            )
            receipt["logical_shards"] = [
                {
                    "index": item["index"],
                    "id": item["id"],
                    "status": "already_complete_for_selected_shard",
                    "pending_item_count": 0,
                }
                for item in pending_preflight["shards"]
            ]
            receipt["delivery"] = delivery
            receipt["status"] = "completed"
            receipt["updated_at"] = now_iso()
            _atomic_write_json(recovery_file, receipt)
            return {**receipt, "receipt": str(recovery_file)}
        capacity_preflight = devgpu_capacity_preflight(
            policy,
            pending_item_count=int(pending_preflight["global_pending_item_count"]),
            runner=runner,
        )
        receipt["devgpu_capacity_preflight"] = capacity_preflight
        receipt["updated_at"] = now_iso()
        _atomic_write_json(recovery_file, receipt)
        if not capacity_preflight["clean"]:
            raise CampaignRepositoryError(
                "Dev GPU capacity preflight failed: " + "; ".join(capacity_preflight["errors"])
            )
        workspace_preflight = _workspace_preflight(str(source["repository"]), runner)
        receipt["workspace_preflight"] = workspace_preflight
        receipt["updated_at"] = now_iso()
        _atomic_write_json(recovery_file, receipt)
        if workspace_preflight["status"] != "clean":
            raise CampaignRepositoryError(
                "Dev GPU recovery found a visible running workspace for the disposable campaign"
            )
        runtime_export_dir, runner_refresh = _prepare_devgpu_runner_refresh(
            repository_root=root,
            github_commit=refresh_commit,
            recovery_dir=recovery_dir,
        )
        receipt["runner_refresh"] = runner_refresh
        overlay = _prepare_devgpu_overlay(
            policy=policy,
            source_receipt=source,
            recovery_dir=recovery_dir,
            runtime_export_dir=runtime_export_dir,
            runner_refresh=runner_refresh,
            devgpu_profile=selected_profile["name"],
        )
        receipt["overlay"] = overlay
        receipt["status"] = "starting_workspace"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(recovery_file, receipt)
        workspace_sn, reused_workspace = _start_receipt_bound_workspace(
            repository=str(source["repository"]), branch=str(overlay["branch"]), runner=runner
        )
        receipt["workspace"] = {
            "sn": workspace_sn,
            "status": "submitted",
            "reused_visible_workspace": reused_workspace,
            "timeout_seconds": min(float(timeout_seconds), DEVGPU_WORKSPACE_MAX_SECONDS),
            "lifecycle_guard_seconds": DEVGPU_WORKSPACE_LIFECYCLE_GUARD_SECONDS,
        }
        receipt["status"] = "running"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(recovery_file, receipt)
        started = time.monotonic()
        pipeline_id: str | None = None
        while True:
            if time.monotonic() - started > min(float(timeout_seconds), DEVGPU_WORKSPACE_MAX_SECONDS):
                raise CampaignRepositoryError(f"Dev GPU recovery workspace timed out: {workspace_sn}")
            status, pipeline_id = _workspace_recovery_stage_status(str(source["repository"]), workspace_sn, runner)
            receipt["workspace"].update({"status": status, "pipeline_id": pipeline_id})
            receipt["updated_at"] = now_iso()
            _atomic_write_json(recovery_file, receipt)
            if status in TERMINAL_BUILD_STATES:
                if status != "success":
                    raise CampaignRepositoryError(f"Dev GPU recovery stage reached terminal status {status}")
                break
            time.sleep(max(0.1, poll_seconds))
        try:
            runner(["cnb", "workspace", "workspace-stop", "--sn", workspace_sn, "--verbose"])
            receipt["workspace"]["stopped"] = True
        except Exception as exc:
            receipt["workspace"]["stop_error"] = str(exc)
            raise CampaignRepositoryError(f"Dev GPU recovery succeeded but workspace stop failed: {exc}") from exc
        delivery = _recover_delivery(
            delivery_source,
            run_dir=run_dir_path,
            require_source_url=True,
            fresh_ledger=True,
        )
        receipt["logical_shards"] = [
            {"index": index, "id": f"{source['run_id']}-s{index}", "status": "success", "workspace_sn": workspace_sn}
            for index in range(1, int(policy["campaign_repository"]["shard_count"]) + 1)
        ]
        receipt["delivery"] = delivery
        receipt["status"] = "completed"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(recovery_file, receipt)
        return {**receipt, "receipt": str(recovery_file)}
    except Exception as exc:
        if workspace_sn:
            try:
                runner(["cnb", "workspace", "workspace-stop", "--sn", workspace_sn, "--verbose"])
                receipt.setdefault("workspace", {})["stopped_after_failure"] = True
            except Exception as stop_exc:
                receipt.setdefault("workspace", {})["stop_error"] = str(stop_exc)
        receipt["status"] = "failed"
        receipt["failure"] = {"phase": "devgpu_recovery", "message": str(exc)}
        receipt["updated_at"] = now_iso()
        _atomic_write_json(recovery_file, receipt)
        raise


def launch_fresh_campaign_with_devgpu(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    source_receipt_path: str | Path,
    admission_path: str | Path,
    launch_receipt_path: str | Path,
    run_dir: str | Path,
    execute: bool = False,
    wait: bool = True,
    timeout_seconds: float = DEVGPU_WORKSPACE_MAX_SECONDS,
    poll_seconds: float = 10.0,
    runner: JsonRunner = run_cnb,
    transport: str | None = None,
    repository_root: str | Path | None = None,
    github_commit: str | None = None,
    devgpu_profile: str = "L40",
) -> dict[str, Any]:
    """Launch a fresh, explicitly admitted campaign in Dev GPU.

    This route is deliberately separate from ``recover_campaign_with_devgpu``:
    it never submits a Build-GPU job and it cannot accept a receipt that has
    already entered the build or delivery lifecycle.
    """

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_devgpu_launch")
    operations_sha256 = sha256_file(operations)
    policy = _policy_with_transport(load_campaign_policy(policy_path), transport)
    selected_profile = resolve_devgpu_recovery_profile(policy, devgpu_profile)
    source_file = Path(source_receipt_path).expanduser().resolve()
    source = _read_json(source_file)
    binding = validate_campaign_receipt_binding(policy, source, operations_sha256=None)
    errors = list(binding["errors"])
    if source.get("status") not in {"created_and_pushed", "planned"}:
        errors.append("fresh Dev GPU launch requires a prepared-and-pushed campaign receipt")
    if source.get("repository_created") is not True or source.get("repository_pushed") is not True:
        errors.append("fresh Dev GPU launch requires repository creation/push proof")
    if source.get("delivery") is not None:
        errors.append("fresh Dev GPU launch rejects a receipt that already has delivery")
    builds = source.get("builds")
    if not isinstance(builds, list) or builds:
        errors.append("fresh Dev GPU launch rejects a receipt with submitted builds")
    if errors:
        raise CampaignRepositoryError("fresh Dev GPU source is invalid: " + "; ".join(errors))
    if not _repo_exists(str(source["repository"]), runner):
        raise CampaignRepositoryError("receipt-bound campaign repository is missing")

    admission = _validated_fresh_devgpu_admission(
        admission_path,
        source_receipt_path=source_file,
        source_receipt=source,
        operations_sha256=operations_sha256,
    )
    launch_file = Path(launch_receipt_path).expanduser().resolve()
    launch_dir = launch_file.parent
    launch_dir.mkdir(parents=True, exist_ok=True)
    existing = _read_json(launch_file) if launch_file.is_file() else {}
    if existing.get("status") == "completed":
        return {**existing, "receipt": str(launch_file)}
    if existing and (
        existing.get("source_receipt_sha256") != sha256_file(source_file)
        or existing.get("admission", {}).get("sha256") != admission["sha256"]
    ):
        raise CampaignRepositoryError("fresh Dev GPU launch receipt is not bound to this source/admission")
    attempts = list(existing.get("attempts", [])) if isinstance(existing.get("attempts"), list) else []
    if existing.get("status") == "failed" and isinstance(existing.get("workspace"), Mapping):
        attempts.append({"status": "failed", "updated_at": existing.get("updated_at"), "workspace": copy.deepcopy(existing["workspace"]), "failure": copy.deepcopy(existing.get("failure"))})
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "atom": "cnb_campaign_devgpu_launch",
        "status": "planned" if not execute else "preparing",
        "operations_sha256": operations_sha256,
        "source_receipt": str(source_file),
        "source_receipt_sha256": sha256_file(source_file),
        "admission": admission,
        "run_id": source["run_id"],
        "repository": source["repository"],
        "manifest": copy.deepcopy(source["manifest"]),
        "runtime_image": source["runtime_image"],
        "campaign_commit": source["campaign_commit"],
        "devgpu_profile": copy.deepcopy(selected_profile),
        "execution_mode": "fresh_direct_devgpu",
        "attempts": attempts,
        "created_at": existing.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "failure": None,
    }
    if not execute:
        _atomic_write_json(launch_file, receipt)
        return {**receipt, "receipt": str(launch_file)}
    if not wait:
        raise CampaignRepositoryError("executable fresh Dev GPU launch must wait for the full workspace stage")
    if repository_root is None or github_commit is None:
        raise CampaignRepositoryError("executable fresh Dev GPU launch requires --repository-root and --github-commit")
    root = Path(repository_root).expanduser().resolve()
    refresh_commit = _validate_commit(root, github_commit)
    workspace_sn = ""
    try:
        ledger_branch = str(policy["campaign_repository"]["ledger_branch_template"]).format(campaign_id=source["run_id"])
        pending_preflight = _plan_devgpu_pending_preflight(source, recovery_dir=launch_dir, ledger_branch=ledger_branch, execution_profile=str(selected_profile["execution_profile"]), shard_count=int(policy["campaign_repository"]["shard_count"]))
        receipt["pending_preflight"] = pending_preflight
        _atomic_write_json(launch_file, receipt)
        if pending_preflight["global_pending_item_count"] == 0:
            raise CampaignRepositoryError("fresh Dev GPU launch requires pending items; use the normal delivery recovery path")
        capacity = devgpu_capacity_preflight(policy, pending_item_count=int(pending_preflight["global_pending_item_count"]), runner=runner)
        receipt["devgpu_capacity_preflight"] = capacity
        _atomic_write_json(launch_file, receipt)
        if not capacity["clean"]:
            raise CampaignRepositoryError("Dev GPU capacity preflight failed: " + "; ".join(capacity["errors"]))
        workspace_preflight = _workspace_preflight(str(source["repository"]), runner)
        receipt["workspace_preflight"] = workspace_preflight
        _atomic_write_json(launch_file, receipt)
        if workspace_preflight["status"] != "clean":
            raise CampaignRepositoryError(
                "fresh Dev GPU launch found a visible running workspace for the disposable campaign"
            )
        runtime_export_dir, runner_refresh = _prepare_devgpu_runner_refresh(repository_root=root, github_commit=refresh_commit, recovery_dir=launch_dir)
        receipt["runner_refresh"] = runner_refresh
        overlay = _prepare_devgpu_overlay(policy=policy, source_receipt=source, recovery_dir=launch_dir, runtime_export_dir=runtime_export_dir, runner_refresh=runner_refresh, devgpu_profile=selected_profile["name"], overlay_kind="campaign")
        receipt["overlay"] = overlay
        receipt["status"] = "starting_workspace"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(launch_file, receipt)
        workspace_sn, reused_workspace = _start_receipt_bound_workspace(
            repository=str(source["repository"]), branch=str(overlay["branch"]), runner=runner
        )
        receipt["workspace"] = {
            "sn": workspace_sn,
            "status": "submitted",
            "reused_visible_workspace": reused_workspace,
            "timeout_seconds": min(float(timeout_seconds), DEVGPU_WORKSPACE_MAX_SECONDS),
            "lifecycle_guard_seconds": DEVGPU_WORKSPACE_LIFECYCLE_GUARD_SECONDS,
        }
        receipt["status"] = "running"
        _atomic_write_json(launch_file, receipt)
        started = time.monotonic()
        while True:
            if time.monotonic() - started > min(float(timeout_seconds), DEVGPU_WORKSPACE_MAX_SECONDS):
                raise CampaignRepositoryError(f"fresh Dev GPU workspace timed out: {workspace_sn}")
            status, pipeline_id = _workspace_recovery_stage_status(str(source["repository"]), workspace_sn, runner)
            receipt["workspace"].update({"status": status, "pipeline_id": pipeline_id})
            _atomic_write_json(launch_file, receipt)
            if status in TERMINAL_BUILD_STATES:
                if status != "success":
                    raise CampaignRepositoryError(f"fresh Dev GPU stage reached terminal status {status}")
                break
            time.sleep(max(0.1, poll_seconds))
        runner(["cnb", "workspace", "workspace-stop", "--sn", workspace_sn, "--verbose"])
        receipt["workspace"]["stopped"] = True
        delivery_source = dict(source)
        delivery_source["ledger_branch"] = ledger_branch
        receipt["delivery"] = _recover_delivery(delivery_source, run_dir=Path(run_dir).expanduser().resolve(), require_source_url=True, fresh_ledger=True)
        receipt["logical_shards"] = [{"index": item["index"], "id": item["id"], "status": "success", "pending_item_count": item["pending_item_count"], "workspace_sn": workspace_sn} for item in pending_preflight["shards"]]
        receipt["status"] = "completed"
        receipt["updated_at"] = now_iso()
        _atomic_write_json(launch_file, receipt)
        return {**receipt, "receipt": str(launch_file)}
    except Exception as exc:
        if workspace_sn:
            try:
                runner(["cnb", "workspace", "workspace-stop", "--sn", workspace_sn, "--verbose"])
                receipt.setdefault("workspace", {})["stopped_after_failure"] = True
            except Exception as stop_exc:
                receipt.setdefault("workspace", {})["stop_error"] = str(stop_exc)
        receipt["status"] = "failed"
        receipt["failure"] = {"phase": "fresh_devgpu_launch", "message": str(exc)}
        receipt["updated_at"] = now_iso()
        _atomic_write_json(launch_file, receipt)
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


_MANIFEST_DELIVERY_IDENTITY_FIELDS = (
    "campaign_id",
    "id",
    "manifest_index",
    "title",
    "artist",
    "relative_audio_path",
    "source_sha256",
    "source_bytes",
    "source_url",
)
_DELIVERY_RELEASE_IDENTITY_FIELDS = (
    *_MANIFEST_DELIVERY_IDENTITY_FIELDS,
    "output_text_sha256",
    "generated_token_count",
    "max_new_tokens",
    "contract",
    "attempt_id",
    "canonical_source",
)


def _manifest_identity_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for line_number, row in enumerate(rows, 1):
        item_id = str(row["id"])
        if item_id in result:
            raise CampaignRepositoryError(f"duplicate manifest identity during reconciliation: {item_id}")
        result[item_id] = {
            "campaign_id": str(row["campaign_id"]),
            "id": item_id,
            "manifest_index": line_number,
            "title": str(row["title"]),
            "artist": str(row["artist"]),
            "relative_audio_path": str(row["relative_audio_path"]),
            "source_sha256": str(row["sha256"]),
            "source_bytes": int(row["source_bytes"]),
            "source_url": str(row["source_url"]),
        }
    return result


def _delivery_identity_map(entries: Sequence[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        item_id = str(entry.delivery_id)
        if item_id in result:
            raise CampaignRepositoryError(f"duplicate external delivery identity during reconciliation: {item_id}")
        result[item_id] = {
            "campaign_id": str(entry.campaign_id),
            "id": item_id,
            "manifest_index": int(entry.manifest_index),
            "title": str(entry.title),
            "artist": str(entry.artist),
            "relative_audio_path": str(entry.relative_audio_path),
            "source_sha256": str(entry.source_sha256),
            "source_bytes": int(entry.source_bytes),
            "source_url": str(entry.source_url or ""),
            "output_text_sha256": str(entry.output_text_sha256),
            "generated_token_count": int(entry.generated_token_count),
            "max_new_tokens": int(entry.max_new_tokens),
            "contract": str(entry.contract),
            "attempt_id": str(entry.attempt_id),
            "canonical_source": str(entry.canonical_source),
        }
    return result


def _release_identity_map(
    release_manifest: str | Path, *, campaign_id: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Verify the immutable release and recover its exact delivery provenance."""

    release = verify_snapshot(Path(release_manifest).expanduser().resolve())
    database = Path(str(release["database"])).expanduser().resolve()
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT c.campaign_id, c.delivery_id, c.manifest_index,
                   c.source_title, c.source_artist, c.relative_audio_path,
                   c.source_sha256, c.source_bytes, c.output_text_sha256,
                   c.generated_token_count, c.max_new_tokens, c.contract,
                   c.attempt_id, c.canonical_source, st.source_url
            FROM campaign_delivery_provenance AS c
            JOIN analysis_revision AS ar ON ar.id = c.analysis_id
            JOIN source_track AS st
              ON st.recording_id = ar.recording_id
             AND st.source_name = 'kugou'
             AND st.source_track_id = c.delivery_id
            WHERE c.campaign_id = ?
            ORDER BY c.manifest_index, c.delivery_id
            """,
            (campaign_id,),
        ).fetchall()
    finally:
        connection.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item_id = str(row["delivery_id"])
        if item_id in result:
            raise CampaignRepositoryError(
                f"release has duplicate campaign provenance for delivery id: {item_id}"
            )
        result[item_id] = {
            "campaign_id": str(row["campaign_id"]),
            "id": item_id,
            "manifest_index": int(row["manifest_index"]),
            "title": str(row["source_title"]),
            "artist": str(row["source_artist"]),
            "relative_audio_path": str(row["relative_audio_path"]),
            "source_sha256": str(row["source_sha256"]),
            "source_bytes": int(row["source_bytes"]),
            "source_url": str(row["source_url"] or ""),
            "output_text_sha256": str(row["output_text_sha256"]),
            "generated_token_count": int(row["generated_token_count"]),
            "max_new_tokens": int(row["max_new_tokens"]),
            "contract": str(row["contract"]),
            "attempt_id": str(row["attempt_id"]),
            "canonical_source": str(row["canonical_source"]),
        }
    return release, result


def _identity_map_errors(
    expected: Mapping[str, Mapping[str, Any]],
    actual: Mapping[str, Mapping[str, Any]],
    *,
    fields: Sequence[str],
    expected_name: str,
    actual_name: str,
) -> list[str]:
    errors: list[str] = []
    expected_ids = set(expected)
    actual_ids = set(actual)
    if expected_ids != actual_ids:
        errors.append(
            f"{expected_name}/{actual_name} identity set differs: "
            f"missing={sorted(expected_ids - actual_ids)[:10]} extra={sorted(actual_ids - expected_ids)[:10]}"
        )
    for item_id in sorted(expected_ids & actual_ids):
        mismatched = [
            field for field in fields if expected[item_id].get(field) != actual[item_id].get(field)
        ]
        if mismatched:
            errors.append(
                f"{expected_name}/{actual_name} identity mismatch for {item_id}: {', '.join(mismatched)}"
            )
    return errors


def _identity_set_sha256(rows: Mapping[str, Mapping[str, Any]]) -> str:
    payload = [dict(rows[item_id]) for item_id in sorted(rows)]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _external_delivery_reconciliation_proof(
    *,
    policy: Mapping[str, Any],
    source_receipt_path: str | Path,
    delivery_path: str | Path,
    release_manifest_path: str | Path,
) -> dict[str, Any]:
    """Build a fail-closed proof for a legacy receipt with external delivery.

    The original failed receipt remains immutable.  The only acceptable
    substitute for its missing delivery field is an independently revalidated
    delivery which exactly matches the original manifest and is present in a
    verified immutable SQLite release.
    """

    source_receipt_file = Path(source_receipt_path).expanduser().resolve()
    receipt = _read_json(source_receipt_file)
    source_receipt_sha256 = sha256_file(source_receipt_file)
    manifest = receipt.get("manifest")
    if not isinstance(manifest, Mapping):
        raise CampaignRepositoryError("legacy receipt has no manifest for external-delivery reconciliation")
    manifest_path = Path(str(manifest.get("path", ""))).expanduser().resolve()
    manifest_summary, manifest_rows = _read_manifest_contract(
        manifest_path,
        expected_count=int(manifest.get("item_count", 0)),
    )
    errors = campaign_receipt_binding_errors(policy, receipt, manifest=manifest_summary)
    if receipt.get("status") not in {"failed", "interrupted"}:
        errors.append("external-delivery reconciliation only accepts a failed or interrupted legacy receipt")
    if receipt.get("delivery") is not None:
        errors.append("legacy receipt already has a delivery; ordinary receipt-bound cleanup is required")
    if receipt.get("repository_created") is not True or receipt.get("repository_pushed") is not True:
        errors.append("legacy receipt lacks repository creation/push provenance")
    runtime_export = receipt.get("runtime_export")
    if not isinstance(runtime_export, Mapping) or runtime_export.get("validated") is not True:
        errors.append("legacy receipt lacks validated runtime export provenance")
    if not re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("github_commit", ""))):
        errors.append("legacy receipt lacks a valid GitHub source commit")
    repository = str(receipt.get("repository", ""))
    if repository == str(policy.get("protected_runtime_repository_slug", "")):
        errors.append("legacy receipt targets the protected runtime repository")
    if errors:
        return {"valid": False, "errors": sorted(set(errors))}

    delivery_file = Path(delivery_path).expanduser().resolve()
    entries = load_campaign_delivery_file(delivery_file, expected_count=int(manifest_summary["item_count"]))
    manifest_identity = _manifest_identity_map(manifest_rows)
    delivery_identity = _delivery_identity_map(entries)
    errors.extend(
        _identity_map_errors(
            manifest_identity,
            delivery_identity,
            fields=_MANIFEST_DELIVERY_IDENTITY_FIELDS,
            expected_name="source manifest",
            actual_name="external delivery",
        )
    )
    if any(not str(row["source_url"]) for row in delivery_identity.values()):
        errors.append("external delivery is missing one or more source_url values")
    release_file = Path(release_manifest_path).expanduser().resolve()
    release, release_identity = _release_identity_map(
        release_file,
        campaign_id=str(manifest_summary["campaign_id"]),
    )
    errors.extend(
        _identity_map_errors(
            delivery_identity,
            release_identity,
            fields=_DELIVERY_RELEASE_IDENTITY_FIELDS,
            expected_name="external delivery",
            actual_name="verified release",
        )
    )
    if errors:
        return {"valid": False, "errors": sorted(set(errors))}
    return {
        "valid": True,
        "source_campaign_receipt": {
            "path": str(source_receipt_file),
            "sha256": source_receipt_sha256,
            "run_id": str(receipt["run_id"]),
            "repository": repository,
            "github_commit": str(receipt["github_commit"]),
            "manifest": manifest_summary,
        },
        "external_delivery": {
            "path": str(delivery_file),
            "sha256": sha256_file(delivery_file),
            "count": len(entries),
            "campaign_id": str(manifest_summary["campaign_id"]),
        },
        "release": {
            "manifest": str(release_file),
            "manifest_sha256": sha256_file(release_file),
            "database": str(release["database"]),
            "database_sha256": str(release["sha256"]),
            "release_name": str(release["release_name"]),
        },
        "identity": {
            "count": len(delivery_identity),
            "sha256": _identity_set_sha256(delivery_identity),
        },
    }


def reconcile_external_delivery(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    source_receipt_path: str | Path,
    delivery_path: str | Path,
    release_manifest_path: str | Path,
    reconciliation_receipt_path: str | Path,
    transport: str | None = None,
) -> dict[str, Any]:
    """Persist a separate proof for a legacy campaign; never edit its receipt."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_cleanup")
    reconciliation_file = Path(reconciliation_receipt_path).expanduser().resolve()
    result: dict[str, Any] = {
        "schema_version": EXTERNAL_DELIVERY_RECONCILIATION_SCHEMA_VERSION,
        "atom": "cnb_external_delivery_reconciliation",
        "action": "reconcile-external-delivery",
        "operations_sha256": sha256_file(operations),
        "status": "blocked",
        "proof": None,
        "failures": [],
        "updated_at": now_iso(),
    }
    try:
        policy = _policy_with_transport(load_campaign_policy(policy_path), transport)
        proof = _external_delivery_reconciliation_proof(
            policy=policy,
            source_receipt_path=source_receipt_path,
            delivery_path=delivery_path,
            release_manifest_path=release_manifest_path,
        )
        if proof.get("valid"):
            result.update({"status": "succeeded", "proof": proof})
        else:
            result["failures"] = [{"kind": "proof", "error": error} for error in proof.get("errors", [])]
    except Exception as exc:
        result["failures"] = [{"kind": "proof", "error": str(exc)}]
    result["updated_at"] = now_iso()
    _atomic_write_json(reconciliation_file, result)
    result["reconciliation_receipt"] = str(reconciliation_file)
    return result


def external_delivery_reconciliation_validation_errors(
    policy: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    *,
    operations_sha256: str,
) -> list[str]:
    """Revalidate a stored proof immediately before destructive deletion."""

    errors: list[str] = []
    if reconciliation.get("schema_version") != EXTERNAL_DELIVERY_RECONCILIATION_SCHEMA_VERSION:
        errors.append("external reconciliation schema_version does not match")
    if reconciliation.get("atom") != "cnb_external_delivery_reconciliation":
        errors.append("external reconciliation atom does not match")
    if reconciliation.get("status") != "succeeded":
        errors.append("external reconciliation is not succeeded")
    if reconciliation.get("operations_sha256") != operations_sha256:
        errors.append("external reconciliation operations_sha256 does not match the validated operations file")
    stored_proof = reconciliation.get("proof")
    if not isinstance(stored_proof, Mapping):
        errors.append("external reconciliation proof is missing")
        return errors
    source = stored_proof.get("source_campaign_receipt")
    delivery = stored_proof.get("external_delivery")
    release = stored_proof.get("release")
    identity = stored_proof.get("identity")
    if not all(isinstance(value, Mapping) for value in (source, delivery, release, identity)):
        errors.append("external reconciliation proof shape is incomplete")
        return errors
    try:
        rebuilt = _external_delivery_reconciliation_proof(
            policy=policy,
            source_receipt_path=str(source["path"]),
            delivery_path=str(delivery["path"]),
            release_manifest_path=str(release["manifest"]),
        )
    except Exception as exc:
        errors.append(f"external reconciliation revalidation failed: {exc}")
        return errors
    if not rebuilt.get("valid"):
        errors.extend(f"external reconciliation proof failed: {error}" for error in rebuilt.get("errors", []))
        return sorted(set(errors))
    comparisons = (
        ("source_campaign_receipt", "sha256"),
        ("source_campaign_receipt", "run_id"),
        ("source_campaign_receipt", "repository"),
        ("external_delivery", "sha256"),
        ("external_delivery", "count"),
        ("external_delivery", "campaign_id"),
        ("release", "manifest_sha256"),
        ("release", "database_sha256"),
        ("release", "release_name"),
        ("identity", "count"),
        ("identity", "sha256"),
    )
    for section, field in comparisons:
        if stored_proof[section].get(field) != rebuilt[section].get(field):
            errors.append(f"external reconciliation {section}.{field} changed since proof creation")
    return sorted(set(errors))


def _perform_disposable_repository_delete(
    *, policy: Mapping[str, Any], repository: str, runner: JsonRunner, result: dict[str, Any]
) -> None:
    """Delete one already-proven disposable repository and record every check."""

    protected_before = _protected_runtime_status(policy, runner)
    result["protected_runtime_before"] = protected_before
    if not protected_before["protected"]:
        result["failures"].append(
            {"kind": "protected-runtime", "error": "protected runtime/main/digest is unhealthy"}
        )
        return
    workspaces_response, absent = _cnb_optional(
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
    workspace_rows = (
        ((_response_data(workspaces_response) or {}).get("list", []))
        if not absent and workspaces_response
        else []
    )
    if workspace_rows:
        result["failures"].append({"kind": "workspace", "error": "running workspace exists"})
        return
    organization = str(policy["organization_slug"])
    before_group = _group_volume(organization, runner)["object"]
    before_repo = _repo_volume(organization, repository, runner)
    result.update(
        {
            "group_object_used_bytes_before": before_group,
            "repository_object_bytes_before": before_repo,
        }
    )
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
    result.update(
        {
            "repository_present_after": present_after,
            "repository_object_bytes_after": after_repo,
            "group_object_used_bytes_after": after_group,
            "group_object_usage_decreased": after_group < before_group,
            "protected_runtime_after": protected_after,
        }
    )
    if present_after or after_repo != 0:
        result["failures"].append(
            {"kind": "repository-verification", "error": "repository is not 404/zero-volume"}
        )
    if before_repo > 0 and after_group >= before_group:
        result["failures"].append(
            {"kind": "organization-charge-verification", "error": "organization object usage did not decrease"}
        )
    if not protected_after["protected"]:
        result["failures"].append(
            {
                "kind": "protected-runtime-verification",
                "error": "protected runtime/main/digest missing after deletion",
            }
        )


def cleanup_reconciled_external_delivery_campaign(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    reconciliation_receipt_path: str | Path,
    confirm: bool,
    release_verified: bool,
    peer_gate: bool,
    runner: JsonRunner = run_cnb,
    transport: str | None = None,
) -> dict[str, Any]:
    """Delete a legacy campaign only through a revalidated external proof."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_cleanup")
    operations_sha256 = sha256_file(operations)
    policy = _policy_with_transport(load_campaign_policy(policy_path), transport)
    reconciliation_file = Path(reconciliation_receipt_path).expanduser().resolve()
    reconciliation = _read_json(reconciliation_file)
    proof = reconciliation.get("proof") if isinstance(reconciliation.get("proof"), Mapping) else {}
    source = proof.get("source_campaign_receipt") if isinstance(proof, Mapping) else {}
    repository = str(source.get("repository", "")) if isinstance(source, Mapping) else ""
    result: dict[str, Any] = {
        "action": "cleanup-reconciled-external-delivery",
        "repository": repository,
        "confirmed": confirm,
        "release_verified": release_verified,
        "peer_gate": peer_gate,
        "deleted": False,
        "failures": [],
    }

    def record_result() -> dict[str, Any]:
        reconciliation["cleanup"] = copy.deepcopy(result)
        reconciliation["updated_at"] = now_iso()
        _atomic_write_json(reconciliation_file, reconciliation)
        result["reconciliation_receipt"] = str(reconciliation_file)
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
    for error in external_delivery_reconciliation_validation_errors(
        policy, reconciliation, operations_sha256=operations_sha256
    ):
        result["failures"].append({"kind": "reconciliation", "error": error})
    if not release_verified:
        result["failures"].append({"kind": "release-gate", "error": "verified local release is required"})
    if not peer_gate:
        result["failures"].append(
            {"kind": "peer-gate", "error": "peer gate or explicit peer skip is required"}
        )
    if not repository:
        result["failures"].append({"kind": "repository", "error": "reconciled repository is missing"})
    if repository == str(policy["protected_runtime_repository_slug"]):
        result["failures"].append({"kind": "repository", "error": "protected runtime cannot be deleted"})
    if result["failures"]:
        result.update({"status": "blocked", "clean": False})
        return record_result()
    _perform_disposable_repository_delete(policy=policy, repository=repository, runner=runner, result=result)
    result["status"] = "succeeded" if not result["failures"] else "failed"
    result["clean"] = result["status"] == "succeeded"
    return record_result()


def cleanup_campaign_repository(
    *,
    policy_path: str | Path,
    operations_path: str | Path,
    receipt_path: str | Path,
    confirm: bool,
    release_verified: bool,
    peer_gate: bool,
    runner: JsonRunner = run_cnb,
    transport: str | None = None,
) -> dict[str, Any]:
    """Delete and verify one receipt-bound disposable repository."""

    operations = Path(operations_path).expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_campaign_cleanup")
    operations_sha256 = sha256_file(operations)
    policy = _policy_with_transport(load_campaign_policy(policy_path), transport)
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
    _perform_disposable_repository_delete(policy=policy, repository=repository, runner=runner, result=result)
    result["status"] = "succeeded" if not result["failures"] else "failed"
    result["clean"] = result["status"] == "succeeded"
    return record_result()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "preflight",
            "prepare",
            "submit",
            "recover-devgpu",
            "launch-devgpu",
            "cleanup",
            "reconcile-external-delivery",
            "cleanup-reconciled-external-delivery",
        ),
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--operations-file", type=Path, default=Path(__file__).resolve().parents[1] / "references" / "validated-operations.json")
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--recovery-receipt", type=Path)
    parser.add_argument("--admission", type=Path, help="Required receipt-bound authorization for fresh direct Dev GPU launch")
    parser.add_argument("--launch-receipt", type=Path)
    parser.add_argument(
        "--history-review",
        type=Path,
        help="Required before executable Dev GPU recovery; receipt-bound history review JSON",
    )
    parser.add_argument("--delivery", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--reconciliation-receipt", type=Path)
    parser.add_argument("--github-commit")
    parser.add_argument(
        "--devgpu-profile",
        default="L40",
        help="Explicit configured Dev GPU recovery profile (default: L40)",
    )
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--transport", choices=("lfs", "git-objects"))
    parser.add_argument("--resume-repository", help="Exact receipt-bound campaign repository allowed during resume")
    parser.add_argument(
        "--retained-campaign-receipt",
        type=Path,
        help="Explicit failed/interrupted receipt allowed to retain one otherwise-blocking campaign repository",
    )
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
        retained_campaign_receipt = (
            _read_json(args.retained_campaign_receipt.expanduser().resolve())
            if args.retained_campaign_receipt is not None
            else None
        )
        if args.action == "preflight":
            operations = args.operations_file.expanduser().resolve()
            load_validated_operations(operations, required_atom="cnb_campaign_repository")
            policy = _policy_with_transport(load_campaign_policy(args.policy), args.transport)
            result = campaign_preflight(
                policy,
                runner=run_cnb,
                resume_repository=args.resume_repository,
                retained_campaign_receipt=retained_campaign_receipt,
                retained_campaign_receipt_path=(
                    str(args.retained_campaign_receipt.expanduser().resolve())
                    if args.retained_campaign_receipt is not None
                    else None
                ),
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
                retained_campaign_receipt=retained_campaign_receipt,
                retained_campaign_receipt_path=(
                    str(args.retained_campaign_receipt.expanduser().resolve())
                    if args.retained_campaign_receipt is not None
                    else None
                ),
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
                transport=args.transport,
            )
        elif args.action == "recover-devgpu":
            if not args.receipt or not args.recovery_receipt or not args.run_dir:
                raise CampaignRepositoryError(
                    "recover-devgpu requires --receipt, --recovery-receipt, and --run-dir"
                )
            result = recover_campaign_with_devgpu(
                policy_path=args.policy,
                operations_path=args.operations_file,
                source_receipt_path=args.receipt,
                recovery_receipt_path=args.recovery_receipt,
                run_dir=args.run_dir,
                history_review_path=args.history_review,
                execute=args.execute,
                wait=args.wait,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                transport=args.transport,
                repository_root=args.repository_root,
                github_commit=args.github_commit,
                devgpu_profile=args.devgpu_profile,
            )
        elif args.action == "launch-devgpu":
            if not args.receipt or not args.admission or not args.launch_receipt or not args.run_dir:
                raise CampaignRepositoryError("launch-devgpu requires --receipt, --admission, --launch-receipt, and --run-dir")
            result = launch_fresh_campaign_with_devgpu(
                policy_path=args.policy,
                operations_path=args.operations_file,
                source_receipt_path=args.receipt,
                admission_path=args.admission,
                launch_receipt_path=args.launch_receipt,
                run_dir=args.run_dir,
                execute=args.execute,
                wait=args.wait,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                transport=args.transport,
                repository_root=args.repository_root,
                github_commit=args.github_commit,
                devgpu_profile=args.devgpu_profile,
            )
        elif args.action == "cleanup":
            if not args.receipt:
                raise CampaignRepositoryError("cleanup requires --receipt")
            result = cleanup_campaign_repository(
                policy_path=args.policy,
                operations_path=args.operations_file,
                receipt_path=args.receipt,
                confirm=args.confirm_delete_cnb_repositories,
                release_verified=args.release_verified,
                peer_gate=args.peer_gate,
                transport=args.transport,
            )
        elif args.action == "reconcile-external-delivery":
            if not args.receipt or not args.delivery or not args.release_manifest or not args.reconciliation_receipt:
                raise CampaignRepositoryError(
                    "reconcile-external-delivery requires --receipt, --delivery, --release-manifest, and --reconciliation-receipt"
                )
            result = reconcile_external_delivery(
                policy_path=args.policy,
                operations_path=args.operations_file,
                source_receipt_path=args.receipt,
                delivery_path=args.delivery,
                release_manifest_path=args.release_manifest,
                reconciliation_receipt_path=args.reconciliation_receipt,
                transport=args.transport,
            )
        else:
            if not args.reconciliation_receipt:
                raise CampaignRepositoryError(
                    "cleanup-reconciled-external-delivery requires --reconciliation-receipt"
                )
            result = cleanup_reconciled_external_delivery_campaign(
                policy_path=args.policy,
                operations_path=args.operations_file,
                reconciliation_receipt_path=args.reconciliation_receipt,
                confirm=args.confirm_delete_cnb_repositories,
                release_verified=args.release_verified,
                peer_gate=args.peer_gate,
                transport=args.transport,
            )
        print(json.dumps(result, ensure_ascii=False))
        if args.action == "preflight":
            return 0 if result.get("clean") else 3
        if args.action in {"cleanup", "cleanup-reconciled-external-delivery"} and args.confirm_delete_cnb_repositories:
            return 0 if result.get("clean") else 4
        if args.action == "reconcile-external-delivery":
            return 0 if result.get("status") == "succeeded" else 4
        return 0
    except (CampaignRepositoryError, OSError, ValueError) as exc:
        print(json.dumps({"action": args.action, "status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
