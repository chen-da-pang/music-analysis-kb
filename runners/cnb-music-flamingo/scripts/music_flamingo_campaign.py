#!/usr/bin/env python3
"""Manifest, contract, and ledger helpers for resumable Music Flamingo runs.

This module deliberately contains no model imports or storage-provider logic.
The inference runner owns execution and durable checkpoint transport, while this
module makes the local manifest and append-only ledger safe to resume from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IMMUTABLE_IMAGE_RE = re.compile(r".+@sha256:[0-9a-fA-F]{64}$")
_DEFAULT_MODEL_ID = "nvidia/music-flamingo-think-2601-hf"
_DEFAULT_MODEL_REVISION = "1ea2109"
_DEFAULT_MODEL_DIR = "/opt/models/music-flamingo-think-2601-hf"


class CampaignError(ValueError):
    """Base error for campaign input, contract, and ledger problems."""


class CampaignManifestError(CampaignError):
    """Raised when image-local campaign input cannot be safely used."""


class CampaignLedgerError(CampaignError):
    """Raised when a campaign ledger cannot be safely resumed."""


@dataclass(frozen=True)
class CampaignItem:
    """One validated campaign manifest row and its image-local audio file."""

    item_id: str
    relative_audio_path: str
    audio_path: Path
    source_bytes: int
    sha256: str
    title: str
    artist: str
    campaign_id: str
    manifest_index: int = 0

    @property
    def id(self) -> str:
        """Compatibility alias for the manifest's ``id`` field."""
        return self.item_id


@dataclass(frozen=True)
class RuntimeContract:
    """The execution inputs which make an inference result reusable."""

    runtime_image: str
    prompt_sha256: str
    max_new_tokens: int
    audio_clip_seconds: float
    model_id: str
    model_revision: str
    model_dir: str
    execution_profile: str
    runner_code_sha256: str
    fingerprint: str

    def ledger_fields(self) -> dict[str, object]:
        """Return auditable, non-prompt ledger fields for this contract."""
        return {
            "contract": self.fingerprint,
            "runtime_image": self.runtime_image,
            "prompt_sha256": self.prompt_sha256,
            "max_new_tokens": self.max_new_tokens,
            "audio_clip_seconds": self.audio_clip_seconds,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_dir": self.model_dir,
            "execution_profile": self.execution_profile,
            "runner_code_sha256": self.runner_code_sha256,
        }


@dataclass(frozen=True)
class CampaignInputConfig:
    """Campaign paths and count resolved from runner environment variables."""

    manifest_path: Path
    audio_root: Path
    expected_count: int
    expected_campaign_id: str


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file without loading it into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CampaignManifestError(f"Unable to hash audio file {path}: {exc}") from exc
    return digest.hexdigest()


def _require_item_id(value: object, *, field: str = "id", error_type: type[CampaignError] = CampaignManifestError) -> str:
    if not isinstance(value, str):
        raise error_type(f"{field} must be a string")
    item_id = value.strip()
    if not _ITEM_ID_RE.fullmatch(item_id):
        raise error_type(f"Unsafe {field}: {value!r}")
    return item_id


def _require_sha256(value: object, *, field: str = "sha256", error_type: type[CampaignError] = CampaignManifestError) -> str:
    if not isinstance(value, str):
        raise error_type(f"{field} must be a SHA-256 string")
    digest = value.strip()
    if not _SHA256_RE.fullmatch(digest):
        raise error_type(f"Invalid {field}: {value!r}")
    return digest.lower()


def _require_nonnegative_int(value: object, *, field: str, error_type: type[CampaignError] = CampaignManifestError) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error_type(f"{field} must be a non-negative integer")
    return value


def _require_text(value: object, *, field: str, error_type: type[CampaignError] = CampaignManifestError) -> str:
    if not isinstance(value, str):
        raise error_type(f"{field} must be a string")
    return value


def _safe_relative_audio_path(value: object) -> tuple[str, Path]:
    if not isinstance(value, str):
        raise CampaignManifestError("relative_audio_path must be a string")
    raw = value.strip()
    if not raw or "\\" in raw:
        raise CampaignManifestError(f"Unsafe relative_audio_path: {value!r}")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise CampaignManifestError(f"Unsafe relative_audio_path: {value!r}")
    return relative.as_posix(), Path(*relative.parts)


def _resolve_confined_audio_path(audio_root: Path, relative_path: Path) -> Path:
    candidate = (audio_root / relative_path).resolve()
    try:
        candidate.relative_to(audio_root)
    except ValueError as exc:
        raise CampaignManifestError(f"Audio path escapes audio root: {relative_path.as_posix()}") from exc
    return candidate


def _read_manifest_rows(manifest_path: Path) -> list[dict[str, object]]:
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CampaignManifestError(f"Unable to read campaign manifest {manifest_path}: {exc}") from exc
    rows: list[dict[str, object]] = []
    # JSON Lines is delimited by physical LF bytes.  ``str.splitlines()`` is
    # deliberately not suitable here: it also treats valid Unicode text
    # characters such as U+2028/U+2029 as record separators, which corrupts a
    # JSON value that contains them.
    lines = text.split("\n")
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            if line_number == len(lines):
                # A conventional final LF does not add a JSONL record.
                continue
            raise CampaignManifestError(f"Campaign manifest has an empty line at {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CampaignManifestError(f"Campaign manifest has invalid JSON at line {line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise CampaignManifestError(f"Campaign manifest line {line_number} must contain an object")
        rows.append(row)
    if not rows:
        raise CampaignManifestError("Campaign manifest must contain at least one item")
    return rows


def _normalise_expected_count(expected_count: int | None) -> int | None:
    if expected_count is None:
        return None
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
        raise CampaignManifestError("expected_count must be a positive integer when provided")
    return expected_count


def load_campaign_manifest_items(
    manifest_path: Path,
    audio_root: Path,
    *,
    expected_count: int | None = None,
    expected_campaign_id: str | None = None,
) -> list[CampaignItem]:
    """Load a safe manifest without requiring every LFS audio object locally.

    This split is intentional for sparse LFS shards: the complete manifest is
    ordinary Git data, while only the selected shard's audio files are hydrated
    before inference.  Call :func:`validate_campaign_item_audio` for every
    selected item before it is handed to the model.
    """
    manifest_path = Path(manifest_path)
    audio_root = Path(audio_root).resolve()
    expected_count = _normalise_expected_count(expected_count)
    if expected_campaign_id is not None:
        expected_campaign_id = _require_item_id(
            expected_campaign_id,
            field="expected_campaign_id",
        )
    if not manifest_path.is_file():
        raise CampaignManifestError(f"Campaign manifest is not a file: {manifest_path}")
    if not audio_root.is_dir():
        raise CampaignManifestError(f"Campaign audio root is not a directory: {audio_root}")

    rows = _read_manifest_rows(manifest_path)
    if expected_count is not None and len(rows) != expected_count:
        raise CampaignManifestError(
            f"Campaign manifest has {len(rows)} items, expected exactly {expected_count}"
        )

    items: list[CampaignItem] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    campaign_id: str | None = None
    for line_number, row in enumerate(rows, 1):
        item_id = _require_item_id(row.get("id"), field=f"id at manifest line {line_number}")
        if item_id in seen_ids:
            raise CampaignManifestError(f"Duplicate campaign item id: {item_id}")
        relative_text, relative_path = _safe_relative_audio_path(row.get("relative_audio_path"))
        if relative_text in seen_paths:
            raise CampaignManifestError(f"Duplicate campaign audio path: {relative_text}")
        source_bytes = _require_nonnegative_int(row.get("source_bytes"), field=f"source_bytes for {item_id}")
        source_sha256 = _require_sha256(row.get("sha256"), field=f"sha256 for {item_id}")
        title = _require_text(row.get("title"), field=f"title for {item_id}")
        artist = _require_text(row.get("artist"), field=f"artist for {item_id}")
        row_campaign_id = _require_item_id(row.get("campaign_id"), field=f"campaign_id for {item_id}")
        if campaign_id is None:
            campaign_id = row_campaign_id
        elif row_campaign_id != campaign_id:
            raise CampaignManifestError(
                f"Campaign id changed at {item_id}: {row_campaign_id!r} != {campaign_id!r}"
            )
        if expected_campaign_id is not None and row_campaign_id != expected_campaign_id:
            raise CampaignManifestError(
                f"Campaign id mismatch at {item_id}: {row_campaign_id!r} != {expected_campaign_id!r}"
            )

        audio_path = _resolve_confined_audio_path(audio_root, relative_path)
        seen_ids.add(item_id)
        seen_paths.add(relative_text)
        items.append(
            CampaignItem(
                item_id=item_id,
                relative_audio_path=relative_text,
                audio_path=audio_path,
                source_bytes=source_bytes,
                sha256=source_sha256,
                title=title,
                artist=artist,
                campaign_id=row_campaign_id,
                manifest_index=line_number,
            )
        )
    return items


def validate_campaign_item_audio(item: CampaignItem, *, verify_sha256: bool = True) -> CampaignItem:
    """Verify one hydrated campaign audio object against its manifest identity."""
    if not isinstance(item, CampaignItem):
        raise CampaignManifestError("item must be a CampaignItem")
    if not item.audio_path.is_file():
        raise CampaignManifestError(f"Campaign audio file is missing for {item.item_id}: {item.audio_path}")
    try:
        actual_bytes = item.audio_path.stat().st_size
    except OSError as exc:
        raise CampaignManifestError(
            f"Unable to stat campaign audio for {item.item_id}: {item.audio_path}"
        ) from exc
    if actual_bytes != item.source_bytes:
        raise CampaignManifestError(
            f"Campaign audio byte size mismatch for {item.item_id}: {actual_bytes} != {item.source_bytes}"
        )
    if verify_sha256:
        actual_sha256 = sha256_file(item.audio_path)
        if actual_sha256 != item.sha256:
            raise CampaignManifestError(
                f"Campaign audio digest mismatch for {item.item_id}: {actual_sha256} != {item.sha256}"
            )
    return item


def load_campaign_items(
    manifest_path: Path,
    audio_root: Path,
    *,
    expected_count: int | None = None,
    expected_campaign_id: str | None = None,
    verify_sha256: bool = True,
) -> list[CampaignItem]:
    """Load and validate a manifest whose audio files are all locally present."""
    items = load_campaign_manifest_items(
        manifest_path,
        audio_root,
        expected_count=expected_count,
        expected_campaign_id=expected_campaign_id,
    )
    return [validate_campaign_item_audio(item, verify_sha256=verify_sha256) for item in items]


def _normalise_runtime_image(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignError("runtime_image must be a non-empty string")
    image = value.strip()
    if not _IMMUTABLE_IMAGE_RE.fullmatch(image):
        raise CampaignError("runtime_image must use an immutable @sha256 digest")
    return image


def _normalise_prompt(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CampaignError("prompt must be a non-empty string")
    return value


def _normalise_max_new_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CampaignError("max_new_tokens must be a positive integer")
    return value


def _normalise_clip_seconds(value: object) -> float:
    if isinstance(value, bool):
        raise CampaignError("audio_clip_seconds must be a positive finite number")
    try:
        clip_seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise CampaignError("audio_clip_seconds must be a positive finite number") from exc
    if not math.isfinite(clip_seconds) or clip_seconds <= 0:
        raise CampaignError("audio_clip_seconds must be a positive finite number")
    return clip_seconds


def _normalise_contract_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(f"{field} must be a non-empty string")
    return value.strip()


def _model_contract_value(value: object | None, *, env_name: str, default: str, field: str) -> str:
    if value is None:
        value = os.environ.get(env_name, default)
    return _normalise_contract_text(value, field=field)


def _execution_profile_value(value: object | None) -> str:
    if value is None:
        value = os.environ.get("MUSIC_FLAMINGO_EXECUTION_PROFILE")
    return _normalise_contract_text(value, field="execution_profile")


def current_runner_code_sha256() -> str:
    """Hash the checked-out Python sources which define inference semantics.

    The runtime image pins CUDA/model dependencies, but campaign scripts are
    checked out by CNB.  Including their source bytes prevents a result from a
    previous checkout from being silently reused after runner logic changes.
    """
    digest = hashlib.sha256()
    scripts_dir = Path(__file__).resolve().parent
    for name in (
        "music_flamingo_campaign.py",
        "run_music_flamingo_batch.py",
        "run_one_music_flamingo_smoke.py",
    ):
        path = scripts_dir / name
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CampaignError(f"Unable to hash runner source {path}: {exc}") from exc
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _normalise_runner_code_sha256(value: object | None) -> str:
    if value is None:
        return current_runner_code_sha256()
    return _require_sha256(value, field="runner_code_sha256", error_type=CampaignError)


def _contract_components(
    runtime_image: object,
    prompt: object,
    max_new_tokens: object,
    audio_clip_seconds: object,
    runner_code_sha256: object | None,
    model_id: object | None,
    model_revision: object | None,
    model_dir: object | None,
    execution_profile: object | None,
) -> tuple[str, str, int, float, str, str, str, str, str]:
    return (
        _normalise_runtime_image(runtime_image),
        _normalise_prompt(prompt),
        _normalise_max_new_tokens(max_new_tokens),
        _normalise_clip_seconds(audio_clip_seconds),
        _normalise_runner_code_sha256(runner_code_sha256),
        _model_contract_value(
            model_id,
            env_name="MUSIC_FLAMINGO_MODEL",
            default=_DEFAULT_MODEL_ID,
            field="model_id",
        ),
        _model_contract_value(
            model_revision,
            env_name="MUSIC_FLAMINGO_REVISION",
            default=_DEFAULT_MODEL_REVISION,
            field="model_revision",
        ),
        _model_contract_value(
            model_dir,
            env_name="MUSIC_FLAMINGO_MODEL_DIR",
            default=_DEFAULT_MODEL_DIR,
            field="model_dir",
        ),
        _execution_profile_value(execution_profile),
    )


def _fingerprint_components(
    runtime_image: str,
    prompt: str,
    max_new_tokens: int,
    audio_clip_seconds: float,
    runner_code_sha256: str,
    model_id: str,
    model_revision: str,
    model_dir: str,
    execution_profile: str,
) -> str:
    canonical = json.dumps(
        {
            "audio_clip_seconds": audio_clip_seconds,
            "max_new_tokens": max_new_tokens,
            "model_dir": model_dir,
            "model_id": model_id,
            "model_revision": model_revision,
            "prompt": prompt,
            "runner_code_sha256": runner_code_sha256,
            "runtime_image": runtime_image,
            "execution_profile": execution_profile,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def contract_fingerprint(
    runtime_image: str,
    prompt: str,
    max_new_tokens: int,
    audio_clip_seconds: float,
    *,
    runner_code_sha256: str | None = None,
    model_id: str | None = None,
    model_revision: str | None = None,
    model_dir: str | None = None,
    execution_profile: str | None = None,
) -> str:
    """Return a stable fingerprint of all inference inputs that affect reuse."""
    components = _contract_components(
        runtime_image,
        prompt,
        max_new_tokens,
        audio_clip_seconds,
        runner_code_sha256,
        model_id,
        model_revision,
        model_dir,
        execution_profile,
    )
    return _fingerprint_components(*components)


def build_runtime_contract(
    runtime_image: str,
    prompt: str,
    max_new_tokens: int,
    audio_clip_seconds: float,
    *,
    runner_code_sha256: str | None = None,
    model_id: str | None = None,
    model_revision: str | None = None,
    model_dir: str | None = None,
    execution_profile: str | None = None,
) -> RuntimeContract:
    """Build the contract that is recorded next to each item result."""
    image, text, tokens, clip_seconds, code_digest, selected_model_id, selected_revision, selected_model_dir, profile = _contract_components(
        runtime_image,
        prompt,
        max_new_tokens,
        audio_clip_seconds,
        runner_code_sha256,
        model_id,
        model_revision,
        model_dir,
        execution_profile,
    )
    return RuntimeContract(
        runtime_image=image,
        prompt_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        max_new_tokens=tokens,
        audio_clip_seconds=clip_seconds,
        model_id=selected_model_id,
        model_revision=selected_revision,
        model_dir=selected_model_dir,
        execution_profile=profile,
        runner_code_sha256=code_digest,
        fingerprint=_fingerprint_components(
            image,
            text,
            tokens,
            clip_seconds,
            code_digest,
            selected_model_id,
            selected_revision,
            selected_model_dir,
            profile,
        ),
    )


def build_runtime_contract_from_environment(
    runtime_image: str,
    prompt: str,
    max_new_tokens: int,
    audio_clip_seconds: float,
    *,
    env: Mapping[str, str] | None = None,
    runner_code_sha256: str | None = None,
) -> RuntimeContract:
    """Build a strict contract from the campaign's declared environment."""
    values = os.environ if env is None else env
    return build_runtime_contract(
        runtime_image,
        prompt,
        max_new_tokens,
        audio_clip_seconds,
        runner_code_sha256=runner_code_sha256,
        model_id=values.get("MUSIC_FLAMINGO_MODEL", _DEFAULT_MODEL_ID),
        model_revision=values.get("MUSIC_FLAMINGO_REVISION", _DEFAULT_MODEL_REVISION),
        model_dir=values.get("MUSIC_FLAMINGO_MODEL_DIR", _DEFAULT_MODEL_DIR),
        execution_profile=values.get("MUSIC_FLAMINGO_EXECUTION_PROFILE"),
    )


def validate_execution_profile(contract: RuntimeContract, actual_profile: str) -> None:
    """Fail before a success ledger append if model execution differed from policy."""
    if not isinstance(contract, RuntimeContract):
        raise CampaignError("contract must be a RuntimeContract")
    actual = _execution_profile_value(actual_profile)
    if actual != contract.execution_profile:
        raise CampaignError(
            f"Music Flamingo execution profile mismatch: {actual!r} != {contract.execution_profile!r}"
        )


def make_campaign_ledger_record(
    item: CampaignItem,
    contract: RuntimeContract,
    *,
    status: str,
    attempt_id: str,
    **result_fields: object,
) -> dict[str, object]:
    """Create an auditable success or error record before appending it.

    Callers may add result fields such as ``output_dir`` or ``error`` but may
    not overwrite identity or contract fields.
    """
    if status not in {"success", "error"}:
        raise CampaignLedgerError(f"Unsupported campaign item status: {status!r}")
    if not isinstance(contract, RuntimeContract):
        raise CampaignLedgerError("contract must be a RuntimeContract")
    if isinstance(item.manifest_index, bool) or not isinstance(item.manifest_index, int) or item.manifest_index <= 0:
        raise CampaignLedgerError("item.manifest_index must be a positive integer")
    attempt_id = _require_item_id(attempt_id, field="attempt_id", error_type=CampaignLedgerError)
    base = {
        "schema_version": 1,
        "recorded_at_epoch_seconds": round(time.time(), 3),
        "status": status,
        "id": item.item_id,
        "manifest_index": item.manifest_index,
        "attempt_id": attempt_id,
        "relative_audio_path": item.relative_audio_path,
        "source_sha256": item.sha256,
        "source_bytes": item.source_bytes,
        **contract.ledger_fields(),
    }
    collision = set(base).intersection(result_fields)
    if collision:
        raise CampaignLedgerError(f"Campaign ledger result fields cannot overwrite: {sorted(collision)!r}")
    record = {**base, **result_fields}
    if status == "success":
        _validate_success_output(record)
    return record


def append_campaign_ledger(ledger_path: Path, record: dict[str, object]) -> None:
    """Append one complete JSONL record, flushing and syncing it before return."""
    if not isinstance(record, dict):
        raise CampaignLedgerError("Campaign ledger record must be an object")
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise CampaignLedgerError(f"Campaign ledger record is not JSON serializable: {exc}") from exc
    ledger_path = Path(ledger_path)
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_previously_existed = ledger_path.exists()
        with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # The first append also creates a directory entry.  Sync its parent so
        # a power loss cannot leave a successfully fsynced file unreachable.
        if not ledger_previously_existed:
            directory_fd = os.open(str(ledger_path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise CampaignLedgerError(f"Unable to append campaign ledger {ledger_path}: {exc}") from exc


def read_campaign_ledger(ledger_path: Path) -> list[dict[str, object]]:
    """Read valid ledger records, tolerating one damaged final JSONL line only."""
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return []
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CampaignLedgerError(f"Unable to read campaign ledger {ledger_path}: {exc}") from exc

    records: list[dict[str, object]] = []
    # JSON Lines is delimited by physical LF bytes.  Do not use
    # ``str.splitlines()``: it recognizes U+2028, U+2029, and U+0085 inside a
    # valid JSON string as line boundaries.  Music-model output can contain
    # those characters, so doing so would turn one valid ledger record into
    # several invalid fragments.
    lines = text.split("\n")
    for line_number, line in enumerate(lines, 1):
        is_final_line = line_number == len(lines)
        if not line.strip():
            if is_final_line:
                continue
            raise CampaignLedgerError(f"Campaign ledger has an empty line at {line_number}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if is_final_line:
                # A process can be interrupted after a partial append.  All
                # earlier lines have to remain parseable before reuse is safe.
                break
            raise CampaignLedgerError(f"Campaign ledger has invalid JSON at line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise CampaignLedgerError(f"Campaign ledger line {line_number} must contain an object")
        records.append(record)
    return records


def _matching_success_records(ledger_path: Path, contract: str) -> list[dict[str, object]]:
    if not isinstance(contract, str) or not contract.strip():
        raise CampaignLedgerError("contract must be a non-empty string")
    return [
        record
        for record in read_campaign_ledger(ledger_path)
        if record.get("status") == "success" and record.get("contract") == contract
    ]


def _record_item_id(record: Mapping[str, object]) -> str:
    try:
        return _require_item_id(record.get("id"), field="ledger success id", error_type=CampaignLedgerError)
    except CampaignError as exc:
        if isinstance(exc, CampaignLedgerError):
            raise
        raise CampaignLedgerError(str(exc)) from exc


def _require_positive_ledger_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CampaignLedgerError("ledger success manifest_index must be a positive integer")
    return value


def _validate_success_output(record: Mapping[str, object]) -> None:
    output_text = record.get("output_text")
    if not isinstance(output_text, str):
        raise CampaignLedgerError("ledger success output_text must be a string")
    output_sha256 = _require_sha256(
        record.get("output_text_sha256"),
        field="ledger success output_text_sha256",
        error_type=CampaignLedgerError,
    )
    actual_sha256 = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    if actual_sha256 != output_sha256:
        raise CampaignLedgerError(
            f"ledger success output_text digest mismatch: {actual_sha256} != {output_sha256}"
        )


def _validate_success_contract_fields(record: Mapping[str, object]) -> None:
    """Reject a structurally incomplete success before it can affect resume."""
    try:
        _normalise_runtime_image(record.get("runtime_image"))
        _require_sha256(
            record.get("prompt_sha256"),
            field="ledger success prompt_sha256",
            error_type=CampaignLedgerError,
        )
        _normalise_max_new_tokens(record.get("max_new_tokens"))
        _normalise_clip_seconds(record.get("audio_clip_seconds"))
        _normalise_contract_text(record.get("model_id"), field="ledger success model_id")
        _normalise_contract_text(record.get("model_revision"), field="ledger success model_revision")
        _normalise_contract_text(record.get("model_dir"), field="ledger success model_dir")
        _normalise_contract_text(record.get("execution_profile"), field="ledger success execution_profile")
        _require_sha256(
            record.get("runner_code_sha256"),
            field="ledger success runner_code_sha256",
            error_type=CampaignLedgerError,
        )
    except CampaignLedgerError:
        raise
    except CampaignError as exc:
        raise CampaignLedgerError(f"Invalid ledger success contract fields: {exc}") from exc


def _validated_success_identity(record: Mapping[str, object]) -> tuple[str, str, int, int]:
    """Validate every field that makes a success safe to report or reuse."""
    record_id = _record_item_id(record)
    record_sha256 = _require_sha256(
        record.get("source_sha256"),
        field="ledger success source_sha256",
        error_type=CampaignLedgerError,
    )
    record_bytes = _require_nonnegative_int(
        record.get("source_bytes"),
        field="ledger success source_bytes",
        error_type=CampaignLedgerError,
    )
    record_index = _require_positive_ledger_index(record.get("manifest_index"))
    _require_item_id(
        record.get("attempt_id"),
        field="ledger success attempt_id",
        error_type=CampaignLedgerError,
    )
    _validate_success_output(record)
    _validate_success_contract_fields(record)
    return record_id, record_sha256, record_bytes, record_index


def read_successful_item_ids(ledger_path: Path, contract: str) -> set[str]:
    """Return structurally valid matching-contract success IDs for reporting.

    This function cannot compare a success digest to the *current* manifest.
    Use :func:`pending_campaign_items` for a safe resume decision.
    """
    return {
        _validated_success_identity(record)[0]
        for record in _matching_success_records(ledger_path, contract)
    }


def is_reusable_success(record: Mapping[str, object], item: CampaignItem, contract: str) -> bool:
    """Return whether one ledger row exactly matches this item's identity and digest."""
    if record.get("status") != "success" or record.get("contract") != contract:
        return False
    record_id, record_sha256, record_bytes, record_index = _validated_success_identity(record)
    if isinstance(item.manifest_index, bool) or not isinstance(item.manifest_index, int) or item.manifest_index <= 0:
        raise CampaignLedgerError("item.manifest_index must be a positive integer")
    return (
        record_id == item.item_id
        and record_sha256 == item.sha256
        and record_bytes == item.source_bytes
        and record_index == item.manifest_index
    )


def pending_campaign_items(
    items: Sequence[CampaignItem],
    ledger_path: Path,
    contract: str,
) -> list[CampaignItem]:
    """Keep manifest order while dropping only exactly reusable success records."""
    success_records = _matching_success_records(ledger_path, contract)
    return [
        item
        for item in items
        if not any(is_reusable_success(record, item, contract) for record in success_records)
    ]


def campaign_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the runner should use the image-local manifest path."""
    values = os.environ if env is None else env
    return bool(str(values.get("MUSIC_FLAMINGO_INPUT_MANIFEST") or "").strip())


def campaign_input_config_from_environment(env: Mapping[str, str] | None = None) -> CampaignInputConfig:
    """Resolve image-local campaign input settings without importing model code."""
    values = os.environ if env is None else env
    manifest_text = str(values.get("MUSIC_FLAMINGO_INPUT_MANIFEST") or "").strip()
    if not manifest_text:
        raise CampaignManifestError("MUSIC_FLAMINGO_INPUT_MANIFEST must be set for campaign mode")
    manifest_path = Path(manifest_text)
    audio_root_text = str(values.get("MUSIC_FLAMINGO_INPUT_AUDIO_ROOT") or manifest_path.parent).strip()
    expected_text = str(values.get("MUSIC_FLAMINGO_EXPECTED_ITEM_COUNT") or "927").strip()
    try:
        expected_count = int(expected_text)
    except ValueError as exc:
        raise CampaignManifestError("MUSIC_FLAMINGO_EXPECTED_ITEM_COUNT must be an integer") from exc
    expected_count = _normalise_expected_count(expected_count)
    assert expected_count is not None
    expected_campaign_text = str(
        values.get("MUSIC_FLAMINGO_CAMPAIGN_ID") or values.get("MUSIC_FLAMINGO_RUN_ID") or ""
    ).strip()
    if not expected_campaign_text:
        raise CampaignManifestError(
            "MUSIC_FLAMINGO_CAMPAIGN_ID or MUSIC_FLAMINGO_RUN_ID must be set for campaign mode"
        )
    expected_campaign_id = _require_item_id(
        expected_campaign_text,
        field="MUSIC_FLAMINGO_CAMPAIGN_ID",
    )
    return CampaignInputConfig(
        manifest_path=manifest_path,
        audio_root=Path(audio_root_text),
        expected_count=expected_count,
        expected_campaign_id=expected_campaign_id,
    )


def _add_manifest_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-campaign-id")
    parser.add_argument(
        "--no-verify-sha256",
        action="store_true",
        help="validate paths and byte sizes without hashing every audio file",
    )


def _items_from_cli_args(args: argparse.Namespace) -> list[CampaignItem]:
    return load_campaign_items(
        args.manifest,
        args.audio_root,
        expected_count=args.expected_count,
        expected_campaign_id=args.expected_campaign_id,
        verify_sha256=not args.no_verify_sha256,
    )


def _campaign_summary(items: Sequence[CampaignItem]) -> dict[str, object]:
    campaign_ids = {item.campaign_id for item in items}
    assert len(campaign_ids) == 1
    return {
        "campaign_id": next(iter(campaign_ids)),
        "item_count": len(items),
        "first_item_id": items[0].item_id,
        "last_item_id": items[-1].item_id,
    }


def _read_cli_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    try:
        return args.prompt_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise CampaignError(f"Unable to read prompt file {args.prompt_file}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """Validate campaign input or report which items remain after a ledger retry."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate image-local manifest and audio")
    _add_manifest_arguments(validate_parser)

    pending_parser = subparsers.add_parser("pending", help="report manifest items not reusable from a ledger")
    _add_manifest_arguments(pending_parser)
    pending_parser.add_argument("--ledger", type=Path, required=True)
    pending_parser.add_argument("--runtime-image", required=True)
    prompt_group = pending_parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    pending_parser.add_argument("--max-new-tokens", type=int, default=2048)
    pending_parser.add_argument("--audio-clip-seconds", type=float, default=240.0)
    pending_parser.add_argument("--execution-profile", required=True)
    pending_parser.add_argument("--include-pending-ids", action="store_true")

    args = parser.parse_args(argv)
    try:
        # A ledger audit works from the ordinary-Git manifest and must not
        # force a caller to hydrate every LFS object just to count successful
        # current-contract ids.  Audio validation remains strict for `validate`.
        if args.command == "pending":
            items = load_campaign_manifest_items(
                args.manifest,
                args.audio_root,
                expected_count=args.expected_count,
                expected_campaign_id=args.expected_campaign_id,
            )
        else:
            items = _items_from_cli_args(args)
        summary = _campaign_summary(items)
        if args.command == "validate":
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0

        contract = build_runtime_contract(
            args.runtime_image,
            _read_cli_prompt(args),
            args.max_new_tokens,
            args.audio_clip_seconds,
            execution_profile=args.execution_profile,
        )
        pending = pending_campaign_items(items, args.ledger, contract.fingerprint)
        result: dict[str, object] = {
            **summary,
            "contract": contract.fingerprint,
            "reusable_success_count": len(items) - len(pending),
            "pending_count": len(pending),
        }
        if args.include_pending_ids:
            result["pending_item_ids"] = [item.item_id for item in pending]
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except CampaignError as exc:
        print(f"music_flamingo_campaign: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
