"""Strict adapter for canonical KuGou/Music Flamingo campaign deliveries.

The CNB campaign ledger is intentionally not treated as a generic JSON import:
it is a line-oriented, hash-addressed delivery contract.  Keeping that parser
separate prevents a malformed export from quietly becoming a canonical record.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .errors import ValidationError
from .normalization import normalized
from .tagging import extract_music_flamingo_metadata


REQUIRED_FIELDS = (
    "schema_version",
    "campaign_id",
    "id",
    "manifest_index",
    "title",
    "artist",
    "relative_audio_path",
    "source_sha256",
    "source_bytes",
    "output_text",
    "output_text_sha256",
    "generated_token_count",
    "max_new_tokens",
    "contract",
    "attempt_id",
    "canonical_source",
)
_DELIVERY_CORE_FIELDS = frozenset((*REQUIRED_FIELDS, "provenance"))
DELIVERY_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CampaignDeliveryEntry:
    """One validated physical line from a canonical delivery manifest."""

    line_number: int
    delivery_schema_version: int
    campaign_id: str
    delivery_id: str
    manifest_index: int
    title: str
    artist: str
    relative_audio_path: str
    source_sha256: str
    source_bytes: int
    output_text: str
    output_text_sha256: str
    generated_token_count: int
    max_new_tokens: int
    contract: str
    attempt_id: str
    canonical_source: str
    provenance_json: str | None
    source_url: str | None


def load_campaign_delivery_file(
    path: str | Path, *, expected_count: int | None = None
) -> list[CampaignDeliveryEntry]:
    """Read a strict LF JSONL delivery file and verify every entry before writes.

    Unlike the generic importer this accepts only JSONL, requires a final LF,
    rejects blank physical lines and does not use ``splitlines()``.  The latter
    is important because Music Flamingo text may legally contain U+2028/U+2029
    inside a JSON string; those are text characters, not record separators.
    """

    source = Path(path)
    if not source.is_file():
        raise ValidationError(f"Campaign delivery input does not exist: {source}")
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise ValidationError(f"Unable to read campaign delivery input: {source}") from exc
    if not raw_bytes:
        raise ValidationError("Campaign delivery JSONL must contain at least one entry")
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raise ValidationError("Campaign delivery JSONL must be UTF-8 without a BOM")
    if b"\r" in raw_bytes:
        raise ValidationError("Campaign delivery JSONL must use LF (\\n) line endings only; CR/CRLF is not allowed")
    if not raw_bytes.endswith(b"\n"):
        raise ValidationError("Campaign delivery JSONL must end with one LF (\\n)")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Campaign delivery JSONL must be valid UTF-8") from exc

    entries: list[CampaignDeliveryEntry] = []
    # split("\n") deliberately preserves U+2028/U+2029 inside JSON strings.
    physical_lines = raw.split("\n")
    assert physical_lines[-1] == ""
    for line_number, line in enumerate(physical_lines[:-1], 1):
        if not line:
            raise ValidationError(f"Campaign delivery JSONL has a blank record at line {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Invalid campaign delivery JSONL at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(value, Mapping):
            raise ValidationError(f"Campaign delivery line {line_number} must be a JSON object")
        entries.append(_parse_entry(value, line_number=line_number))

    _validate_collection(entries, expected_count=expected_count)
    return entries


def group_campaign_delivery(
    entries: Sequence[CampaignDeliveryEntry],
) -> list[tuple[CampaignDeliveryEntry, ...]]:
    """Group source aliases by identical audio while retaining every source row.

    A KuGou campaign can contain several source IDs/paths for the exact same
    audio. They must become one recording and one canonical analysis, not a
    false duplicate or a sequence of competing canonicals. The delivery must
    give those aliases the same model output and source byte count.
    """

    grouped: dict[str, list[CampaignDeliveryEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.source_sha256, []).append(entry)
    result: list[tuple[CampaignDeliveryEntry, ...]] = []
    for source_sha256, group in grouped.items():
        ordered = tuple(sorted(group, key=lambda entry: (entry.manifest_index, entry.line_number)))
        first = ordered[0]
        if any(entry.output_text_sha256 != first.output_text_sha256 for entry in ordered[1:]):
            raise ValidationError(
                "Campaign delivery source aliases must have identical output_text_sha256: "
                f"{source_sha256}",
                details={
                    "source_sha256": source_sha256,
                    "delivery_ids": [entry.delivery_id for entry in ordered],
                },
            )
        if any(entry.output_text != first.output_text for entry in ordered[1:]):
            raise ValidationError(
                "Campaign delivery source aliases must have byte-identical output_text: "
                f"{source_sha256}"
            )
        if any(entry.source_bytes != first.source_bytes for entry in ordered[1:]):
            raise ValidationError(
                "Campaign delivery source aliases must have identical source_bytes: "
                f"{source_sha256}"
            )
        if any(entry.generated_token_count != first.generated_token_count for entry in ordered[1:]):
            raise ValidationError(
                "Campaign delivery source aliases must have identical generated_token_count: "
                f"{source_sha256}"
            )
        result.append(ordered)
    return sorted(result, key=lambda group: (group[0].manifest_index, group[0].line_number))


def to_import_payload(entries: Sequence[CampaignDeliveryEntry]) -> dict[str, Any]:
    """Map one validated source-audio group into the canonical importer."""

    if not entries:
        raise ValidationError("Campaign delivery source-audio group cannot be empty")
    group = tuple(sorted(entries, key=lambda entry: (entry.manifest_index, entry.line_number)))
    entry = group[0]
    tags, numeric_features = extract_music_flamingo_metadata(entry.output_text)
    title_aliases = list(
        dict.fromkeys(candidate.title for candidate in group if candidate.title != entry.title)
    )
    artists: list[dict[str, str]] = []
    seen_artists: set[str] = set()
    for candidate in group:
        key = normalized(candidate.artist)
        if key in seen_artists:
            continue
        seen_artists.add(key)
        artists.append(
            {
                "name": candidate.artist,
                "role": "primary" if not artists else "source_alias",
            }
        )

    return {
        # Full source content hash is a stable recording identity.  The KuGou
        # delivery ID is preserved separately as the source track ID/provenance.
        "recording": {
            "id": f"rec_kugou_{entry.source_sha256}",
            "title": entry.title,
            "audio_sha256": entry.source_sha256,
        },
        "artists": artists,
        "title_aliases": title_aliases,
        "analysis": {
            "raw_text": entry.output_text,
            "summary": "",
            "model_version": "music-flamingo",
            "prompt_version": entry.contract,
            "generated_token_count": entry.generated_token_count,
            "quality_state": "passed",
        },
        "tags": tags,
        "numeric_features": numeric_features,
        "source_tracks": [
            {
                "source": "kugou",
                "source_track_id": candidate.delivery_id,
                "source_title": candidate.title,
                "source_artist_credit": candidate.artist,
                **({"source_url": candidate.source_url} if candidate.source_url else {}),
            }
            for candidate in group
        ],
        "canonical": True,
    }


def _parse_entry(value: Mapping[str, Any], *, line_number: int) -> CampaignDeliveryEntry:
    missing = [field for field in REQUIRED_FIELDS if field not in value]
    if missing:
        raise ValidationError(
            f"Campaign delivery line {line_number} is missing required fields: {', '.join(missing)}"
        )
    _reject_feigua_context(value, path=f"line {line_number}")

    delivery_schema_version = _integer(
        value["schema_version"], "schema_version", line_number=line_number, minimum=1
    )
    if delivery_schema_version != DELIVERY_SCHEMA_VERSION:
        raise ValidationError(
            f"Campaign delivery line {line_number} has unsupported schema_version "
            f"{delivery_schema_version}; expected {DELIVERY_SCHEMA_VERSION}"
        )
    campaign_id = _required_text(value["campaign_id"], "campaign_id", line_number=line_number)
    delivery_id = _required_text(value["id"], "id", line_number=line_number)
    manifest_index = _integer(value["manifest_index"], "manifest_index", line_number=line_number, minimum=0)
    title = _required_text(value["title"], "title", line_number=line_number)
    artist = _required_text(value["artist"], "artist", line_number=line_number)
    relative_audio_path = _relative_audio_path(value["relative_audio_path"], line_number=line_number)
    source_sha256 = _sha256(value["source_sha256"], "source_sha256", line_number=line_number)
    source_bytes = _integer(value["source_bytes"], "source_bytes", line_number=line_number, minimum=1)
    output_text = _nonempty_raw_text(value["output_text"], "output_text", line_number=line_number)
    output_text_sha256 = _sha256(
        value["output_text_sha256"], "output_text_sha256", line_number=line_number
    )
    actual_output_sha256 = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    if actual_output_sha256 != output_text_sha256:
        raise ValidationError(
            f"Campaign delivery line {line_number} output_text_sha256 does not match UTF-8 output_text",
            details={"expected": output_text_sha256, "actual": actual_output_sha256},
        )
    generated_token_count = _integer(
        value["generated_token_count"], "generated_token_count", line_number=line_number, minimum=0
    )
    max_new_tokens = _integer(value["max_new_tokens"], "max_new_tokens", line_number=line_number, minimum=1)
    if generated_token_count > max_new_tokens:
        raise ValidationError(
            f"Campaign delivery line {line_number} generated_token_count exceeds max_new_tokens"
        )
    contract = _required_text(value["contract"], "contract", line_number=line_number)
    attempt_id = _required_text(value["attempt_id"], "attempt_id", line_number=line_number)
    canonical_source = _required_text(value["canonical_source"], "canonical_source", line_number=line_number)
    provenance_json = _delivery_provenance(value, line_number=line_number)
    source_url = _source_url(value.get("source_url"), line_number=line_number)
    return CampaignDeliveryEntry(
        line_number=line_number,
        delivery_schema_version=delivery_schema_version,
        campaign_id=campaign_id,
        delivery_id=delivery_id,
        manifest_index=manifest_index,
        title=title,
        artist=artist,
        relative_audio_path=relative_audio_path,
        source_sha256=source_sha256,
        source_bytes=source_bytes,
        output_text=output_text,
        output_text_sha256=output_text_sha256,
        generated_token_count=generated_token_count,
        max_new_tokens=max_new_tokens,
        contract=contract,
        attempt_id=attempt_id,
        canonical_source=canonical_source,
        provenance_json=provenance_json,
        source_url=source_url,
    )


def _validate_collection(entries: Sequence[CampaignDeliveryEntry], *, expected_count: int | None) -> None:
    if not entries:
        raise ValidationError("Campaign delivery JSONL must contain at least one entry")
    if expected_count is not None:
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 1:
            raise ValidationError("expected_count must be a positive integer")
        if len(entries) != expected_count:
            raise ValidationError(
                f"Campaign delivery has {len(entries)} entries; expected {expected_count}",
                details={"actual_count": len(entries), "expected_count": expected_count},
            )
    _assert_unique(entries, key=lambda entry: entry.delivery_id, field="id")
    _assert_unique(entries, key=lambda entry: entry.manifest_index, field="manifest_index")
    # A SHA-256 identifies the exact source audio.  Multiple entries with the
    # same content would violate the one-public-analysis-per-recording rule.
    _assert_unique(entries, key=lambda entry: entry.relative_audio_path, field="relative_audio_path")


def _assert_unique(
    entries: Sequence[CampaignDeliveryEntry], *, key: Any, field: str
) -> None:
    seen: dict[Any, int] = {}
    for entry in entries:
        value = key(entry)
        prior_line = seen.get(value)
        if prior_line is not None:
            raise ValidationError(
                f"Campaign delivery has duplicate {field!r} at lines {prior_line} and {entry.line_number}",
                details={"field": field, "first_line": prior_line, "duplicate_line": entry.line_number},
            )
        seen[value] = entry.line_number


def _required_text(value: object, field: str, *, line_number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Campaign delivery line {line_number} {field} must be a non-empty string")
    return value.strip()


def _nonempty_raw_text(value: object, field: str, *, line_number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Campaign delivery line {line_number} {field} must be a non-empty string")
    # Deliberately do not strip: the delivery hash covers the exact UTF-8
    # model output, including any intentional leading/trailing whitespace.
    return value


def _integer(value: object, field: str, *, line_number: int, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        comparator = "non-negative" if minimum == 0 else f">= {minimum}"
        raise ValidationError(
            f"Campaign delivery line {line_number} {field} must be an integer {comparator}"
        )
    return value


def _sha256(value: object, field: str, *, line_number: int) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError(
            f"Campaign delivery line {line_number} {field} must be a lowercase 64-character SHA-256"
        )
    return value


def _relative_audio_path(value: object, *, line_number: int) -> str:
    path = _required_text(value, "relative_audio_path", line_number=line_number)
    if "\\" in path:
        raise ValidationError(
            f"Campaign delivery line {line_number} relative_audio_path must use POSIX '/' separators"
        )
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValidationError(
            f"Campaign delivery line {line_number} relative_audio_path must be a safe relative path"
        )
    return path


def _source_url(value: object, *, line_number: int) -> str | None:
    if value is None or not str(value).strip():
        return None
    url = str(value).strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(
            f"Campaign delivery line {line_number} source_url must be an absolute http(s) URL"
        )
    return url


def _delivery_provenance(value: Mapping[str, Any], *, line_number: int) -> str | None:
    """Retain optional wrapped provenance and all producer-defined fields.

    The published CNB delivery places runner/model evidence at the top level,
    while some synthetic or future producers use an explicit ``provenance``
    wrapper.  Core contract fields have normalized relational columns; every
    other producer field belongs in the immutable JSON evidence rather than
    being silently dropped.
    """

    wrapped = value.get("provenance")
    producer_fields = {
        key: child for key, child in value.items() if key not in _DELIVERY_CORE_FIELDS
    }
    if wrapped is None:
        provenance: object = producer_fields or None
    elif not producer_fields:
        # Preserve the original public shape for explicit-only provenance.
        provenance = wrapped
    else:
        provenance = {
            "declared_provenance": wrapped,
            "producer_fields": producer_fields,
        }
    return _canonicalize_provenance(provenance, line_number=line_number)


def _canonicalize_provenance(value: object, *, line_number: int) -> str | None:
    if value is None:
        return None
    try:
        # Canonical JSON makes the immutable database comparison independent of
        # field ordering in the producer's JSONL serializer.
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Campaign delivery line {line_number} provenance must be JSON-compatible") from exc


def _reject_feigua_context(value: object, *, path: str) -> None:
    """Reject Feigua names/values anywhere in the delivery envelope.

    The delivery contract has no legitimate Feigua field, tag or metadata, so
    rejecting it recursively prevents accidental cross-workflow exports.
    """

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if _has_feigua_marker(key_text):
                raise ValidationError(f"Campaign delivery {path}.{key_text} is outside the Music Flamingo-only boundary")
            _reject_feigua_context(child, path=f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_feigua_context(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and _has_feigua_marker(value):
        raise ValidationError(f"Campaign delivery {path} contains Feigua workflow data")


def _has_feigua_marker(value: str) -> bool:
    marker = normalized(value).replace(" ", "")
    return "feigua" in marker or "飞瓜" in marker
