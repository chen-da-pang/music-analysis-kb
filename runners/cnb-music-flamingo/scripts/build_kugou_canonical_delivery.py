#!/usr/bin/env python3
"""Build a canonical KuGou delivery manifest from a durable campaign ledger.

The campaign ledger is intentionally append-only recovery history.  This tool
does not rewrite it.  Instead it emits one deterministic, ordered delivery
record per source-manifest row.  A completed weekly campaign can be published
directly from its latest valid success records.  When all quality-rerun inputs
are supplied, the audited rerun record becomes the public canonical result for
the selected IDs and all other IDs retain their campaign result.

The resulting JSONL is the only input that downstream knowledge-base importers
should use.  It preserves the raw model output (including any lyrics) for the
later cleaning stage, but makes the replacement decision explicit and
auditable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from audit_kugou_quality_rerun import QualityRerunAuditError, audit_quality_rerun, repeated_tail
from music_flamingo_campaign import CampaignLedgerError, read_campaign_ledger
from prepare_kugou_quality_rerun import QualityRerunError, read_selection_indices


DELIVERY_SCHEMA_VERSION = 1


class CanonicalDeliveryError(ValueError):
    """Raised when a source/rerun/ledger combination is unsafe to publish."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Read JSONL with physical LF only; U+2028/U+2029 are valid text."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CanonicalDeliveryError(f"Unable to read JSONL {path}: {exc}") from exc
    rows: list[dict[str, object]] = []
    lines = text.split("\n")
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            if line_number == len(lines):
                continue
            raise CanonicalDeliveryError(f"JSONL has an empty record at line {line_number}: {path}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CanonicalDeliveryError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise CanonicalDeliveryError(f"JSONL row at {path}:{line_number} is not an object")
        rows.append(row)
    return rows


def _required_text(row: Mapping[str, object], field: str, *, context: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CanonicalDeliveryError(f"{context} has no usable {field}")
    return value.strip()


def _required_raw_text(row: Mapping[str, object], field: str, *, context: str) -> str:
    """Require text without changing bytes covered by an output digest."""

    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CanonicalDeliveryError(f"{context} has no usable {field}")
    return value


def _required_positive_int(row: Mapping[str, object], field: str, *, context: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CanonicalDeliveryError(f"{context} has invalid {field}")
    return value


def _required_nonnegative_int(row: Mapping[str, object], field: str, *, context: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CanonicalDeliveryError(f"{context} has invalid {field}")
    return value


def _validate_source_rows(
    rows: list[dict[str, object]],
    *,
    expected_count: int,
    expected_campaign_id: str,
    require_source_url: bool,
) -> list[dict[str, object]]:
    if len(rows) != expected_count:
        raise CanonicalDeliveryError(f"Source manifest count {len(rows)} != expected {expected_count}")
    seen_ids: set[str] = set()
    for manifest_index, row in enumerate(rows, 1):
        context = f"source manifest index {manifest_index}"
        item_id = _required_text(row, "id", context=context)
        if item_id in seen_ids:
            raise CanonicalDeliveryError(f"Source manifest has duplicate id: {item_id}")
        seen_ids.add(item_id)
        _required_text(row, "relative_audio_path", context=context)
        _required_text(row, "sha256", context=context)
        _required_positive_int(row, "source_bytes", context=context)
        _required_text(row, "title", context=context)
        _required_text(row, "artist", context=context)
        campaign_id = _required_text(row, "campaign_id", context=context)
        if campaign_id != expected_campaign_id:
            raise CanonicalDeliveryError(
                f"{context} campaign_id {campaign_id!r} != expected {expected_campaign_id!r}"
            )
        source_url = row.get("source_url")
        if source_url is not None or require_source_url:
            source_url = _required_text(row, "source_url", context=context)
            parsed = urlsplit(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CanonicalDeliveryError(f"{context} has an unsafe source_url")
    return rows


def _validate_output_record(
    record: Mapping[str, object],
    source_row: Mapping[str, object],
    *,
    manifest_index: int,
    context: str,
    require_manifest_index: bool = True,
) -> None:
    if record.get("status") != "success":
        raise CanonicalDeliveryError(f"{context} is not a success record")
    if record.get("id") != source_row["id"]:
        raise CanonicalDeliveryError(f"{context} id does not match source manifest")
    if require_manifest_index and record.get("manifest_index") != manifest_index:
        raise CanonicalDeliveryError(f"{context} manifest_index does not match source manifest")
    if record.get("source_sha256") != source_row["sha256"]:
        raise CanonicalDeliveryError(f"{context} source_sha256 does not match source manifest")
    if record.get("source_bytes") != source_row["source_bytes"]:
        raise CanonicalDeliveryError(f"{context} source_bytes does not match source manifest")
    _required_text(record, "attempt_id", context=context)
    _required_text(record, "contract", context=context)
    output_text = _required_raw_text(record, "output_text", context=context)
    digest = _required_text(record, "output_text_sha256", context=context)
    if _sha256_bytes(output_text.encode("utf-8")) != digest:
        raise CanonicalDeliveryError(f"{context} output_text_sha256 mismatch")
    max_tokens = _required_positive_int(record, "max_new_tokens", context=context)
    generated = _required_nonnegative_int(record, "generated_token_count", context=context)
    if generated > max_tokens:
        raise CanonicalDeliveryError(f"{context} generated_token_count exceeds max_new_tokens")


def _matches_source_identity(
    record: Mapping[str, object],
    source_row: Mapping[str, object],
    *,
    manifest_index: int,
    require_manifest_index: bool = True,
) -> bool:
    """Whether an append-only historical row is reusable for this source row."""

    return (
        record.get("id") == source_row.get("id")
        and record.get("source_sha256") == source_row.get("sha256")
        and record.get("source_bytes") == source_row.get("source_bytes")
        and record.get("relative_audio_path") == source_row.get("relative_audio_path")
        and (not require_manifest_index or record.get("manifest_index") == manifest_index)
    )


def _latest_valid_campaign_records(
    source_rows: Sequence[dict[str, object]], campaign_ledger: Path
) -> dict[str, dict[str, object]]:
    by_id = {str(row["id"]): (index, row) for index, row in enumerate(source_rows, 1)}
    latest: dict[str, dict[str, object]] = {}
    try:
        records = read_campaign_ledger(campaign_ledger)
    except CampaignLedgerError as exc:
        raise CanonicalDeliveryError(str(exc)) from exc
    for record in records:
        item_id = record.get("id")
        if not isinstance(item_id, str) or item_id not in by_id:
            continue
        manifest_index, source_row = by_id[item_id]
        if record.get("status") != "success":
            continue
        # The ledger is recovery history.  A prior manifest repair can leave a
        # valid-but-old record with a stale index or digest; it must not block
        # selection of the later exact source match.
        # Campaign jobs may be executed as shards.  Their durable records keep
        # the shard-local manifest coordinate, while the source manifest used
        # for publication is the global campaign coordinate.  The immutable
        # source identity is the safe join key here; the delivery writer below
        # assigns the global 1..N index freshly.
        if not _matches_source_identity(
            record, source_row, manifest_index=manifest_index, require_manifest_index=False
        ):
            continue
        _validate_output_record(
            record,
            source_row,
            manifest_index=manifest_index,
            context=f"campaign ledger record for {item_id}",
            require_manifest_index=False,
        )
        latest[item_id] = dict(record)
    missing = [str(row["id"]) for row in source_rows if str(row["id"]) not in latest]
    if missing:
        preview = ", ".join(missing[:12])
        suffix = "..." if len(missing) > 12 else ""
        raise CanonicalDeliveryError(f"Campaign ledger has no valid success for {len(missing)} source items: {preview}{suffix}")
    return latest


def _audited_quality_rerun_records(
    *,
    source_manifest: Path,
    selection_file: Path,
    quality_ledger: Path,
    attempt_id: str,
    source_rows: Sequence[dict[str, object]],
) -> tuple[dict[str, dict[str, object]], dict[str, object], list[int]]:
    try:
        audit = audit_quality_rerun(
            source_manifest=source_manifest,
            selection_file=selection_file,
            ledger_path=quality_ledger,
            attempt_id=attempt_id,
        )
        indexes = read_selection_indices(selection_file, source_count=len(source_rows))
    except (QualityRerunAuditError, QualityRerunError) as exc:
        raise CanonicalDeliveryError(str(exc)) from exc
    if audit.get("status") != "pass":
        raise CanonicalDeliveryError(f"Quality rerun audit did not pass: {audit.get('failures')}")
    expected_ids = {str(source_rows[index - 1]["id"]) for index in indexes}
    latest: dict[str, dict[str, object]] = {}
    try:
        records = read_campaign_ledger(quality_ledger)
    except CampaignLedgerError as exc:
        raise CanonicalDeliveryError(str(exc)) from exc
    source_by_id = {str(row["id"]): (index, row) for index, row in enumerate(source_rows, 1)}
    for record in records:
        if record.get("attempt_id") != attempt_id:
            continue
        # The append-only ledger can contain an earlier failed retry with the
        # same attempt id (for example a full-precision OOM before a 4-bit
        # success).  The audit has already selected the latest valid record;
        # historical error rows must not abort promotion here.
        if record.get("status") != "success":
            continue
        item_id = record.get("id")
        if not isinstance(item_id, str) or item_id not in expected_ids:
            continue
        manifest_index, source_row = source_by_id[item_id]
        # The quality rerun deliberately uses a compact 1..12 manifest.  Its
        # ledger index is therefore not the original 927-row source index;
        # bind it by immutable ID/path/digest/byte identity instead.
        if not _matches_source_identity(
            record, source_row, manifest_index=manifest_index, require_manifest_index=False
        ):
            continue
        _validate_output_record(
            record,
            source_row,
            manifest_index=manifest_index,
            context=f"quality rerun record for {item_id}",
            require_manifest_index=False,
        )
        latest[item_id] = dict(record)
    if set(latest) != expected_ids:
        raise CanonicalDeliveryError("Quality rerun audit passed but selected record set changed during publication")
    return latest, audit, indexes


def _audit_direct_campaign_records(
    source_rows: Sequence[dict[str, object]],
    records: Mapping[str, dict[str, object]],
) -> list[str]:
    """Apply the non-rerun quality gate to a normal campaign.

    Ordinary campaign runs intentionally do not carry the anti-repetition
    controls required by the isolated 12-item recovery route.  They still
    must not publish a token-capped or obviously looping model response.
    Short or oddly punctuated text is retained as a warning for later review.
    """

    warnings: list[str] = []
    for source_row in source_rows:
        item_id = str(source_row["id"])
        record = records[item_id]
        max_tokens = _required_positive_int(record, "max_new_tokens", context=f"campaign record {item_id}")
        generated_tokens = _required_nonnegative_int(
            record, "generated_token_count", context=f"campaign record {item_id}"
        )
        if generated_tokens >= max_tokens:
            raise CanonicalDeliveryError(
                f"campaign record {item_id} reached token cap ({generated_tokens}/{max_tokens})"
            )
        output_text = _required_raw_text(record, "output_text", context=f"campaign record {item_id}")
        tail = repeated_tail(output_text)
        if tail is not None:
            raise CanonicalDeliveryError(
                f"campaign record {item_id} has a repeated terminal tail "
                f"({tail['repeat_count']}x {tail['unit_characters']}-character unit)"
            )
        if len(output_text.strip()) < 400:
            warnings.append(f"{item_id}: short output_text (<400 characters)")
    return warnings


def _delivery_entry(
    source_row: Mapping[str, object],
    *,
    manifest_index: int,
    canonical_record: Mapping[str, object],
    canonical_source: str,
    superseded_record: Mapping[str, object] | None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "campaign_id": source_row["campaign_id"],
        "id": source_row["id"],
        "manifest_index": manifest_index,
        "relative_audio_path": source_row["relative_audio_path"],
        "source_sha256": source_row["sha256"],
        "source_bytes": source_row["source_bytes"],
        "title": source_row["title"],
        "artist": source_row["artist"],
        "canonical_source": canonical_source,
        "attempt_id": canonical_record["attempt_id"],
        "contract": canonical_record["contract"],
        "generated_token_count": canonical_record["generated_token_count"],
        "max_new_tokens": canonical_record["max_new_tokens"],
        "output_text": canonical_record["output_text"],
        "output_text_sha256": canonical_record["output_text_sha256"],
    }
    if source_row.get("source_url"):
        entry["source_url"] = source_row["source_url"]
    for key in (
        "runtime_image",
        "model_id",
        "model_revision",
        "model_dir",
        "execution_profile",
        "audio_clip_seconds",
        "prompt_sha256",
        "runner_code_sha256",
        "was_truncated",
        "generation_controls",
    ):
        if key in canonical_record:
            entry[key] = canonical_record[key]
    if superseded_record is not None:
        entry["superseded_campaign_attempt_id"] = superseded_record["attempt_id"]
        entry["superseded_campaign_contract"] = superseded_record["contract"]
        entry["superseded_output_text_sha256"] = superseded_record["output_text_sha256"]
    return entry


def build_canonical_delivery(
    *,
    source_manifest: Path,
    campaign_ledger: Path,
    output_manifest: Path,
    output_state: Path,
    expected_count: int,
    expected_campaign_id: str,
    quality_ledger: Path | None = None,
    selection_file: Path | None = None,
    quality_attempt_id: str | None = None,
    require_source_url: bool = False,
) -> dict[str, object]:
    """Produce a deterministic, canonical-only delivery manifest and state."""

    source_rows = _validate_source_rows(
        _read_jsonl(source_manifest),
        expected_count=expected_count,
        expected_campaign_id=expected_campaign_id,
        require_source_url=require_source_url,
    )
    campaign_records = _latest_valid_campaign_records(source_rows, campaign_ledger)
    quality_inputs = (quality_ledger, selection_file, quality_attempt_id)
    if any(value is not None for value in quality_inputs) and not all(
        value is not None for value in quality_inputs
    ):
        raise CanonicalDeliveryError(
            "quality_ledger, selection_file, and quality_attempt_id must be supplied together"
        )
    quality_records: dict[str, dict[str, object]] = {}
    audit: dict[str, object] | None = None
    selected_indexes: list[int] = []
    direct_quality_warnings: list[str] = []
    if not any(value is not None for value in quality_inputs):
        direct_quality_warnings = _audit_direct_campaign_records(source_rows, campaign_records)
    if all(value is not None for value in quality_inputs):
        assert quality_ledger is not None
        assert selection_file is not None
        assert quality_attempt_id is not None
        quality_records, audit, selected_indexes = _audited_quality_rerun_records(
            source_manifest=source_manifest,
            selection_file=selection_file,
            quality_ledger=quality_ledger,
            attempt_id=quality_attempt_id,
            source_rows=source_rows,
        )

    delivery_entries: list[dict[str, object]] = []
    for manifest_index, source_row in enumerate(source_rows, 1):
        item_id = str(source_row["id"])
        campaign_record = campaign_records[item_id]
        if item_id in quality_records:
            delivery_entries.append(
                _delivery_entry(
                    source_row,
                    manifest_index=manifest_index,
                    canonical_record=quality_records[item_id],
                    canonical_source="quality_rerun",
                    superseded_record=campaign_record,
                )
            )
        else:
            delivery_entries.append(
                _delivery_entry(
                    source_row,
                    manifest_index=manifest_index,
                    canonical_record=campaign_record,
                    canonical_source="campaign",
                    superseded_record=None,
                )
            )

    payload = b"".join(
        json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        for entry in delivery_entries
    )
    _atomic_write(output_manifest, payload)
    state = {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "campaign_id": expected_campaign_id,
        "delivery_manifest": Path(output_manifest).name,
        "delivery_manifest_sha256": _sha256_bytes(payload),
        "delivery_item_count": len(delivery_entries),
        "campaign_source_count": len(delivery_entries) - len(quality_records),
        "quality_rerun_source_count": len(quality_records),
        "quality_rerun_attempt_id": quality_attempt_id,
        "quality_rerun_selected_manifest_indexes": selected_indexes,
        "source_url_required": require_source_url,
        "source_url_count": sum(1 for row in source_rows if row.get("source_url")),
        "direct_quality_gate": {
            "status": "pass",
            "warnings": direct_quality_warnings,
        },
        "source_manifest_sha256": sha256_file(source_manifest),
        "campaign_ledger_sha256": sha256_file(campaign_ledger),
    }
    if quality_ledger is not None and audit is not None:
        state["quality_ledger_sha256"] = sha256_file(quality_ledger)
        state["quality_rerun_audit"] = {
            "status": audit["status"],
            "expected_item_count": audit["expected_item_count"],
            "observed_item_count": audit["observed_item_count"],
            "failures": audit["failures"],
            "warnings": audit["warnings"],
        }
    _atomic_write(output_state, (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return {
        "status": "pass",
        "output_manifest": str(output_manifest),
        "output_state": str(output_state),
        **state,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--campaign-ledger", type=Path, required=True)
    parser.add_argument("--quality-ledger", type=Path)
    parser.add_argument("--selection-file", type=Path)
    parser.add_argument("--quality-attempt-id")
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--output-state", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--expected-campaign-id", required=True)
    parser.add_argument(
        "--require-source-url",
        action="store_true",
        help="Require one valid HTTP(S) source_url on every source manifest row",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = build_canonical_delivery(
            source_manifest=args.source_manifest,
            campaign_ledger=args.campaign_ledger,
            quality_ledger=args.quality_ledger,
            selection_file=args.selection_file,
            quality_attempt_id=args.quality_attempt_id,
            output_manifest=args.output_manifest,
            output_state=args.output_state,
            expected_count=args.expected_count,
            expected_campaign_id=args.expected_campaign_id,
            require_source_url=args.require_source_url,
        )
    except (CanonicalDeliveryError, OSError) as exc:
        print(f"build_kugou_canonical_delivery: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
