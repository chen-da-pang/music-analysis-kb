from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .errors import ValidationError


LYRIC_NORMALIZER_VERSION = "lrc-v1"
LYRIC_RECEIPT_SCHEMA_VERSION = 1
LYRIC_STATUS_PENDING = "pending"
LYRIC_STATUS_AVAILABLE = "available"
LYRIC_STATUS_INSTRUMENTAL = "instrumental"
LYRIC_STATUS_PLATFORM_UNAVAILABLE = "platform_unavailable"

LYRIC_STATUSES = frozenset(
    {
        LYRIC_STATUS_PENDING,
        LYRIC_STATUS_AVAILABLE,
        LYRIC_STATUS_INSTRUMENTAL,
        LYRIC_STATUS_PLATFORM_UNAVAILABLE,
    }
)
LYRIC_TERMINAL_STATUSES = frozenset(
    {
        LYRIC_STATUS_AVAILABLE,
        LYRIC_STATUS_INSTRUMENTAL,
        LYRIC_STATUS_PLATFORM_UNAVAILABLE,
    }
)

# Keep the expression narrow: lyric text may legitimately contain square
# brackets such as "[Chorus]". Only LRC timing prefixes are transport data.
_TIMESTAMP_PREFIX = re.compile(r"^\s*(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+\s*")
_METADATA_LINE = re.compile(
    r"^\s*\[(?:ar|al|ti|by|offset|re|ve|tool|length|au):[^\]]*\]\s*$",
    re.IGNORECASE,
)
_NON_LYRIC_PAYLOAD_MARKERS = (
    "<script",
    "<html",
    "<!doctype",
    "获取失败",
)


def normalize_lyric_text(value: object) -> str:
    """Convert LRC or plain lyric text into ordinary, readable text lines.

    This intentionally preserves repeated chorus lines and user-visible
    bracketed section labels. It removes only LRC timestamps and common LRC
    transport metadata, normalizes line endings, and drops blank lines.
    """

    if not isinstance(value, str):
        return ""
    lines: list[str] = []
    for raw_line in value.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _METADATA_LINE.fullmatch(raw_line):
            continue
        line = _TIMESTAMP_PREFIX.sub("", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def is_publishable_lyric_text(value: object) -> bool:
    """Reject known HTML/error placeholders that are not song lyrics.

    A non-empty response alone is not sufficient evidence of usable lyrics:
    some upstream Kugou paths return a script-based failure page as text. The
    markers are deliberately narrow so ordinary lyric lines remain untouched.
    """

    lyric_text = normalize_lyric_text(value)
    if not lyric_text:
        return False
    folded = lyric_text.casefold()
    return not any(marker.casefold() in folded for marker in _NON_LYRIC_PAYLOAD_MARKERS)


def lyric_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_lyric_receipts(path: str | Path) -> list[dict[str, Any]]:
    """Load append-safe worker receipts from a UTF-8 JSONL file.

    The CC worker appends one receipt per exact source-track attempt.  Repeated
    source identities are intentional: a later run can replace an earlier
    ``pending`` result with a verified terminal result.  Identity and content
    validation stays in :class:`MusicKBRepository`, where the source track can
    be checked against the writable master database.
    """

    source = Path(path).expanduser()
    if not source.is_file():
        raise ValidationError(f"Lyric receipt file does not exist: {source}")
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"Unable to read lyric receipt file: {source}") from exc
    if not raw:
        raise ValidationError("Lyric receipt file must contain at least one JSONL record")

    receipts: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Invalid lyric receipt JSONL at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(value, Mapping):
            raise ValidationError(f"Lyric receipt line {line_number} must be an object")
        receipt = dict(value)
        schema_version = receipt.get("schema_version")
        if schema_version != LYRIC_RECEIPT_SCHEMA_VERSION:
            raise ValidationError(
                f"Lyric receipt line {line_number} has unsupported schema_version "
                f"{schema_version!r}; expected {LYRIC_RECEIPT_SCHEMA_VERSION}"
            )
        for field in ("source_name", "source_track_id", "status", "evidence"):
            if field not in receipt:
                raise ValidationError(f"Lyric receipt line {line_number} is missing {field}")
        if not isinstance(receipt["evidence"], Mapping):
            raise ValidationError(f"Lyric receipt line {line_number} evidence must be an object")
        receipts.append(receipt)
    if not receipts:
        raise ValidationError("Lyric receipt file must contain at least one JSONL record")
    return receipts


def receipt_summary(receipts: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Return a compact status count for receipts without treating it as coverage."""

    counts = {status: 0 for status in sorted(LYRIC_STATUSES)}
    for receipt in receipts:
        status = str(receipt.get("status") or "").strip().lower()
        if status in counts:
            counts[status] += 1
    return counts
