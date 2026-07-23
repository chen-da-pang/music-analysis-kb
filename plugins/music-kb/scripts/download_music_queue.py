#!/usr/bin/env python3
"""Download only the songs in a prepared queue with musicdl.

This script is intentionally deterministic and is normally *run by Claude
Code*, not called directly by the publisher skill.  It updates the inventory
after every attempt so an interrupted run can be resumed without downloading
an already-present song again.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


MATCH_POLICY = "exact_kugou_mix_song_id_title_compatible_artist_v2"
LYRIC_RECEIPT_SCHEMA_VERSION = 1
LYRIC_QUERY_METHOD = "musicdl_kugou_exact_mix_song_id_v1"
DIRECT_LYRIC_QUERY_METHOD = "kugou_mixsong_page_exact_lyrics_v1"
DIRECT_HASH_LYRIC_QUERY_METHOD = "kugou_verified_audio_hash_exact_lyrics_v1"
LYRIC_NORMALIZER_VERSION = "lrc-v1"

_KUGOU_MIXSONG_HOST = "www.kugou.com"
_KUGOU_MIXSONG_PATH_PREFIX = "/mixsong/agent_gateway/"
_KUGOU_BROWSER_HEADERS = {
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
}
_DATA_FROM_SMARTY_ASSIGNMENT = re.compile(r"var\s+dataFromSmarty\s*=\s*", re.IGNORECASE)
_KUGOU_FILE_HASH = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_DIRECT_RETRYABLE_RESPONSE_KINDS = frozenset(
    {
        "direct_mixsong_page_request_failed",
        "direct_lyric_search_request_failed",
        "direct_lyric_download_request_failed",
        "direct_timeout",
    }
)

# These are exact platform-facing placeholder responses, not broad keyword
# guesses. An empty SongInfo.lyric is never enough to publish an exception.
INSTRUMENTAL_MARKERS = frozenset({"纯音乐，请欣赏", "纯音乐请欣赏", "此歌曲为纯音乐，请您欣赏"})
PLATFORM_UNAVAILABLE_MARKERS = frozenset({"暂无歌词", "该歌曲暂无歌词", "暂无歌词，敬请期待"})
NON_LYRIC_PAYLOAD_MARKERS = ("<script", "<html", "<!doctype", "获取失败")
_TIMESTAMP_PREFIX = re.compile(r"^\s*(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+\s*")
_METADATA_LINE = re.compile(
    r"^\s*\[(?:ar|al|ti|by|offset|re|ve|tool|length|au):[^\]]*\]\s*$",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ItemTimeoutError(RuntimeError):
    """Raised when one musicdl operation does not return in time."""


class DirectKugouLyricError(RuntimeError):
    """An auditable error from the exact Kugou mix-song lyric path."""

    def __init__(
        self,
        message: str,
        *,
        response_kind: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.response_kind = response_kind
        self.evidence = dict(evidence or {})


@contextmanager
def item_timeout(seconds: float):
    """Bound one musicdl network operation without changing its API."""

    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def raise_timeout(_signum: int, _frame: Any) -> None:
        raise ItemTimeoutError(f"musicdl operation timed out after {seconds:g}s")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def normalize_title(value: Any) -> str:
    """Normalize only presentation-equivalent title differences.

    NFKC handles full-width punctuation. Removing whitespace immediately
    around brackets accepts harmless API formatting differences without
    collapsing meaningful words or version qualifiers.
    """

    return re.sub(r"\s*([()\[\]{}])\s*", r"\1", normalize(value))


def split_artists(value: Any) -> set[str]:
    text = normalize(value)
    if not text or text == "null":
        return set()
    return {
        normalize(part)
        for part in re.split(r"\s*(?:、|&|/|,|，|;|；)\s*", text)
        if normalize(part)
    }


def artists_match(target: Any, found: Any) -> bool:
    wanted = normalize(target)
    candidate = normalize(found)
    if not wanted or not candidate:
        return False
    if wanted == candidate:
        return wanted != "null"
    wanted_artists = split_artists(wanted)
    candidate_artists = split_artists(candidate)
    return bool(wanted_artists and candidate_artists) and wanted_artists == candidate_artists


def title_artist_key(platform: str, title: Any, artist: Any) -> str:
    return f"{platform}:{normalize(title)}\x00{normalize(artist)}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_queue(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def path_for_download(raw_path: Any, audio_root: Path) -> Path:
    path = Path(str(raw_path)).expanduser()
    return path if path.is_absolute() else audio_root / path


def present(song: dict[str, Any], audio_root: Path) -> bool:
    download = song.get("download")
    if not isinstance(download, dict) or download.get("status") != "downloaded":
        return False
    if download.get("retention") == "purged_after_analysis":
        return True
    raw_path = download.get("path")
    return bool(raw_path and path_for_download(raw_path, audio_root).is_file())


def relative_path(path: Path, audio_root: Path) -> str:
    try:
        return path.resolve().relative_to(audio_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def file_record(path: Path, audio_root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "status": "downloaded",
        "retention": "retained",
        "path": relative_path(path, audio_root),
        "extension": path.suffix.lower().lstrip("."),
        "exists": True,
        "file_present": True,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": None,
        "recorded_at": now_iso(),
    }


def _search_payload(item: Any) -> Mapping[str, Any]:
    raw_data = getattr(item, "raw_data", None)
    if not isinstance(raw_data, Mapping):
        return {}
    search = raw_data.get("search")
    return search if isinstance(search, Mapping) else {}


def result_kugou_track_keys(item: Any) -> set[str]:
    """Return the exact mix-song identifiers carried by musicdl's raw result."""

    search = _search_payload(item)
    return {
        str(search[key]).strip()
        for key in ("MixSongID", "mixsongid", "EMixSongID", "emixsongid", "ID", "id")
        if search.get(key) is not None and str(search[key]).strip()
    }


def expected_kugou_track_key(candidate: Mapping[str, Any]) -> str | None:
    """Resolve a queue row's exact Kugou mix-song key without title fallback."""

    direct = str(candidate.get("platform_track_key") or "").strip()
    if direct:
        return direct
    identity = str(candidate.get("identity_key") or "").strip()
    if identity.startswith("kugou:") and identity.split(":", 1)[1].strip():
        return identity.split(":", 1)[1].strip()
    source_track_id = str(candidate.get("source_track_id") or "").strip()
    if source_track_id.startswith("kugou-") and source_track_id.removeprefix("kugou-").strip():
        return source_track_id.removeprefix("kugou-").strip()
    return None


def source_track_identity(candidate: Mapping[str, Any], expected_track_key: str | None) -> str | None:
    explicit = str(candidate.get("source_track_id") or "").strip()
    if explicit:
        return explicit
    return f"kugou-{expected_track_key}" if expected_track_key else None


def source_name(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("source_name") or candidate.get("platform") or "kugou").strip().lower()


def choose_match(
    results: list[Any],
    title: str,
    artist: str,
    *,
    platform_track_key: str | None = None,
) -> Any | None:
    """Return a title/artist-compatible result with an exact platform identity.

    Title and artist remain useful for candidate discovery, but they cannot
    prove a lyric or downloaded audio belongs to a canonical source track.
    Passing ``platform_track_key`` therefore requires a matching Kugou
    ``MixSongID``/``ID`` in the raw musicdl response.
    """

    wanted_title = normalize_title(title)
    for item in results:
        item_title = normalize_title(getattr(item, "song_name", ""))
        if item_title != wanted_title or not artists_match(artist, getattr(item, "singers", "")):
            continue
        if platform_track_key is not None and platform_track_key not in result_kugou_track_keys(item):
            continue
        return item
    return None


def result_metadata(item: Any) -> dict[str, Any]:
    return {
        "source": str(getattr(item, "source", None) or "KugouMusicClient"),
        "matched_title": str(getattr(item, "song_name", "") or ""),
        "matched_artist": str(getattr(item, "singers", "") or ""),
        "matched_kugou_track_keys": sorted(result_kugou_track_keys(item)),
        "matched_file_hash": str(getattr(item, "identifier", "") or ""),
        "match_policy": MATCH_POLICY,
    }


def rejected_candidate_metadata(results: list[Any], limit: int = 10) -> list[dict[str, str]]:
    rejected: list[dict[str, str]] = []
    for item in results[:limit]:
        entry = {
            "title": str(getattr(item, "song_name", "") or ""),
            "artist": str(getattr(item, "singers", "") or ""),
        }
        track_keys = sorted(result_kugou_track_keys(item))
        if track_keys:
            entry["kugou_track_keys"] = ",".join(track_keys)
        rejected.append(entry)
    return rejected


def normalize_lyric_text(value: Any) -> str:
    """Keep lyric text readable while removing only LRC transport markers."""

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


def lyric_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _lyric_response(item: Any) -> Any:
    raw_data = getattr(item, "raw_data", None)
    return raw_data.get("lyric") if isinstance(raw_data, Mapping) else None


def _marker(value: str) -> str:
    return re.sub(r"[\s，,。.!！]", "", value).strip()


def _explicitly_no_platform_lyric(response: Any) -> bool:
    return isinstance(response, Mapping) and isinstance(response.get("candidates"), list) and not response["candidates"]


def _is_non_lyric_payload(value: str) -> bool:
    folded = value.casefold()
    return any(marker.casefold() in folded for marker in NON_LYRIC_PAYLOAD_MARKERS)


def exact_kugou_mixsong_url(candidate: Mapping[str, Any]) -> str | None:
    """Return a safe, exact Kugou mix-song URL carried by the queue row.

    This is intentionally narrower than a general source URL.  The public
    mix-song page embeds the platform's own MixSongID, audio hash, and
    duration, which creates a deterministic chain into the lyric endpoint.
    A title/artist query cannot offer the same guarantee.
    """

    value = str(candidate.get("source_url") or candidate.get("play_link") or "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _KUGOU_MIXSONG_HOST
        or not parsed.path.startswith(_KUGOU_MIXSONG_PATH_PREFIX)
        or not parsed.path.endswith(".html")
    ):
        return None
    return value


def normalized_kugou_file_hash(value: Any) -> str | None:
    """Return a Kugou audio hash only when it has the canonical shape."""

    file_hash = str(value or "").strip().upper()
    return file_hash if _KUGOU_FILE_HASH.fullmatch(file_hash) else None


def verified_musicdl_file_hash(item: Any, expected_track_key: str | None) -> str | None:
    """Return a hash only when this musicdl object carries the exact MixSongID."""

    if not expected_track_key or expected_track_key not in result_kugou_track_keys(item):
        return None
    return normalized_kugou_file_hash(getattr(item, "identifier", None))


def _fetch_url(url: str, *, timeout: float) -> bytes:
    """Fetch a bounded Kugou response; kept small so tests can replace it."""

    request = Request(url, headers=_KUGOU_BROWSER_HEADERS)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL was allow-listed above.
        return response.read()


def _json_payload(payload: bytes, *, endpoint: str) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise DirectKugouLyricError(
            f"Kugou {endpoint} response was not valid JSON",
            response_kind="direct_invalid_json_response",
        ) from exc
    if not isinstance(value, Mapping):
        raise DirectKugouLyricError(
            f"Kugou {endpoint} response was not an object",
            response_kind="direct_invalid_json_response",
        )
    return value


def _mixsong_page_tracks(payload: bytes) -> list[Mapping[str, Any]]:
    try:
        page = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DirectKugouLyricError(
            "Kugou mix-song page was not UTF-8",
            response_kind="direct_invalid_mixsong_page",
        ) from exc
    assignment = _DATA_FROM_SMARTY_ASSIGNMENT.search(page)
    if assignment is None:
        raise DirectKugouLyricError(
            "Kugou mix-song page did not contain platform track metadata",
            response_kind="direct_invalid_mixsong_page",
        )
    try:
        decoded, _ = json.JSONDecoder().raw_decode(page[assignment.end() :].lstrip())
    except ValueError as exc:
        raise DirectKugouLyricError(
            "Kugou mix-song page track metadata was not valid JSON",
            response_kind="direct_invalid_mixsong_page",
        ) from exc
    if not isinstance(decoded, list):
        raise DirectKugouLyricError(
            "Kugou mix-song page track metadata was not a list",
            response_kind="direct_invalid_mixsong_page",
        )
    tracks = [item for item in decoded if isinstance(item, Mapping)]
    if not tracks:
        raise DirectKugouLyricError(
            "Kugou mix-song page did not provide a track record",
            response_kind="direct_invalid_mixsong_page",
        )
    return tracks


def _page_mix_song_id(track: Mapping[str, Any]) -> str:
    for key in ("mixsongid", "mix_song_id", "MixSongID"):
        if track.get(key) is not None:
            return str(track[key]).strip()
    return ""


def _page_track_for_mix_song_id(
    tracks: list[Mapping[str, Any]], expected_track_key: str
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    returned_ids = sorted({_page_mix_song_id(track) for track in tracks})
    matches = [track for track in tracks if _page_mix_song_id(track) == expected_track_key]
    if len(matches) != 1:
        # Some agent_gateway pages expose the exact URL, hash and duration but
        # deliberately render ``mixsongid: 0``. The source URL was already
        # bound to the requested platform ID by the queue materializer, so a
        # singleton zero-ID page remains identity-bound without falling back to
        # title/artist matching. A nonzero conflicting ID is always rejected.
        if len(tracks) == 1 and set(returned_ids).issubset({"", "0"}):
            return (
                tracks[0],
                {
                    "page_kugou_mix_song_id": returned_ids[0] if returned_ids else None,
                    "identity_verification": "queue_exact_source_url_page_id_unavailable_v1",
                },
            )
        raise DirectKugouLyricError(
            "Kugou mix-song page identity does not match the requested MixSongID",
            response_kind="direct_identity_mismatch",
            evidence={
                "page_matching_mix_song_id_count": len(matches),
                "page_returned_mix_song_ids": returned_ids,
            },
        )
    return (
        matches[0],
        {
            "page_kugou_mix_song_id": expected_track_key,
            "identity_verification": "mixsong_page_echo_v1",
        },
    )


def _page_track_lyric_query(
    track: Mapping[str, Any], candidate: Mapping[str, Any]
) -> tuple[str, str, int]:
    file_hash = str(track.get("hash") or track.get("FileHash") or "").strip()
    if not file_hash:
        raise DirectKugouLyricError(
            "Kugou mix-song page did not provide an audio hash",
            response_kind="direct_missing_platform_hash",
        )
    raw_duration = track.get("timelength")
    try:
        duration_ms = int(float(raw_duration))
    except (TypeError, ValueError) as exc:
        raise DirectKugouLyricError(
            "Kugou mix-song page did not provide a valid millisecond duration",
            response_kind="direct_missing_platform_duration",
        ) from exc
    if duration_ms <= 0:
        raise DirectKugouLyricError(
            "Kugou mix-song page did not provide a positive millisecond duration",
            response_kind="direct_missing_platform_duration",
        )
    keyword = str(track.get("audio_name") or track.get("filename") or "").strip()
    if not keyword:
        source_artist = str(track.get("artist_name") or candidate.get("artist") or "").strip()
        source_title = str(track.get("song_name") or candidate.get("title") or "").strip()
        keyword = " - ".join(part for part in (source_artist, source_title) if part)
    if not keyword:
        raise DirectKugouLyricError(
            "Kugou mix-song page did not provide a lyric-search keyword",
            response_kind="direct_missing_platform_metadata",
        )
    return file_hash, keyword, duration_ms


def _lyric_candidate_is_duration_compatible(candidate: Mapping[str, Any], duration_ms: int) -> bool:
    value = candidate.get("duration")
    if value is None or str(value).strip() == "":
        return True
    try:
        observed = int(float(value))
    except (TypeError, ValueError):
        return False
    return observed == duration_ms or observed * 1000 == duration_ms


def _lyric_candidate_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in ("id", "duration", "song", "singer")
        if candidate.get(key) is not None
    }


def _direct_kugou_lyric_item_from_hash(
    candidate: Mapping[str, Any],
    *,
    file_hash: str,
    keyword: str,
    duration_ms: int | None,
    evidence: Mapping[str, Any],
    timeout: float,
) -> tuple[Any, dict[str, Any]]:
    """Fetch a lyric after an already identity-bound Kugou audio hash is known."""

    expected_track_key = expected_kugou_track_key(candidate)
    if not expected_track_key:
        raise DirectKugouLyricError(
            "queue row has no exact Kugou MixSongID",
            response_kind="unsupported_source_identity",
        )
    query_duration = duration_ms if duration_ms is not None else -1
    search_url = "https://lyrics.kugou.com/search?" + urlencode(
        {"keyword": keyword, "duration": str(query_duration), "hash": file_hash}
    )
    try:
        lyric_search = _json_payload(
            _fetch_url(search_url, timeout=timeout), endpoint="lyric search"
        )
    except DirectKugouLyricError:
        raise
    except Exception as exc:  # pragma: no cover - network-dependent
        raise DirectKugouLyricError(
            f"Kugou lyric search request failed: {exc}",
            response_kind="direct_lyric_search_request_failed",
        ) from exc
    raw_candidates = lyric_search.get("candidates")
    if not isinstance(raw_candidates, list):
        raise DirectKugouLyricError(
            "Kugou lyric search did not provide a candidate list",
            response_kind="direct_invalid_lyric_search_response",
        )
    candidates = [item for item in raw_candidates if isinstance(item, Mapping)]
    receipt_evidence = {
        **dict(evidence),
        "lyric_search_hash": file_hash,
        "lyric_search_duration": query_duration,
        "lyric_candidate_count": len(candidates),
    }
    response_candidates = [_lyric_candidate_evidence(item) for item in candidates]
    if not candidates:
        return (
            SimpleNamespace(
                source="KugouMusicClient",
                song_name=str(candidate.get("title") or ""),
                singers=str(candidate.get("artist") or ""),
                identifier=file_hash,
                lyric=None,
                raw_data={
                    "search": {"MixSongID": expected_track_key, "hash": file_hash},
                    "lyric": {"candidates": []},
                },
            ),
            receipt_evidence,
        )
    usable_candidates = [
        item
        for item in candidates
        if item.get("id") is not None and str(item.get("accesskey") or "").strip()
    ]
    if duration_ms is None:
        selected = usable_candidates[0] if len(usable_candidates) == 1 else None
        duration_compatible: bool | None = None
        if len(usable_candidates) > 1:
            raise DirectKugouLyricError(
                "Kugou hash lyric lookup returned multiple usable candidates without a duration",
                response_kind="direct_ambiguous_hash_lyric_candidates",
                evidence={**receipt_evidence, "lyric_candidates": response_candidates[:10]},
            )
    else:
        selected = next(
            (item for item in usable_candidates if _lyric_candidate_is_duration_compatible(item, duration_ms)),
            usable_candidates[0] if usable_candidates else None,
        )
        duration_compatible = (
            _lyric_candidate_is_duration_compatible(selected, duration_ms)
            if selected is not None
            else None
        )
    if selected is None:
        raise DirectKugouLyricError(
            "Kugou lyric search candidates did not include a usable result",
            response_kind="direct_invalid_lyric_search_response",
            evidence={**receipt_evidence, "lyric_candidates": response_candidates[:10]},
        )
    selected_id = str(selected["id"])
    receipt_evidence["selected_lyric_candidate_id"] = selected_id
    receipt_evidence["selected_lyric_candidate_duration_compatible"] = duration_compatible
    download_url = "https://lyrics.kugou.com/download?" + urlencode(
        {
            "ver": "1",
            "client": "pc",
            "id": selected_id,
            "accesskey": str(selected["accesskey"]),
            "fmt": "lrc",
            "charset": "utf8",
        }
    )
    try:
        lyric_download = _json_payload(
            _fetch_url(download_url, timeout=timeout), endpoint="lyric download"
        )
    except DirectKugouLyricError:
        raise
    except Exception as exc:  # pragma: no cover - network-dependent
        raise DirectKugouLyricError(
            f"Kugou lyric download request failed: {exc}",
            response_kind="direct_lyric_download_request_failed",
            evidence=receipt_evidence,
        ) from exc
    encoded_lyric = lyric_download.get("content")
    if not isinstance(encoded_lyric, str) or not encoded_lyric:
        raise DirectKugouLyricError(
            "Kugou lyric download did not provide encoded lyric content",
            response_kind="direct_missing_lyric_content",
            evidence=receipt_evidence,
        )
    try:
        lyric = base64.b64decode(encoded_lyric.encode("ascii"), validate=True).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise DirectKugouLyricError(
            "Kugou lyric download content could not be decoded as UTF-8 LRC",
            response_kind="direct_invalid_lyric_content",
            evidence=receipt_evidence,
        ) from exc
    return (
        SimpleNamespace(
            source="KugouMusicClient",
            song_name=str(candidate.get("title") or ""),
            singers=str(candidate.get("artist") or ""),
            identifier=file_hash,
            lyric=lyric,
            raw_data={
                "search": {"MixSongID": expected_track_key, "hash": file_hash},
                "lyric": {
                    "candidates": response_candidates,
                    "selected_candidate_id": selected_id,
                    "download_content_sha256": hashlib.sha256(encoded_lyric.encode("ascii")).hexdigest(),
                },
            },
        ),
        receipt_evidence,
    )


def _direct_kugou_lyric_item(
    candidate: Mapping[str, Any],
    *,
    mixsong_url: str,
    expected_track_key: str,
    timeout: float,
) -> tuple[Any, dict[str, Any]]:
    """Fetch lyrics through the exact MixSongID -> page hash -> lyric chain.

    The page must echo the queue's MixSongID before its hash is trusted. A
    singleton ``mixsongid: 0`` page is the documented platform exception: its
    exact queue source URL remains the identity proof. The platform lyric
    search is then called with that hash and exact duration; no title/artist
    matching or downloaded audio is involved.
    """

    try:
        page_payload = _fetch_url(mixsong_url, timeout=timeout)
    except Exception as exc:  # pragma: no cover - network-dependent
        raise DirectKugouLyricError(
            f"Kugou mix-song page request failed: {exc}",
            response_kind="direct_mixsong_page_request_failed",
            evidence={"kugou_mixsong_url": mixsong_url},
        ) from exc
    track, identity_evidence = _page_track_for_mix_song_id(
        _mixsong_page_tracks(page_payload), expected_track_key
    )
    file_hash, keyword, duration_ms = _page_track_lyric_query(track, candidate)
    item, evidence = _direct_kugou_lyric_item_from_hash(
        candidate,
        file_hash=file_hash,
        keyword=keyword,
        duration_ms=duration_ms,
        evidence={
            "kugou_mixsong_url": mixsong_url,
            **identity_evidence,
            "page_file_hash": file_hash,
            "page_duration_ms": duration_ms,
            "source_page_sha256": hashlib.sha256(page_payload).hexdigest(),
        },
        timeout=timeout,
    )
    item.song_name = str(track.get("song_name") or candidate.get("title") or "")
    item.singers = str(track.get("artist_name") or candidate.get("artist") or "")
    return item, evidence


def direct_kugou_lyric_receipt(
    candidate: Mapping[str, Any], *, run_id: str, timeout: float
) -> dict[str, Any] | None:
    """Return an exact-page lyric receipt, or ``None`` when no safe URL exists."""

    expected_track_key = expected_kugou_track_key(candidate)
    mixsong_url = exact_kugou_mixsong_url(candidate)
    if not expected_track_key or not mixsong_url:
        return None
    try:
        item, evidence = _direct_kugou_lyric_item(
            candidate,
            mixsong_url=mixsong_url,
            expected_track_key=expected_track_key,
            timeout=timeout,
        )
    except DirectKugouLyricError as exc:
        return lyric_receipt_for_match(
            candidate,
            None,
            run_id=run_id,
            reason=str(exc),
            response_kind=exc.response_kind,
            query_method=DIRECT_LYRIC_QUERY_METHOD,
            evidence_extra={"kugou_mixsong_url": mixsong_url, **exc.evidence},
        )
    return lyric_receipt_for_match(
        candidate,
        item,
        run_id=run_id,
        query_method=DIRECT_LYRIC_QUERY_METHOD,
        evidence_extra=evidence,
    )


def direct_kugou_hash_lyric_receipt(
    candidate: Mapping[str, Any],
    *,
    file_hash: Any,
    run_id: str,
    timeout: float,
    identity_verification: str,
    evidence_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fetch lyrics with a hash that was already bound to the exact MixSongID.

    This intentionally does not search by title/artist for identity.  The
    caller supplies a hash only after proving its link to the queued
    ``MixSongID``; title/artist merely form Kugou's required lookup keyword.
    When the lyric API returns several usable candidates without a duration,
    the result stays pending rather than choosing one heuristically.
    """

    expected_track_key = expected_kugou_track_key(candidate)
    verified_hash = normalized_kugou_file_hash(file_hash)
    title = str(candidate.get("title") or "").strip()
    artist = str(candidate.get("artist") or "").strip()
    keyword = " - ".join(part for part in (artist, title) if part)
    if not expected_track_key or not verified_hash:
        return None
    if not keyword:
        return lyric_receipt_for_match(
            candidate,
            None,
            run_id=run_id,
            reason="queue row has no title or artist for the verified Kugou hash lyric lookup",
            response_kind="direct_missing_platform_metadata",
            query_method=DIRECT_HASH_LYRIC_QUERY_METHOD,
            evidence_extra={
                "identity_verification": identity_verification,
                "verified_kugou_file_hash": verified_hash,
                **dict(evidence_extra or {}),
            },
        )
    evidence = dict(evidence_extra or {})
    evidence.update(
        {
            "identity_verification": identity_verification,
            "verified_kugou_file_hash": verified_hash,
        }
    )
    try:
        item, direct_evidence = _direct_kugou_lyric_item_from_hash(
            candidate,
            file_hash=verified_hash,
            keyword=keyword,
            duration_ms=None,
            evidence=evidence,
            timeout=timeout,
        )
    except DirectKugouLyricError as exc:
        return lyric_receipt_for_match(
            candidate,
            None,
            run_id=run_id,
            reason=str(exc),
            response_kind=exc.response_kind,
            query_method=DIRECT_HASH_LYRIC_QUERY_METHOD,
            evidence_extra={**evidence, **exc.evidence},
        )
    return lyric_receipt_for_match(
        candidate,
        item,
        run_id=run_id,
        query_method=DIRECT_HASH_LYRIC_QUERY_METHOD,
        evidence_extra=direct_evidence,
    )


def _archived_kugou_hash_proof(candidate: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Validate the inventory-to-queue proof before trusting an archived hash."""

    expected_track_key = expected_kugou_track_key(candidate)
    expected_identity = f"kugou:{expected_track_key}" if expected_track_key else ""
    candidate_identity = str(candidate.get("identity_key") or "").strip()
    provenance = candidate.get("archived_kugou_file_hash_provenance")
    verified_hash = normalized_kugou_file_hash(candidate.get("archived_kugou_file_hash"))
    if (
        not expected_identity
        or candidate_identity != expected_identity
        or not verified_hash
        or not isinstance(provenance, Mapping)
        or provenance.get("method") != "song_inventory_download_path_exact_identity_v1"
        or str(provenance.get("inventory_identity_key") or "").strip() != expected_identity
        or str(provenance.get("download_status") or "").strip() != "downloaded"
    ):
        return None
    return verified_hash, {
        "inventory_identity_key": expected_identity,
        "inventory_download_retention": provenance.get("download_retention"),
        "inventory_relative_audio_path": provenance.get("inventory_relative_audio_path"),
    }


def direct_kugou_archived_hash_lyric_receipt(
    candidate: Mapping[str, Any], *, run_id: str, timeout: float
) -> dict[str, Any] | None:
    """Use only a validated, exact-identity archived Kugou audio hash."""

    proof = _archived_kugou_hash_proof(candidate)
    if proof is None:
        return None
    file_hash, evidence = proof
    return direct_kugou_hash_lyric_receipt(
        candidate,
        file_hash=file_hash,
        run_id=run_id,
        timeout=timeout,
        identity_verification="queue_exact_mixsong_inventory_download_hash_v1",
        evidence_extra=evidence,
    )


def lyric_receipt_for_match(
    candidate: Mapping[str, Any],
    item: Any | None,
    *,
    run_id: str,
    reason: str | None = None,
    response_kind: str | None = None,
    query_method: str = LYRIC_QUERY_METHOD,
    evidence_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an auditable receipt for one exact-result lyric attempt.

    Missing lyrics remain ``pending`` unless the Kugou response explicitly
    reports zero lyric candidates or returns an exact, known exception marker.
    Network, parsing, and identity errors therefore cannot masquerade as
    platform-unavailable content.
    """

    expected_track_key = expected_kugou_track_key(candidate)
    receipt_source_name = source_name(candidate)
    receipt_source_track_id = source_track_identity(candidate, expected_track_key)
    if not receipt_source_track_id:
        receipt_source_track_id = str(candidate.get("identity_key") or "").strip()
    result_keys = sorted(result_kugou_track_keys(item)) if item is not None else []
    response = _lyric_response(item) if item is not None else None
    raw_lyric = getattr(item, "lyric", None) if item is not None else None
    readable = normalize_lyric_text(raw_lyric)
    status = "pending"
    response_kind = response_kind or ("query_error" if reason else "missing_lyric")
    final_reason = reason or "exact platform result did not return a publishable lyric response"
    lyric_text: str | None = None

    if item is not None and expected_track_key and expected_track_key not in result_keys:
        response_kind = "identity_mismatch"
        final_reason = "musicdl result identity does not match the requested Kugou mix-song ID"
    elif item is not None and readable:
        marker = _marker(readable)
        if marker in {_marker(value) for value in INSTRUMENTAL_MARKERS}:
            status = "instrumental"
            response_kind = "platform_instrumental_marker"
            final_reason = "exact Kugou lyric response explicitly marks this recording as instrumental"
        elif marker in {_marker(value) for value in PLATFORM_UNAVAILABLE_MARKERS}:
            status = "platform_unavailable"
            response_kind = "platform_unavailable_marker"
            final_reason = "exact Kugou lyric response explicitly reports lyrics unavailable"
        elif _is_non_lyric_payload(readable):
            response_kind = "invalid_lyric_payload"
            final_reason = "exact Kugou lyric response was an HTML or failure placeholder, not lyrics"
        else:
            status = "available"
            response_kind = "lyrics_available"
            final_reason = "exact Kugou source identity returned lyric text"
            lyric_text = readable
    elif item is not None and _explicitly_no_platform_lyric(response):
        status = "platform_unavailable"
        response_kind = "platform_zero_lyric_candidates"
        final_reason = "exact Kugou lyric endpoint explicitly returned zero lyric candidates"
    elif item is not None and raw_lyric is not None:
        response_kind = "empty_after_normalization"
        final_reason = "exact Kugou result returned lyric data that was empty after LRC normalization"

    evidence = {
        "source_name": receipt_source_name,
        "source_track_id": receipt_source_track_id,
        "reason": final_reason,
        "query_method": query_method,
        "response_kind": response_kind,
        "run_id": run_id,
        "expected_kugou_mix_song_id": expected_track_key,
        "returned_kugou_mix_song_ids": result_keys,
        "returned_file_hash": str(getattr(item, "identifier", "") or "") if item is not None else None,
        "raw_response_sha256": _json_sha256(response),
    }
    if evidence_extra:
        evidence.update(dict(evidence_extra))
    receipt: dict[str, Any] = {
        "schema_version": LYRIC_RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "source_name": receipt_source_name,
        "source_track_id": receipt_source_track_id,
        "status": status,
        "normalizer_version": LYRIC_NORMALIZER_VERSION,
        "evidence": evidence,
    }
    for optional_key in ("recording_id", "source_track_row_id", "identity_key", "platform_track_key"):
        if candidate.get(optional_key) is not None:
            receipt[optional_key] = candidate[optional_key]
    if lyric_text is not None:
        receipt["lyric_text"] = lyric_text
        receipt["text_sha256"] = lyric_text_sha256(lyric_text)
    return receipt


def append_lyric_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Append one durable worker receipt without depending on an LRC file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(receipt), handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def lyric_progress_metadata(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": str(receipt.get("status") or "pending"),
        "source_name": receipt.get("source_name"),
        "source_track_id": receipt.get("source_track_id"),
        "text_sha256": receipt.get("text_sha256"),
        "response_kind": (
            receipt.get("evidence", {}).get("response_kind")
            if isinstance(receipt.get("evidence"), Mapping)
            else None
        ),
        "at": now_iso(),
    }


def update_inventory_song(inventory: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    songs = inventory.setdefault("songs", [])
    identity = candidate.get("identity_key")
    title_key = candidate.get("title_artist_key") or title_artist_key(
        candidate.get("platform", "kugou"), candidate.get("title"), candidate.get("artist")
    )
    item = next((song for song in songs if song.get("identity_key") == identity), None) if identity else None
    if item is None:
        item = next((song for song in songs if song.get("title_artist_key") == title_key), None)
    if item is None:
        item = dict(candidate)
        item.setdefault("source_runs", [])
        item.setdefault("chart_appearances", [])
        songs.append(item)
    else:
        for key in ("identity_key", "title_artist_key", "platform", "platform_track_key", "title", "artist", "play_link"):
            if candidate.get(key) is not None:
                item[key] = candidate[key]
        if candidate.get("source_run") and candidate["source_run"] not in item.setdefault("source_runs", []):
            item["source_runs"].append(candidate["source_run"])
        if candidate.get("chart_appearances"):
            item["chart_appearances"] = candidate["chart_appearances"]
    item["last_seen_at"] = now_iso()
    return item


def record_attempt(item: dict[str, Any], status: str, **extra: Any) -> None:
    item["download"] = {
        "status": status,
        "retention": "retained",
        "path": None,
        "file_present": False,
        "exists": False,
        "size_bytes": None,
        "mtime_ns": None,
        "sha256": None,
        "recorded_at": now_iso(),
        **extra,
    }


def run_download(
    queue_path: Path,
    inventory_path: Path,
    work_dir: Path,
    progress_path: Path,
    log_path: Path,
    run_id: str,
    max_items: int | None,
    dry_run: bool,
    delay: float,
    retries: int,
    search_size: int,
    item_timeout_seconds: float,
    lyrics_receipt_path: Path | None = None,
) -> dict[str, Any]:
    if item_timeout_seconds <= 0:
        raise ValueError("item-timeout-seconds must be positive")
    queue = load_queue(queue_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    lyrics_receipt_path = (
        lyrics_receipt_path.expanduser()
        if lyrics_receipt_path is not None
        else progress_path.parent / "lyrics-receipts.jsonl"
    )
    audio_root = work_dir / "KugouMusicClient"
    audio_root.mkdir(parents=True, exist_ok=True)
    progress = {
        "schema_version": 1,
        "run_id": run_id,
        "queue": str(queue_path.resolve()),
        "started_at": now_iso(),
        "downloaded": {},
        "results": {},
        "lyrics": {},
    }
    if progress_path.exists():
        try:
            old = json.loads(progress_path.read_text(encoding="utf-8"))
            if old.get("run_id") == run_id:
                progress = old
        except (ValueError, OSError):
            pass
    # A chunk/session retry must not inherit the previous chunk's terminal
    # marker. The wrapper uses this to distinguish a complete worker exit from
    # a Claude session that was cut off mid-command.
    progress.pop("finished_at", None)
    progress.pop("summary", None)
    progress.setdefault("lyrics", {})

    summary = {
        "run_id": run_id,
        "queue": len(queue),
        "downloaded": 0,
        "skipped_existing": 0,
        "failed": 0,
        "no_results": 0,
        "lyrics_available": 0,
        "lyrics_instrumental": 0,
        "lyrics_platform_unavailable": 0,
        "lyrics_pending": 0,
        "lyrics_receipt": str(lyrics_receipt_path.resolve()),
        "dry_run": dry_run,
    }
    selected = queue if max_items is None else queue[:max_items]
    if dry_run:
        summary["would_process"] = len(selected)
        summary["remaining_after_limit"] = max(0, len(queue) - len(selected))
        print(json.dumps(summary, ensure_ascii=False))
        return summary

    try:
        from musicdl.musicdl import MusicClient
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise SystemExit(f"无法导入 musicdl，请在 Claude Code 环境安装 musicdl: {exc}") from exc

    client = MusicClient(
        music_sources=["KugouMusicClient"],
        init_music_clients_cfg={
            "KugouMusicClient": {
                "work_dir": str(work_dir),
                "search_size_per_source": search_size,
            }
        },
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] {message}\n")

    by_identity = {song.get("identity_key"): song for song in inventory.get("songs", [])}
    by_title = {song.get("title_artist_key"): song for song in inventory.get("songs", [])}
    for index, candidate in enumerate(selected, start=1):
        identity = candidate.get("identity_key") or candidate.get("title_artist_key")
        item = by_identity.get(candidate.get("identity_key")) or by_title.get(candidate.get("title_artist_key"))
        if item and present(item, audio_root):
            summary["skipped_existing"] += 1
            progress["results"][identity] = {"status": "skipped_existing", "at": now_iso()}
            continue

        title, artist = candidate["title"], candidate["artist"]
        item = update_inventory_song(inventory, candidate)
        log(f"[{index}/{len(selected)}] {title} - {artist}")
        expected_track_key = expected_kugou_track_key(candidate)
        if not expected_track_key:
            summary["no_results"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            audit = {
                "reason": "missing_kugou_platform_track_identity",
                "match_policy": MATCH_POLICY,
            }
            record_attempt(item, "no_results", **audit)
            progress["results"][identity] = {"status": "no_results", "at": now_iso(), **audit}
            atomic_write_json(progress_path, progress)
            inventory["generated_at"] = now_iso()
            inventory["counts"] = recompute_counts(inventory)
            atomic_write_json(inventory_path, inventory)
            continue
        found: list[Any] | None = None
        last_error = None
        for attempt in range(retries + 1):
            try:
                with item_timeout(item_timeout_seconds):
                    found = client.search(f"{title} {artist}").get("KugouMusicClient", [])
                break
            except ItemTimeoutError as exc:
                # A timeout is a bounded, auditable failure. Retrying the same
                # request would only delay the rest of the queue.
                last_error = str(exc)
                break
            except Exception as exc:  # pragma: no cover - network-dependent
                last_error = str(exc)
                if attempt < retries:
                    time.sleep(3)
        if found is None:
            summary["failed"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            record_attempt(item, "failed", error=last_error or "search failed")
            progress["results"][identity] = {"status": "failed", "error": last_error, "at": now_iso()}
            atomic_write_json(progress_path, progress)
            atomic_write_json(inventory_path, inventory)
            continue
        if not found:
            summary["no_results"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            record_attempt(item, "no_results")
            progress["results"][identity] = {"status": "no_results", "at": now_iso()}
            atomic_write_json(progress_path, progress)
            atomic_write_json(inventory_path, inventory)
            continue

        best = choose_match(found, title, artist, platform_track_key=expected_track_key)
        if best is None:
            summary["no_results"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            audit = {
                "reason": "no_exact_platform_identity_title_artist_match",
                "match_policy": MATCH_POLICY,
                "expected_kugou_mix_song_id": expected_track_key,
                "candidate_count": len(found),
                "rejected_candidates": rejected_candidate_metadata(found),
            }
            record_attempt(item, "no_results", **audit)
            progress["results"][identity] = {"status": "no_results", "at": now_iso(), **audit}
            atomic_write_json(progress_path, progress)
            inventory["generated_at"] = now_iso()
            inventory["counts"] = recompute_counts(inventory)
            atomic_write_json(inventory_path, inventory)
            continue

        matched = result_metadata(best)
        try:
            with item_timeout(item_timeout_seconds):
                downloaded = client.download([best])
            raw_path = None
            result: Any | None = None
            if downloaded:
                result = downloaded[0]
                raw_path = getattr(result, "save_path", None) or getattr(result, "saved_path", None)
            path = path_for_download(raw_path, audio_root) if raw_path else None
            if not path or not path.is_file():
                raise RuntimeError(f"musicdl 未返回已存在的文件路径: {raw_path!r}")
            item["download"] = {**file_record(path, audio_root), **matched}
            progress.setdefault("downloaded", {})[identity] = {
                "title": title,
                "artist": artist,
                "file": str(path),
                "ext": item["download"]["extension"],
                "size_bytes": item["download"]["size_bytes"],
                "time": item["download"]["recorded_at"],
                "play_link": candidate.get("play_link"),
                "platform": candidate.get("platform", "kugou"),
                "platform_track_key": candidate.get("platform_track_key"),
                "identity_key": candidate.get("identity_key"),
                **matched,
            }
            summary["downloaded"] += 1
            progress["results"][identity] = {
                "status": "downloaded",
                "path": item["download"]["path"],
                "at": now_iso(),
                **matched,
            }
            lyric_source = result if result is not None and getattr(result, "raw_data", None) is not None else best
            receipt = lyric_receipt_for_match(candidate, lyric_source, run_id=run_id)
            # Audio downloads still use musicdl as their primary path.  When
            # its lyric attachment is absent or a known bad payload, retry the
            # lyric through the exact public page first. If an old page no
            # longer exposes its hash, the exact-ID-validated musicdl search
            # result carries an independently bound hash, so use it without
            # re-downloading audio or accepting a title-only match.
            if receipt["status"] == "pending":
                direct_receipt: dict[str, Any] | None = None
                try:
                    with item_timeout(item_timeout_seconds):
                        direct_receipt = direct_kugou_lyric_receipt(
                            candidate, run_id=run_id, timeout=item_timeout_seconds
                        )
                except ItemTimeoutError:
                    pass
                if direct_receipt is not None and direct_receipt["status"] != "pending":
                    receipt = direct_receipt
                if receipt["status"] == "pending":
                    musicdl_hash: str | None = None
                    musicdl_hash_identity_source: str | None = None
                    for source, source_label in (
                        (best, "musicdl_search_result_exact_mixsong_id_v1"),
                        (result, "musicdl_download_result_exact_mixsong_id_v1"),
                    ):
                        candidate_hash = verified_musicdl_file_hash(source, expected_track_key)
                        if candidate_hash is not None:
                            musicdl_hash = candidate_hash
                            musicdl_hash_identity_source = source_label
                            break
                    direct_hash_receipt: dict[str, Any] | None = None
                    if musicdl_hash is not None:
                        hash_evidence = {
                            "musicdl_matched_kugou_mix_song_id": expected_track_key,
                            "musicdl_hash_identity_source": musicdl_hash_identity_source,
                            "prior_direct_response_kind": (
                                direct_receipt["evidence"].get("response_kind")
                                if direct_receipt is not None
                                and isinstance(direct_receipt.get("evidence"), Mapping)
                                else None
                            ),
                        }
                        try:
                            with item_timeout(item_timeout_seconds):
                                direct_hash_receipt = direct_kugou_hash_lyric_receipt(
                                    candidate,
                                    file_hash=musicdl_hash,
                                    run_id=run_id,
                                    timeout=item_timeout_seconds,
                                    identity_verification="musicdl_exact_mixsong_id_file_hash_v1",
                                    evidence_extra=hash_evidence,
                                )
                        except ItemTimeoutError as exc:
                            direct_hash_receipt = lyric_receipt_for_match(
                                candidate,
                                None,
                                run_id=run_id,
                                reason=str(exc),
                                response_kind="direct_timeout",
                                query_method=DIRECT_HASH_LYRIC_QUERY_METHOD,
                                evidence_extra=hash_evidence,
                            )
                    if direct_hash_receipt is not None:
                        receipt = direct_hash_receipt
            try:
                append_lyric_receipt(lyrics_receipt_path, receipt)
                progress["lyrics"][identity] = lyric_progress_metadata(receipt)
                summary[f"lyrics_{receipt['status']}"] += 1
            except OSError as lyric_error:
                progress["lyrics"][identity] = {
                    "status": "pending",
                    "error": f"could not persist lyric receipt: {lyric_error}",
                    "at": now_iso(),
                }
                summary["lyrics_pending"] += 1
            log(f"OK {path}")
        except Exception as exc:  # pragma: no cover - network/filesystem-dependent
            summary["failed"] += 1
            record_attempt(item, "failed", error=str(exc))
            progress["results"][identity] = {"status": "failed", "error": str(exc), "at": now_iso()}
            log(f"FAILED {exc}")
        atomic_write_json(progress_path, progress)
        inventory["generated_at"] = now_iso()
        inventory["counts"] = recompute_counts(inventory)
        atomic_write_json(inventory_path, inventory)
        if delay:
            time.sleep(delay)

    progress["finished_at"] = now_iso()
    progress["summary"] = summary
    atomic_write_json(progress_path, progress)
    inventory["generated_at"] = now_iso()
    inventory["counts"] = recompute_counts(inventory)
    atomic_write_json(inventory_path, inventory)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def _queue_identity(candidate: Mapping[str, Any]) -> str:
    for key in ("source_track_id", "identity_key", "title_artist_key"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value
    raise ValueError("queue row has no source_track_id, identity_key, or title_artist_key")


def run_lyrics_only(
    queue_path: Path,
    work_dir: Path,
    progress_path: Path,
    log_path: Path,
    run_id: str,
    max_items: int | None,
    dry_run: bool,
    delay: float,
    retries: int,
    search_size: int,
    item_timeout_seconds: float,
    lyrics_receipt_path: Path,
) -> dict[str, Any]:
    """Query lyrics through the same Kugou worker without touching audio/inventory."""

    if item_timeout_seconds <= 0:
        raise ValueError("item-timeout-seconds must be positive")
    queue = load_queue(queue_path)
    lyrics_receipt_path = lyrics_receipt_path.expanduser()
    progress = {
        "schema_version": 1,
        "mode": "lyrics_only",
        "run_id": run_id,
        "queue": str(queue_path.resolve()),
        "started_at": now_iso(),
        "results": {},
    }
    if progress_path.exists():
        try:
            old = json.loads(progress_path.read_text(encoding="utf-8"))
            if old.get("run_id") == run_id and old.get("mode") == "lyrics_only":
                progress = old
        except (ValueError, OSError):
            pass
    progress.pop("finished_at", None)
    progress.pop("summary", None)
    progress.setdefault("results", {})

    summary = {
        "run_id": run_id,
        "mode": "lyrics_only",
        "queue": len(queue),
        "attempted": 0,
        "skipped_terminal": 0,
        "available": 0,
        "instrumental": 0,
        "platform_unavailable": 0,
        "pending": 0,
        "lyrics_receipt": str(lyrics_receipt_path.resolve()),
        "dry_run": dry_run,
    }
    selected = queue if max_items is None else queue[:max_items]
    if dry_run:
        summary["would_process"] = len(selected)
        summary["remaining_after_limit"] = max(0, len(queue) - len(selected))
        print(json.dumps(summary, ensure_ascii=False))
        return summary

    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] {message}\n")

    # All materialized historical rows carry an exact mix-song URL, so the
    # direct page path normally avoids importing musicdl entirely.  Keep the
    # old client as a lazy compatibility fallback for a manually supplied
    # queue row that lacks that durable URL.
    client: Any | None = None

    def musicdl_client() -> Any:
        nonlocal client
        if client is not None:
            return client
        try:
            from musicdl.musicdl import MusicClient
        except Exception as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(f"无法导入 musicdl，请在 Claude Code 环境安装 musicdl: {exc}") from exc
        client = MusicClient(
            music_sources=["KugouMusicClient"],
            init_music_clients_cfg={
                "KugouMusicClient": {
                    "work_dir": str(work_dir),
                    "search_size_per_source": search_size,
                }
            },
        )
        return client

    terminal_statuses = {"available", "instrumental", "platform_unavailable"}
    for index, candidate in enumerate(selected, start=1):
        identity = _queue_identity(candidate)
        existing = progress["results"].get(identity)
        if isinstance(existing, Mapping) and existing.get("lyric_status") in terminal_statuses:
            summary["skipped_terminal"] += 1
            continue
        title = str(candidate.get("title") or "").strip()
        artist = str(candidate.get("artist") or "").strip()
        expected_track_key = expected_kugou_track_key(candidate)
        summary["attempted"] += 1
        log(f"lyrics-only [{index}/{len(selected)}] {title} - {artist}")
        receipt: dict[str, Any]
        if not expected_track_key:
            receipt = lyric_receipt_for_match(
                candidate,
                None,
                run_id=run_id,
                reason="source_track_id cannot be converted to an exact Kugou mix-song ID",
                response_kind="unsupported_source_identity",
            )
        else:
            direct_receipt: dict[str, Any] | None = None
            mixsong_url = exact_kugou_mixsong_url(candidate)
            if mixsong_url is not None:
                for attempt in range(retries + 1):
                    try:
                        with item_timeout(item_timeout_seconds):
                            direct_receipt = direct_kugou_lyric_receipt(
                                candidate, run_id=run_id, timeout=item_timeout_seconds
                            )
                    except ItemTimeoutError as exc:
                        direct_receipt = lyric_receipt_for_match(
                            candidate,
                            None,
                            run_id=run_id,
                            reason=str(exc),
                            response_kind="direct_timeout",
                            query_method=DIRECT_LYRIC_QUERY_METHOD,
                            evidence_extra={"kugou_mixsong_url": mixsong_url},
                        )
                    if direct_receipt is None:
                        break
                    response_kind = str(direct_receipt["evidence"].get("response_kind") or "")
                    if response_kind not in _DIRECT_RETRYABLE_RESPONSE_KINDS or attempt >= retries:
                        break
                    time.sleep(3)
            archived_hash_receipt: dict[str, Any] | None = None
            if direct_receipt is None or direct_receipt["status"] == "pending":
                for attempt in range(retries + 1):
                    try:
                        with item_timeout(item_timeout_seconds):
                            archived_hash_receipt = direct_kugou_archived_hash_lyric_receipt(
                                candidate, run_id=run_id, timeout=item_timeout_seconds
                            )
                    except ItemTimeoutError as exc:
                        archived_hash_receipt = lyric_receipt_for_match(
                            candidate,
                            None,
                            run_id=run_id,
                            reason=str(exc),
                            response_kind="direct_timeout",
                            query_method=DIRECT_HASH_LYRIC_QUERY_METHOD,
                        )
                    if archived_hash_receipt is None:
                        break
                    response_kind = str(
                        archived_hash_receipt["evidence"].get("response_kind") or ""
                    )
                    if response_kind not in _DIRECT_RETRYABLE_RESPONSE_KINDS or attempt >= retries:
                        break
                    time.sleep(3)
            if archived_hash_receipt is not None:
                if direct_receipt is not None:
                    archived_hash_receipt["evidence"]["prior_direct_response_kind"] = (
                        direct_receipt["evidence"].get("response_kind")
                    )
                    archived_hash_receipt["evidence"]["prior_direct_reason"] = (
                        direct_receipt["evidence"].get("reason")
                    )
                receipt = archived_hash_receipt
            elif direct_receipt is not None:
                receipt = direct_receipt
            elif not title or not artist:
                receipt = lyric_receipt_for_match(
                    candidate,
                    None,
                    run_id=run_id,
                    reason="queue row has no title or artist and no exact Kugou mix-song URL",
                    response_kind="invalid_queue_row",
                )
            else:
                found: list[Any] | None = None
                last_error: str | None = None
                for attempt in range(retries + 1):
                    try:
                        with item_timeout(item_timeout_seconds):
                            found = musicdl_client().search(f"{title} {artist}").get("KugouMusicClient", [])
                        break
                    except ItemTimeoutError as exc:
                        last_error = str(exc)
                        break
                    except Exception as exc:  # pragma: no cover - network-dependent
                        last_error = str(exc)
                        if attempt < retries:
                            time.sleep(3)
                if found is None:
                    receipt = lyric_receipt_for_match(
                        candidate,
                        None,
                        run_id=run_id,
                        reason=last_error or "musicdl search failed",
                        response_kind="network_or_parse_error",
                    )
                else:
                    best = choose_match(
                        found,
                        title,
                        artist,
                        platform_track_key=expected_track_key,
                    )
                    if best is None:
                        receipt = lyric_receipt_for_match(
                            candidate,
                            None,
                            run_id=run_id,
                            reason="no title/artist-compatible musicdl result carried the requested Kugou mix-song ID",
                            response_kind="identity_mismatch",
                        )
                    else:
                        receipt = lyric_receipt_for_match(candidate, best, run_id=run_id)
        append_lyric_receipt(lyrics_receipt_path, receipt)
        progress["results"][identity] = {
            "query_status": "completed",
            "lyric_status": receipt["status"],
            "source_track_id": receipt["source_track_id"],
            "text_sha256": receipt.get("text_sha256"),
            "response_kind": receipt["evidence"]["response_kind"],
            "at": now_iso(),
        }
        summary[str(receipt["status"])] += 1
        atomic_write_json(progress_path, progress)
        if delay:
            time.sleep(delay)

    progress["finished_at"] = now_iso()
    progress["summary"] = summary
    atomic_write_json(progress_path, progress)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def recompute_counts(inventory: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(inventory.get("songs", []))}
    for song in inventory.get("songs", []):
        status = str(song.get("download", {}).get("status", "not_attempted"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, help="Required for audio-download mode; unused by --lyrics-only")
    parser.add_argument("--work-dir", type=Path, required=True, help="musicdl work_dir; audio files go under KugouMusicClient/")
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--lyrics-only",
        action="store_true",
        help="Fetch exact-identity lyrics without downloading audio or modifying inventory",
    )
    parser.add_argument(
        "--lyrics-receipt",
        type=Path,
        help="Append-only JSONL receipt destination (defaults beside --progress)",
    )
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--search-size", type=int, default=3)
    parser.add_argument(
        "--item-timeout-seconds",
        type=float,
        default=60.0,
        help="Maximum seconds for one musicdl search or download operation",
    )
    args = parser.parse_args()
    queue = args.queue.expanduser()
    work_dir = args.work_dir.expanduser()
    progress = args.progress.expanduser()
    log = args.log.expanduser()
    lyrics_receipt = args.lyrics_receipt.expanduser() if args.lyrics_receipt else progress.parent / "lyrics-receipts.jsonl"
    if args.lyrics_only:
        run_lyrics_only(
            queue,
            work_dir,
            progress,
            log,
            args.run_id,
            args.max_items,
            args.dry_run,
            args.delay,
            args.retries,
            args.search_size,
            args.item_timeout_seconds,
            lyrics_receipt,
        )
        return 0
    if args.inventory is None:
        parser.error("--inventory is required unless --lyrics-only is used")
    run_download(
        queue,
        args.inventory.expanduser(),
        work_dir,
        progress,
        log,
        args.run_id,
        args.max_items,
        args.dry_run,
        args.delay,
        args.retries,
        args.search_size,
        args.item_timeout_seconds,
        lyrics_receipt,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
