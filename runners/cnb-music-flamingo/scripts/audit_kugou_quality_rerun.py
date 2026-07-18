#!/usr/bin/env python3
"""Audit one isolated KuGou quality-rerun attempt before publication.

This intentionally treats the durable campaign ledger as the source of truth.
It does not clean lyric leakage: the current recovery policy is to preserve raw
model output and only reject transport/integrity/token-cap/repetition failures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from music_flamingo_campaign import CampaignLedgerError, read_campaign_ledger
from prepare_kugou_quality_rerun import QualityRerunError, read_selection_indices


_TERMINAL_ENDING = re.compile(r"[。！？.!?…][\]\)）】》”’\"']*$")


class QualityRerunAuditError(ValueError):
    """Raised when the audit inputs are not safe to evaluate."""


def _read_jsonl_rows(path: Path) -> list[dict[str, object]]:
    """Read JSONL using only LF as the record delimiter.

    U+2028/U+2029 are valid text inside an output or metadata value and must
    not become synthetic JSONL record boundaries.
    """

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise QualityRerunAuditError(f"Unable to read JSONL {path}: {exc}") from exc
    rows: list[dict[str, object]] = []
    lines = text.split("\n")
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            if line_number == len(lines):
                continue
            raise QualityRerunAuditError(f"JSONL has an empty record at line {line_number}: {path}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QualityRerunAuditError(f"JSONL has invalid JSON at line {line_number}: {path}: {exc}") from exc
        if not isinstance(row, dict):
            raise QualityRerunAuditError(f"JSONL line {line_number} is not an object: {path}")
        rows.append(row)
    return rows


def selected_source_items(source_manifest: Path, selection_file: Path) -> list[dict[str, object]]:
    rows = _read_jsonl_rows(source_manifest)
    try:
        indices = read_selection_indices(selection_file, source_count=len(rows))
    except QualityRerunError as exc:
        raise QualityRerunAuditError(str(exc)) from exc
    selected = [rows[index - 1] for index in indices]
    for source_index, row in zip(indices, selected, strict=True):
        item_id = row.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise QualityRerunAuditError(f"Source manifest index {source_index} has no usable id")
    return selected


def repeated_tail(text: str) -> dict[str, object] | None:
    """Return an obvious contiguous triple-repeat at the end, if one exists.

    This is deliberately conservative.  Repeated choruses elsewhere in a
    music description are allowed; only a long exact loop at the terminal tail
    is a quality-rerun failure.
    """

    compact = re.sub(r"\s+", " ", text.strip())
    if len(compact) < 60:
        return None
    # Search the last 1,800 characters.  The expected degeneration repeats
    # much longer spans, while a 20-character floor avoids incidental phrases.
    tail = compact[-1800:]
    max_width = min(600, len(tail) // 3)
    for width in range(max_width, 19, -1):
        unit = tail[-width:]
        if not re.search(r"[\w\u4e00-\u9fff]", unit):
            continue
        if tail.endswith(unit * 3):
            return {"repeat_count": 3, "unit_characters": width, "preview": unit[:160]}
    return None


def _record_issue(record: dict[str, object]) -> tuple[list[str], list[str]]:
    """Return (fatal errors, nonfatal warnings) for one expected success."""

    errors: list[str] = []
    warnings: list[str] = []
    if record.get("status") != "success":
        errors.append(f"status={record.get('status')!r}")
        return errors, warnings

    text = record.get("output_text")
    if not isinstance(text, str) or not text.strip():
        errors.append("missing output_text")
        return errors, warnings
    digest = record.get("output_text_sha256")
    actual_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != actual_digest:
        errors.append("output_text_sha256 mismatch")

    max_new_tokens = record.get("max_new_tokens")
    generated_tokens = record.get("generated_token_count")
    if not isinstance(max_new_tokens, int) or max_new_tokens <= 0:
        errors.append("missing or invalid max_new_tokens")
    if not isinstance(generated_tokens, int) or generated_tokens < 0:
        errors.append("missing or invalid generated_token_count")
    elif isinstance(max_new_tokens, int) and generated_tokens >= max_new_tokens:
        errors.append(f"token cap reached ({generated_tokens}/{max_new_tokens})")

    controls = record.get("generation_controls")
    if not isinstance(controls, dict):
        errors.append("missing generation_controls")
    else:
        if controls.get("repetition_penalty") != 1.08:
            errors.append("repetition_penalty is not 1.08")
        if controls.get("no_repeat_ngram_size") != 4:
            errors.append("no_repeat_ngram_size is not 4")

    tail = repeated_tail(text)
    if tail is not None:
        errors.append(
            f"repeated terminal tail ({tail['repeat_count']}x {tail['unit_characters']}-character unit)"
        )
    if len(text.strip()) < 400:
        warnings.append("short output_text (<400 characters)")
    if not _TERMINAL_ENDING.search(text.strip()):
        warnings.append("output has no clear terminal punctuation")
    return errors, warnings


def audit_quality_rerun(
    *,
    source_manifest: Path,
    selection_file: Path,
    ledger_path: Path,
    attempt_id: str,
) -> dict[str, object]:
    attempt_id = str(attempt_id).strip()
    if not attempt_id:
        raise QualityRerunAuditError("attempt_id must not be empty")
    selected = selected_source_items(source_manifest, selection_file)
    try:
        records = read_campaign_ledger(ledger_path)
    except CampaignLedgerError as exc:
        raise QualityRerunAuditError(str(exc)) from exc

    expected_ids = [str(row["id"]) for row in selected]
    latest_by_id: dict[str, dict[str, object]] = {}
    unexpected_ids: set[str] = set()
    expected_id_set = set(expected_ids)
    for record in records:
        if record.get("attempt_id") != attempt_id:
            continue
        item_id = record.get("id")
        if not isinstance(item_id, str):
            unexpected_ids.add(repr(item_id))
            continue
        if item_id not in expected_id_set:
            unexpected_ids.add(item_id)
            continue
        latest_by_id[item_id] = record

    item_reports: list[dict[str, object]] = []
    failures: list[str] = []
    warnings: list[str] = []
    for source_item in selected:
        item_id = str(source_item["id"])
        record = latest_by_id.get(item_id)
        if record is None:
            errors = ["missing ledger record for attempt"]
            item_warnings: list[str] = []
        else:
            errors, item_warnings = _record_issue(record)
        if errors:
            failures.append(f"{item_id}: " + "; ".join(errors))
        warnings.extend(f"{item_id}: {warning}" for warning in item_warnings)
        item_reports.append(
            {
                "id": item_id,
                "title": source_item.get("title", ""),
                "artist": source_item.get("artist", ""),
                "status": record.get("status") if record else "missing",
                "generated_token_count": record.get("generated_token_count") if record else None,
                "max_new_tokens": record.get("max_new_tokens") if record else None,
                "output_chars": len(record.get("output_text", "")) if isinstance(record, dict) and isinstance(record.get("output_text"), str) else 0,
                "errors": errors,
                "warnings": item_warnings,
            }
        )
    if unexpected_ids:
        failures.append("attempt wrote unexpected item ids: " + ", ".join(sorted(unexpected_ids)))

    return {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "ledger_path": str(ledger_path),
        "source_manifest": str(source_manifest),
        "selection_file": str(selection_file),
        "expected_item_count": len(expected_ids),
        "observed_item_count": len(latest_by_id),
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "warnings": warnings,
        "items": item_reports,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--selection-file", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--output", type=Path, help="Optional JSON audit report path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = audit_quality_rerun(
            source_manifest=args.source_manifest,
            selection_file=args.selection_file,
            ledger_path=args.ledger,
            attempt_id=args.attempt_id,
        )
    except (QualityRerunAuditError, OSError) as exc:
        print(f"audit_kugou_quality_rerun: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
