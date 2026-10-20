"""Versioned audience-chart producer: policy, matching, and chart evidence.

This module ports the normalization / matching / ambiguity rules from the
2026-10-20 audience experiment (``research/audience-137/scripts/match_and_label.py``)
into the plugin so the weekly pipeline can run them deterministically:

* chart capture stays on ``scripts/capture_kugou_chart.py`` (kugou-cli); the
  throwaway ``curl`` snapshot script is deliberately **not** ported;
* one chart snapshot becomes in-memory ``chart_evidence`` JSONL records for
  ``MusicKBRepository.import_audience(kind="chart_evidence")``, never a raw
  chart table;
* ambiguous entries (title matched, artist did not) are written to a review
  queue and never auto-labelled.

Offline only: every public function takes injected paths / connections so the
test suite can use synthetic capture payloads.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import ValidationError
from .normalization import require_text
from .operation_context import atomic_write_json
from .repository import (
    CHART_EVIDENCE_METHOD_ID,
    CHART_EVIDENCE_METHOD_VERSION,
    CHART_EVIDENCE_TARGET_PLATFORM,
    CHART_EVIDENCE_TIME_WINDOW,
    MusicKBRepository,
    iter_audience_file,
)


DEFAULT_AUDIENCE_CHART_PROFILE = (
    Path(__file__).resolve().parents[2] / "references" / "kugou-audience-charts-v1.json"
)

AUDIENCE_CHART_PROFILE_SCHEMA_VERSION = 1
AUDIENCE_CHART_COUNT = 24
AUDIENCE_CHART_FAMILIES = ("generation", "scene", "tribe")
AUDIENCE_CHART_FAMILY_COUNTS = {"generation": 3, "scene": 7, "tribe": 14}
AUDIENCE_CHART_CAPTURE_MODES = frozenset({"own", "reuse"})
_FAMILY_DIMENSIONS = {
    "generation": "generation_hint",
    "scene": "scene",
    "tribe": "tribe",
}

# Spec v2.1 §7 "chart→bucket mapping（写死）".  The versioned policy must
# declare exactly this mapping; the snapshot date is joined at import time.
CHART_BUCKETS: dict[str, str] = {
    "49224": "gen/00后",
    "49223": "gen/90后",
    "49225": "gen/80后",
    "52144": "scene/shortvideo",
    "52767": "scene/shortvideo",
    "24971": "scene/dj",
    "46910": "scene/variety",
    "33163": "scene/soundtrack",
    "37361": "scene/recognition",
    "100530": "scene/channels",
    "33162": "tribe/acg",
    "33160": "tribe/electronic",
    "44412": "tribe/rap",
    "51341": "tribe/folk",
    "59896": "tribe/rock",
    "59895": "tribe/rnb",
    "59897": "tribe/jazz",
    "59898": "tribe/country",
    "59899": "tribe/classical",
    "85897": "tribe/guofeng",
    "80025": "tribe/guofeng",
    "108332": "tribe/opera",
    "108333": "tribe/opera",
    "108335": "tribe/opera",
}

# One capture entry keeps at most four pages per chart; the 24 v1 charts are
# one page at the current sizes, but the bound keeps a misbehaving API finite.
CAPTURE_MAX_PAGES = 5
CAPTURE_SONGS_SUFFIX = "-songs.json"
AUDIENCE_CAPTURE_MANIFEST = "audience-charts-manifest.json"

_PUNCTUATION = re.compile(r"[\s\-_·’'\"“”‘’.,，。!！?？:：;；()（）\[\]【】<>《》&+~～|/\\]+")
_VERSION_PAREN = re.compile(r"[\(（\[【][^\)）\]】]{0,40}[\)）\]】]")
_VERSION_WORD = re.compile(
    r"(live|remix|dj版|女版|男版|深情版|温柔版|节奏版|翻唱|伴奏|纯音乐|剪辑版|片段|加速版|慢速版|升调|降调)",
    flags=re.IGNORECASE,
)
_ARTIST_SPLIT = re.compile(r"[/、,，&;；]+")


# -- normalization (ported from the 2026-10-20 experiment) -------------------


def normalize_title(value: Any) -> str:
    """Case-fold and strip punctuation so chart/KB titles compare equal."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    return _PUNCTUATION.sub("", text.casefold())


def base_title(value: Any) -> str:
    """Normalize a title after dropping version qualifiers and edition words."""

    text = _VERSION_PAREN.sub("", str(value or ""))
    text = _VERSION_WORD.sub("", text)
    return normalize_title(text)


def artist_set(value: Any) -> set[str]:
    """Split a slash/comma joined artist credit into normalized names."""

    artists: set[str] = set()
    for part in _ARTIST_SPLIT.split(str(value or "")):
        normalized_part = normalize_title(part)
        if normalized_part:
            artists.add(normalized_part)
    return artists


# -- versioned policy --------------------------------------------------------


def _profile_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"audience chart profile requires non-empty {field}")
    return value


def _profile_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"audience chart profile requires positive integer {field}")
    return value


def load_audience_chart_profile(
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and strictly validate ``kugou-audience-charts-v1.json``."""

    profile_path = Path(path).expanduser().resolve() if path is not None else DEFAULT_AUDIENCE_CHART_PROFILE
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"Unable to read audience chart profile: {profile_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Audience chart profile is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("Audience chart profile must be a JSON object")
    if payload.get("schema_version") != AUDIENCE_CHART_PROFILE_SCHEMA_VERSION:
        raise ValidationError(
            "Audience chart profile requires schema_version="
            f"{AUDIENCE_CHART_PROFILE_SCHEMA_VERSION}",
            details={"schema_version": payload.get("schema_version")},
        )
    if _profile_text(payload.get("platform"), "platform").casefold() != "kugou":
        raise ValidationError("Audience chart profile only supports the kugou platform")
    page_size = _profile_positive_int(payload.get("page_size"), "page_size")
    if page_size > 500:
        raise ValidationError("Audience chart profile page_size must be at most 500")
    max_pages = _profile_positive_int(payload.get("max_pages"), "max_pages")
    if max_pages > CAPTURE_MAX_PAGES:
        raise ValidationError(
            "Audience chart profile max_pages exceeds the supported bound",
            details={"max_pages": max_pages, "bound": CAPTURE_MAX_PAGES},
        )
    _profile_text(payload.get("termination"), "termination")
    charts = payload.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ValidationError("Audience chart profile requires a non-empty charts array")

    validated: list[dict[str, Any]] = []
    seen_rank_ids: set[str] = set()
    family_counts = {family: 0 for family in AUDIENCE_CHART_FAMILIES}
    for index, chart in enumerate(charts):
        if not isinstance(chart, Mapping):
            raise ValidationError(f"Audience chart profile charts[{index}] must be an object")
        rank_id = str(chart.get("rank_id") or "").strip()
        if not rank_id.isdigit() or int(rank_id) <= 0:
            raise ValidationError(
                f"Audience chart profile charts[{index}].rank_id must be a positive integer",
                details={"rank_id": chart.get("rank_id")},
            )
        if rank_id in seen_rank_ids:
            raise ValidationError(
                "Audience chart profile rank_id must be unique",
                details={"rank_id": rank_id},
            )
        seen_rank_ids.add(rank_id)
        family = _profile_text(chart.get("family"), f"charts[{index}].family")
        if family not in AUDIENCE_CHART_FAMILIES:
            raise ValidationError(
                "Audience chart profile family must be generation, scene, or tribe",
                details={"rank_id": rank_id, "family": family},
            )
        family_counts[family] += 1
        capture = _profile_text(chart.get("capture"), f"charts[{index}].capture")
        if capture not in AUDIENCE_CHART_CAPTURE_MODES:
            raise ValidationError(
                "Audience chart profile capture must be own or reuse",
                details={"rank_id": rank_id, "capture": capture},
            )
        bucket = _profile_text(chart.get("bucket"), f"charts[{index}].bucket")
        expected_bucket = CHART_BUCKETS.get(rank_id)
        if expected_bucket is None:
            raise ValidationError(
                "Audience chart rank_id is not part of the v1 write-dead mapping",
                details={"rank_id": rank_id},
            )
        if bucket != expected_bucket:
            raise ValidationError(
                "Audience chart bucket does not match the spec §7 mapping",
                details={"rank_id": rank_id, "bucket": bucket, "expected": expected_bucket},
            )
        dimension = _profile_text(chart.get("dimension"), f"charts[{index}].dimension")
        if dimension != _FAMILY_DIMENSIONS[family]:
            raise ValidationError(
                "Audience chart dimension does not match its family",
                details={"rank_id": rank_id, "dimension": dimension, "family": family},
            )
        validated.append(
            {
                "rank_id": rank_id,
                "chart_name": _profile_text(
                    chart.get("chart_name"), f"charts[{index}].chart_name"
                ),
                "family": family,
                "update_frequency": _profile_text(
                    chart.get("update_frequency"), f"charts[{index}].update_frequency"
                ),
                "official_intro": _profile_text(
                    chart.get("official_intro"), f"charts[{index}].official_intro"
                ),
                "capture": capture,
                "dimension": dimension,
                "bucket": bucket,
            }
        )

    if len(validated) != AUDIENCE_CHART_COUNT:
        raise ValidationError(
            "Audience chart profile must declare exactly 24 audience charts",
            details={"chart_count": len(validated)},
        )
    if set(seen_rank_ids) != set(CHART_BUCKETS):
        missing = sorted(set(CHART_BUCKETS) - seen_rank_ids)
        unexpected = sorted(seen_rank_ids - set(CHART_BUCKETS))
        raise ValidationError(
            "Audience chart profile rank_id set does not match the v1 mapping",
            details={"missing": missing, "unexpected": unexpected},
        )
    if family_counts != AUDIENCE_CHART_FAMILY_COUNTS:
        raise ValidationError(
            "Audience chart profile family counts must be 3 generation / 7 scene / 14 tribe",
            details={"family_counts": family_counts},
        )
    declared_count = payload.get("chart_count")
    if declared_count is not None and declared_count != AUDIENCE_CHART_COUNT:
        raise ValidationError(
            "Audience chart profile chart_count does not match the charts array",
            details={"chart_count": declared_count},
        )
    return {**payload, "charts": validated, "family_counts": dict(family_counts)}


# -- capture snapshot reading ------------------------------------------------


def _page_number(path: Path) -> int:
    match = re.search(r"-p(\d+)-songs\.json$", path.name)
    return int(match.group(1)) if match else 0


def chart_capture_files(directory: str | Path, rank_id: str) -> list[Path]:
    """Return the normalized ``-songs.json`` pages for one chart, ordered."""

    base = Path(directory)
    if not base.is_dir():
        return []
    files = [
        path
        for path in base.glob(f"chart-{rank_id}-p*{CAPTURE_SONGS_SUFFIX}")
        if path.is_file() and path.name.startswith(f"chart-{rank_id}-p")
    ]
    return sorted(files, key=_page_number)


def _chart_rank(song: Mapping[str, Any]) -> int | None:
    appearances = song.get("chart_appearances")
    if not isinstance(appearances, Sequence) or isinstance(appearances, (str, bytes)):
        return None
    ranks = [
        item.get("rank")
        for item in appearances
        if isinstance(item, Mapping)
        and isinstance(item.get("rank"), int)
        and not isinstance(item.get("rank"), bool)
        and int(item.get("rank")) >= 1
    ]
    return min(ranks) if ranks else None


def load_chart_capture(
    *,
    chart_directory: str | Path | None,
    profile: Mapping[str, Any] | None = None,
    reuse_directory: str | Path | None = None,
    rank_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Read normalized capture output for the audience charts.

    ``capture: reuse`` charts (52144) may live in the main ``run_dir/charts``
    directory instead of ``run_dir/audience-charts``; the audience directory is
    always preferred when both exist.
    """

    chart_profile = dict(profile) if profile is not None else load_audience_chart_profile()
    selected = {str(rank_id) for rank_id in rank_ids} if rank_ids else None
    entries: list[dict[str, Any]] = []
    charts: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    reused: list[str] = []
    source_directories = [Path(chart_directory)] if chart_directory is not None else []
    for chart in chart_profile["charts"]:
        rank_id = str(chart["rank_id"])
        if selected is not None and rank_id not in selected:
            continue
        files: list[Path] = []
        used_reuse = False
        for directory in source_directories:
            files = chart_capture_files(directory, rank_id)
            if files:
                break
        if not files and chart["capture"] == "reuse" and reuse_directory is not None:
            files = chart_capture_files(reuse_directory, rank_id)
            used_reuse = bool(files)
        if not files:
            missing.append(rank_id)
            continue
        captured_at = ""
        best: dict[str, dict[str, Any]] = {}
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValidationError(
                    f"Unable to read normalized chart capture: {path}",
                    details={"rank_id": rank_id},
                ) from exc
            if not isinstance(payload, Mapping):
                raise ValidationError(
                    "Normalized chart capture must be a JSON object",
                    details={"path": str(path)},
                )
            if payload.get("platform") not in (None, "kugou"):
                raise ValidationError(
                    "Normalized chart capture must come from the kugou platform",
                    details={"path": str(path)},
                )
            summary = payload.get("summary")
            summary = summary if isinstance(summary, Mapping) else {}
            summary_captured_at = str(summary.get("captured_at") or "").strip()
            if summary_captured_at:
                captured_at = max(captured_at, summary_captured_at)
            songs = payload.get("songs")
            if not isinstance(songs, list):
                raise ValidationError(
                    "Normalized chart capture must carry a songs array",
                    details={"path": str(path)},
                )
            for song in songs:
                if not isinstance(song, Mapping):
                    continue
                rank = _chart_rank(song)
                title = str(song.get("title") or "").strip()
                artist = str(song.get("artist") or "").strip()
                if rank is None or not title or not artist:
                    continue
                track_key = str(song.get("platform_track_key") or "").strip() or None
                identity = track_key or song.get("title_artist_key") or f"{title}\x00{artist}"
                previous = best.get(str(identity))
                if previous is None or rank < previous["rank"]:
                    best[str(identity)] = {
                        "rank_id": rank_id,
                        "chart_name": chart["chart_name"],
                        "family": chart["family"],
                        "dimension": chart["dimension"],
                        "bucket": chart["bucket"],
                        "rank": rank,
                        "title": title,
                        "artist": artist,
                        "platform_track_key": track_key,
                        "play_link": song.get("play_link"),
                        "capture_path": str(path),
                        "captured_at": summary_captured_at,
                    }
        if not best:
            missing.append(rank_id)
            continue
        if used_reuse:
            reused.append(rank_id)
        charts[rank_id] = {
            "rank_id": rank_id,
            "chart_name": chart["chart_name"],
            "family": chart["family"],
            "dimension": chart["dimension"],
            "bucket": chart["bucket"],
            "files": [str(path) for path in files],
            "captured_at": captured_at,
            "reused": used_reuse,
        }
        entries.extend(best.values())
    return {
        "profile": chart_profile,
        "entries": entries,
        "charts": charts,
        "missing": missing,
        "reused": reused,
    }


# -- matching (ported from the 2026-10-20 experiment) ------------------------


def _recording_index(connection: Any) -> dict[str, Any]:
    by_mix: dict[str, str] = {}
    by_title: dict[str, list[dict[str, Any]]] = {}
    by_base: dict[str, list[dict[str, Any]]] = {}
    rows = connection.execute(
        """
        SELECT r.id, r.canonical_title, r.version_label,
               (SELECT group_concat(a.canonical_name, '/')
                FROM recording_artist ra
                JOIN artist a ON a.id = ra.artist_id
                WHERE ra.recording_id = r.id) AS artists
        FROM recording r
        WHERE r.canonical_analysis_id IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        title = str(row["canonical_title"] or "")
        artists = str(row["artists"] or "")
        item = {
            "recording_id": str(row["id"]),
            "title": title,
            "version_label": str(row["version_label"] or ""),
            "artists": artists,
            "artist_set": artist_set(artists),
        }
        title_key = normalize_title(title)
        if title_key:
            by_title.setdefault(title_key, []).append(item)
        base_key = base_title(title)
        if base_key:
            by_base.setdefault(base_key, []).append(item)
    mix_rows = connection.execute(
        """
        SELECT st.recording_id, st.source_track_id
        FROM source_track st
        JOIN recording r ON r.id = st.recording_id
        WHERE st.source_name = 'kugou' AND r.canonical_analysis_id IS NOT NULL
        """
    ).fetchall()
    for row in mix_rows:
        source_track_id = str(row["source_track_id"] or "").strip()
        if not source_track_id:
            continue
        recording_id = str(row["recording_id"])
        by_mix.setdefault(source_track_id, recording_id)
        if source_track_id.startswith("kugou-"):
            by_mix.setdefault(source_track_id[len("kugou-"):], recording_id)
    return {"by_mix": by_mix, "by_title": by_title, "by_base": by_base}


def match_chart_entries(connection: Any, entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Match chart entries to canonical recordings in MixSongID → title order."""

    index = _recording_index(connection)
    matches: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    methods: dict[str, int] = {}
    matched_recordings: set[str] = set()
    matched_entries = 0
    for entry in entries:
        track_key = str(entry.get("platform_track_key") or "").strip()
        title = str(entry.get("title") or "")
        artist = str(entry.get("artist") or "")
        if track_key and track_key in index["by_mix"]:
            recording_ids = [index["by_mix"][track_key]]
            method = "mix_song_id"
        else:
            title_key = normalize_title(title)
            candidates = list(index["by_title"].get(title_key) or [])
            method = "exact_title"
            if not candidates:
                base_key = base_title(title)
                candidates = list(index["by_base"].get(base_key) or [])
                method = "base_title"
            if not candidates:
                unmatched.append(
                    {
                        "rank_id": entry.get("rank_id"),
                        "chart_name": entry.get("chart_name"),
                        "rank": entry.get("rank"),
                        "title": title,
                        "artist": artist,
                        "reason": "no normalized title candidate in the canonical library",
                    }
                )
                continue
            artist_names = artist_set(artist)
            artist_hits = [
                candidate for candidate in candidates if candidate["artist_set"] & artist_names
            ]
            if not artist_hits:
                ambiguous.append(
                    {
                        "rank_id": entry.get("rank_id"),
                        "chart_name": entry.get("chart_name"),
                        "rank": entry.get("rank"),
                        "title": title,
                        "artist": artist,
                        "candidate_recordings": [
                            candidate["recording_id"] for candidate in candidates
                        ],
                        "reason": "title matched but artist did not",
                    }
                )
                continue
            recording_ids = [candidate["recording_id"] for candidate in artist_hits]
            method = f"{method}+artist"
        matched_entries += 1
        matched_recordings.update(recording_ids)
        methods[method] = methods.get(method, 0) + 1
        matches.append(
            {
                **dict(entry),
                "recording_ids": recording_ids,
                "matched_by": method,
            }
        )
    return {
        "matches": matches,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "stats": {
            "entries": matched_entries + len(ambiguous) + len(unmatched),
            "matched_entries": matched_entries,
            "matched_recordings": len(matched_recordings),
            "ambiguous": len(ambiguous),
            "unmatched": len(unmatched),
            "methods": dict(sorted(methods.items())),
        },
    }


# -- chart evidence records --------------------------------------------------


def chart_evidence_confidence(charts: Sequence[Mapping[str, Any]]) -> str:
    """Spec §5 v1 rule: one chart=low; 2–3=medium; ≥4 or gen2+scene/tribe=high."""

    count = len(charts)
    generations = sum(1 for chart in charts if str(chart.get("family")) == "generation")
    other_families = sum(
        1 for chart in charts if str(chart.get("family")) in {"scene", "tribe"}
    )
    if count >= 4 or (generations >= 2 and other_families >= 1):
        return "high"
    if count >= 2:
        return "medium"
    return "low"


def build_chart_evidence_records(
    matches: Iterable[Mapping[str, Any]],
    *,
    charts: Mapping[str, Mapping[str, Any]],
    snapshot_date: str,
) -> list[dict[str, Any]]:
    """Collapse matched chart entries into one chart-snapshot profile per song."""

    require_text(snapshot_date, "snapshot_date")
    best: dict[str, dict[str, dict[str, Any]]] = {}
    for match in matches:
        rank_id = str(match.get("rank_id") or "")
        chart = charts.get(rank_id)
        if chart is None:
            continue
        rank = int(match["rank"])
        for recording_id in match.get("recording_ids", []):
            per_recording = best.setdefault(str(recording_id), {})
            previous = per_recording.get(rank_id)
            if previous is None or rank < previous["rank"]:
                per_recording[rank_id] = {
                    "rank_id": rank_id,
                    "rank": rank,
                    "chart_name": chart["chart_name"],
                    "family": chart["family"],
                    "dimension": chart["dimension"],
                    "bucket": chart["bucket"],
                    "captured_at": chart.get("captured_at"),
                    "local_path": (chart.get("files") or [""])[0],
                }
    records: list[dict[str, Any]] = []
    for recording_id in sorted(best):
        hits = sorted(
            best[recording_id].values(),
            key=lambda hit: (str(hit["family"]), int(hit["rank_id"]), hit["rank"]),
        )
        sources = []
        observations = []
        for hit in hits:
            source_key = f"chart:{hit['rank_id']}:{snapshot_date}"
            sources.append(
                {
                    "source_platform": "kugou",
                    "source_key": source_key,
                    "title": f"酷狗 {hit['chart_name']}",
                    "source_type": "chart_snapshot",
                    "fetched_at": hit["captured_at"] or snapshot_date,
                    "evidence_level": "1_platform_public",
                    "coverage_note": "榜内热度≠人口构成",
                    "local_path": hit["local_path"],
                }
            )
            observations.append(
                {
                    "dimension": hit["dimension"],
                    "bucket": hit["bucket"],
                    "rank": hit["rank"],
                    "evidence_level": "3_proxy",
                    "is_proxy": 1,
                    "source_id": source_key,
                }
            )
        confidence = chart_evidence_confidence(hits)
        records.append(
            {
                "kind": "chart_evidence",
                "recording": {"recording_id": recording_id},
                "profile": {
                    "target_platform": CHART_EVIDENCE_TARGET_PLATFORM,
                    "time_window": CHART_EVIDENCE_TIME_WINDOW,
                    "method_id": CHART_EVIDENCE_METHOD_ID,
                    "method_version": CHART_EVIDENCE_METHOD_VERSION,
                    "status": "available",
                    "confidence": confidence,
                    "is_proxy": 1,
                    "researched_at": snapshot_date,
                    "coverage_note": f"榜单证据：单期快照命中 {len(hits)} 榜",
                },
                "sources": sources,
                "observations": observations,
            }
        )
    return records


# -- orchestration used by weekly-run / weekly-update ------------------------


def _manifest_snapshot_date(chart_directory: Path | None) -> str | None:
    if chart_directory is None:
        return None
    manifest = chart_directory / AUDIENCE_CAPTURE_MANIFEST
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("snapshot_date") if isinstance(payload, Mapping) else None
    return str(value).strip() if value else None


def _derived_snapshot_date(capture: Mapping[str, Any], fallback: str) -> str:
    captured = [
        str(chart.get("captured_at") or "")
        for chart in capture.get("charts", {}).values()
    ]
    for value in sorted(captured, reverse=True):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            continue
    return fallback


def _audience_tag_count(repository: MusicKBRepository) -> int:
    row = repository.connection.execute(
        """
        SELECT COUNT(DISTINCT rt.tag_id)
        FROM recording_tag rt
        JOIN tag t ON t.id = rt.tag_id
        WHERE rt.role IN ('audience', 'audience-hint')
           OR t.namespace IN ('audience', 'audience-hint')
        """
    ).fetchone()
    return int(row[0] if row is not None else 0)


def import_audience_inputs(
    *,
    database: str | Path,
    chart_directory: str | Path | None = None,
    chart_profile: str | Path | None = None,
    audience_profiles: str | Path | None = None,
    delivery_resume: bool = False,
    snapshot_date: str | None = None,
    review_queue_path: str | Path | None = None,
) -> dict[str, Any]:
    """Match chart captures, import research profiles, and report coverage.

    ``import_audience`` owns the four audience tables and (per the #140 seam)
    the tag projection on its success path; this function never calls a
    projection API directly, so it also runs before #140 lands.

    Missing inputs are coverage gaps, not failures: the caller records them and
    keeps publishing. Per-record import errors are returned in the stats
    instead of aborting the batch.
    """

    coverage_gaps: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "chart_evidence": {
            "status": "skipped",
            "reason": "",
            "charts": 0,
            "entries": 0,
            "matched_entries": 0,
            "matched_recordings": 0,
            "ambiguous": 0,
            "unmatched": 0,
            "methods": {},
            "import": None,
        },
        "profiles": {"status": "skipped", "reason": "", "path": None, "import": None},
        "tags": 0,
        "coverage_gaps": coverage_gaps,
    }
    chart_records: list[dict[str, Any]] = []
    capture: dict[str, Any] | None = None
    chart_path = Path(chart_directory).expanduser().resolve() if chart_directory else None
    if delivery_resume:
        reason = "delivery resume imports research profiles only; no chart snapshot is re-read"
        result["chart_evidence"].update({"status": "skipped", "reason": reason})
        coverage_gaps.append({"source": "chart_evidence", "reason": reason})
    elif chart_path is None or not chart_path.is_dir():
        reason = "no audience chart capture directory supplied"
        result["chart_evidence"].update({"status": "skipped", "reason": reason})
        coverage_gaps.append({"source": "chart_evidence", "reason": reason})
    else:
        profile = load_audience_chart_profile(chart_profile)
        capture = load_chart_capture(
            chart_directory=chart_path,
            profile=profile,
            reuse_directory=chart_path.parent / "charts",
        )
        if not capture["entries"]:
            reason = f"no normalized audience chart entries found under {chart_path}"
            result["chart_evidence"].update({"status": "skipped", "reason": reason})
            coverage_gaps.append({"source": "chart_evidence", "reason": reason})
        else:
            resolved_date = (
                str(snapshot_date).strip()
                if snapshot_date
                else _manifest_snapshot_date(chart_path) or _derived_snapshot_date(capture, "")
            )
            if not resolved_date:
                resolved_date = datetime.now().astimezone().date().isoformat()
            with MusicKBRepository(database, read_only=True) as repository:
                matched = match_chart_entries(repository.connection, capture["entries"])
                chart_records = build_chart_evidence_records(
                    matched["matches"],
                    charts=capture["charts"],
                    snapshot_date=resolved_date,
                )
                stats = dict(matched["stats"])
                stats["matched_recordings"] = len(chart_records)
                stats["charts"] = len(capture["charts"])
                stats["reused_charts"] = list(capture["reused"])
                stats["missing_charts"] = list(capture["missing"])
                stats["snapshot_date"] = resolved_date
            queue_target = (
                Path(review_queue_path).expanduser().resolve()
                if review_queue_path is not None
                else chart_path / f"audience-review-queue-{resolved_date}.json"
            )
            atomic_write_json(
                queue_target,
                {
                    "schema_version": 1,
                    "snapshot_date": resolved_date,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "counts": stats,
                    "ambiguous": matched["ambiguous"],
                    "note": "歌名命中但歌手不匹配的条目；不自动打标，需人工复核后再决定。",
                },
            )
            result["chart_evidence"].update(
                {
                    "status": "matched",
                    **stats,
                    "review_queue": str(queue_target),
                }
            )

    profile_path = Path(audience_profiles).expanduser().resolve() if audience_profiles else None
    if profile_path is None:
        reason = "no --audience research profile JSONL supplied"
        result["profiles"].update({"status": "skipped", "reason": reason})
        coverage_gaps.append({"source": "research_profiles", "reason": reason})
    elif not profile_path.is_file():
        reason = f"audience research profile JSONL does not exist: {profile_path}"
        result["profiles"].update({"status": "skipped", "reason": reason, "path": str(profile_path)})
        coverage_gaps.append({"source": "research_profiles", "reason": reason})
    else:
        result["profiles"].update({"status": "imported", "path": str(profile_path)})

    imported_anything = bool(chart_records) or result["profiles"]["status"] == "imported"
    if imported_anything:
        with MusicKBRepository(database, read_only=False) as repository:
            if chart_records:
                chart_import = repository.import_audience(chart_records, kind="chart_evidence")
                result["chart_evidence"]["status"] = "imported"
                result["chart_evidence"]["import"] = chart_import
            if result["profiles"]["status"] == "imported":
                profile_import = repository.import_audience(
                    iter_audience_file(profile_path), kind="profile"
                )
                result["profiles"]["import"] = profile_import
            result["tags"] = _audience_tag_count(repository)
    result["status"] = "succeeded" if imported_anything else "skipped"
    return result
