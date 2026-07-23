from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .campaign_delivery import CampaignDeliveryEntry, group_campaign_delivery, to_import_payload
from .errors import NotFoundError, ValidationError
from .lyrics import (
    LYRIC_NORMALIZER_VERSION,
    LYRIC_STATUS_AVAILABLE,
    LYRIC_STATUS_INSTRUMENTAL,
    LYRIC_STATUS_PENDING,
    LYRIC_STATUS_PLATFORM_UNAVAILABLE,
    LYRIC_STATUSES,
    LYRIC_TERMINAL_STATUSES,
    is_publishable_lyric_text,
    lyric_text_sha256,
    load_lyric_receipts,
    normalize_lyric_text,
)
from .normalization import fts_query, normalized, require_text
from .retrieval import (
    CandidateEvidence,
    CandidateSelection,
    DIVERSITY_NAMESPACES,
    EvidenceTag,
    select_representative_candidates,
)
from .schema import SCHEMA_VERSION, connect, ensure_initialized
from .tagging import PARSER_SOURCE, extract_music_flamingo_metadata


MAX_SEARCH_LIMIT = 50
MAX_FACET_LIMIT = 100
DEFAULT_SEARCH_FACETS_PER_NAMESPACE = 5
DEFAULT_DISCOVERY_FACETS_PER_NAMESPACE = 20
REPRESENTATIVE_POOL_SIZE = 50
DEFAULT_ENRICH_BATCH_SIZE = 500
DEFAULT_IMPORT_BATCH_SIZE = 500
IMPORT_LOOKUP_CACHE_SIZE = 8_192
MAX_IMPORT_RESULT_SAMPLE = 1_000
SEARCH_PROJECTION_STATE_KEY = "search_projection_state"
SEARCH_PROJECTION_CURRENT = "current"
SEARCH_PROJECTION_DIRTY = "dirty"


def _optional_source_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("source_track.source_url must be an absolute http(s) URL")
    return text


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class _BoundedImportLookupCache:
    """Bound repeated generic-import identity lookups without retaining a corpus.

    Generic payloads commonly repeat a compact controlled-tag vocabulary and a
    relatively small artist catalogue, while title tags are mostly unique. An
    LRU lets the hot vocabulary avoid thousands of identical upsert/select
    round trips but evicts old title keys, preserving the importer's bounded
    memory promise at 100k scale.
    """

    def __init__(self, *, limit: int = IMPORT_LOOKUP_CACHE_SIZE) -> None:
        self.limit = limit
        self._namespaces: OrderedDict[str, None] = OrderedDict()
        self._artists: OrderedDict[tuple[str, tuple[str, ...]], str] = OrderedDict()
        self._tags: OrderedDict[tuple[str, str, tuple[str, ...], str, str, bool], int] = OrderedDict()

    @staticmethod
    def _get(values: OrderedDict[Any, Any], key: Any) -> Any:
        value = values.pop(key, None)
        if value is not None:
            values[key] = value
        return value

    def artist(self, key: tuple[str, tuple[str, ...]]) -> str | None:
        return self._get(self._artists, key)

    def tag(self, key: tuple[str, str, tuple[str, ...], str, str, bool]) -> int | None:
        return self._get(self._tags, key)

    def has_namespace(self, namespace: str) -> bool:
        if namespace not in self._namespaces:
            return False
        self._namespaces.move_to_end(namespace)
        return True

    def remember_namespace(self, namespace: str) -> None:
        self._namespaces[namespace] = None
        if len(self._namespaces) > self.limit:
            self._namespaces.popitem(last=False)

    def _put(self, values: OrderedDict[Any, Any], key: Any, value: Any) -> None:
        values[key] = value
        if len(values) > self.limit:
            values.popitem(last=False)

    def remember_artist(self, key: tuple[str, tuple[str, ...]], artist_id: str) -> None:
        self._put(self._artists, key, artist_id)

    def remember_tag(self, key: tuple[str, str, tuple[str, ...], str, str, bool], tag_id: int) -> None:
        self._put(self._tags, key, tag_id)


class MusicKBRepository:
    """All database operations. MCP uses only its read methods."""

    def __init__(
        self, database: str | Path, *, read_only: bool = False, allow_snapshot_write: bool = False
    ) -> None:
        self.path = Path(database).expanduser().resolve()
        self.read_only = read_only
        self.allow_snapshot_write = allow_snapshot_write
        self.connection = connect(self.path, read_only=read_only)
        self._supports_returning = sqlite3.sqlite_version_info >= (3, 35, 0)
        try:
            ensure_initialized(self.connection)
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MusicKBRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _require_writer(self) -> None:
        if self.read_only:
            from .errors import ReadOnlyError

            raise ReadOnlyError("This operation requires the writable publisher database.")
        database_kind = self.connection.execute(
            "SELECT value FROM meta WHERE key = 'database_kind'"
        ).fetchone()
        if (
            database_kind is not None
            and str(database_kind["value"]) == "snapshot"
            and not self.allow_snapshot_write
        ):
            from .errors import ReadOnlyError

            raise ReadOnlyError("Client snapshots are never valid write targets; use the publisher master database.")

    # -- importer ---------------------------------------------------------

    def import_analysis(
        self,
        payload: Mapping[str, Any],
        *,
        _transactional: bool = True,
        _promote_existing: bool = True,
        _rebuild_projection: bool = True,
        _lookup_cache: _BoundedImportLookupCache | None = None,
    ) -> dict[str, Any]:
        """Import one immutable revision and make it canonical when eligible."""

        self._require_writer()
        if not isinstance(payload, Mapping):
            raise ValidationError("Each import record must be a JSON object")
        self._reject_feigua_context(payload)

        recording_data = payload.get("recording")
        if recording_data is None:
            recording_data = payload
        if not isinstance(recording_data, Mapping):
            raise ValidationError("recording must be an object")

        title = require_text(recording_data.get("title") or payload.get("title"), "recording.title")
        version_label = str(recording_data.get("version_label") or payload.get("version_label") or "").strip()
        audio_sha256 = str(recording_data.get("audio_sha256") or payload.get("audio_sha256") or "").strip() or None
        artists = self._parse_artists(payload)
        title_aliases = self._string_list(payload.get("title_aliases"))

        analysis = payload.get("analysis")
        if analysis is None:
            analysis = payload
        if not isinstance(analysis, Mapping):
            raise ValidationError("analysis must be an object")
        raw_text_value = analysis.get("raw_text") or payload.get("raw_text")
        if not isinstance(raw_text_value, str) or not raw_text_value.strip():
            raise ValidationError("analysis.raw_text must be a non-empty string")
        # Preserve the exact model output.  Campaign delivery hashes are over
        # UTF-8 bytes and must not silently change because of .strip().
        raw_text = raw_text_value
        summary = str(analysis.get("summary") or payload.get("summary") or "").strip()
        quality_state = str(analysis.get("quality_state") or payload.get("quality_state") or "passed").strip()
        if quality_state not in {"passed", "needs_review", "failed"}:
            raise ValidationError("analysis.quality_state must be passed, needs_review, or failed")
        generated_tokens = analysis.get("generated_token_count", payload.get("generated_token_count"))
        if generated_tokens is not None:
            try:
                generated_tokens = int(generated_tokens)
            except (TypeError, ValueError) as exc:
                raise ValidationError("analysis.generated_token_count must be an integer") from exc
            if generated_tokens < 0:
                raise ValidationError("analysis.generated_token_count cannot be negative")

        primary_artist = artists[0]["name"]
        recording_id = str(recording_data.get("id") or payload.get("recording_id") or "").strip()
        if not recording_id:
            recording_id = (
                _stable_id("rec", audio_sha256)
                if audio_sha256
                else _stable_id("rec", normalized(title), normalized(primary_artist), normalized(version_label))
            )
        recording_id = self._resolve_recording_id(recording_id, audio_sha256)
        output_sha256 = _sha256_text(raw_text)
        analysis_id = _stable_id("anl", recording_id, output_sha256)
        canonical_requested = bool(payload.get("canonical", True))
        if canonical_requested and quality_state != "passed":
            raise ValidationError("Only an analysis with quality_state='passed' can become canonical")

        tags = payload.get("tags") or []
        numeric_features = payload.get("numeric_features") or []
        source_tracks = payload.get("source_tracks") or []
        if not isinstance(tags, list) or not isinstance(numeric_features, list) or not isinstance(source_tracks, list):
            raise ValidationError("tags, numeric_features, and source_tracks must be arrays")
        # Validate the business boundary before duplicate short-circuiting so a
        # malformed retry is rejected rather than silently accepted as an
        # idempotent import.
        for tag in tags:
            self._validate_tag_boundary(tag)

        transaction = self.connection if _transactional else nullcontext()
        with transaction:
            existing = self.connection.execute(
                "SELECT id FROM analysis_revision WHERE recording_id = ? AND output_sha256 = ?",
                (recording_id, output_sha256),
            ).fetchone()
            if existing:
                existing_id = str(existing["id"])
                is_canonical = self._is_canonical(recording_id, existing_id)
                if canonical_requested and _promote_existing:
                    self._set_canonical(recording_id, existing_id)
                    if _rebuild_projection:
                        self.rebuild_search_projection(recording_id)
                    is_canonical = True
                return {
                    "recording_id": recording_id,
                    "analysis_id": existing_id,
                    "idempotent": True,
                    "canonical": is_canonical,
                }

            self._upsert_recording(recording_id, title, version_label, audio_sha256)
            self._upsert_title_aliases(recording_id, [title, *title_aliases])
            for position, artist in enumerate(artists):
                artist_id = self._upsert_artist(
                    artist["name"], artist["aliases"], lookup_cache=_lookup_cache
                )
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO recording_artist(recording_id, artist_id, role, position)
                    VALUES (?, ?, ?, ?)
                    """,
                    (recording_id, artist_id, artist["role"], position),
                )

            self._insert_identity_tags(
                recording_id, title, title_aliases, artists, lookup_cache=_lookup_cache
            )
            for source in source_tracks:
                self._upsert_source_track(recording_id, source)

            self.connection.execute(
                """
                INSERT INTO analysis_revision(
                    id, recording_id, raw_text, summary, model_version, prompt_version,
                    generated_token_count, quality_state, status, output_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                """,
                (
                    analysis_id,
                    recording_id,
                    raw_text,
                    summary,
                    str(analysis.get("model_version") or payload.get("model_version") or "").strip(),
                    str(analysis.get("prompt_version") or payload.get("prompt_version") or "").strip(),
                    generated_tokens,
                    quality_state,
                    output_sha256,
                ),
            )

            for tag_data in tags:
                tag_id, confidence = self._upsert_tag_from_payload(
                    tag_data, lookup_cache=_lookup_cache
                )
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO analysis_tag(analysis_id, tag_id, confidence, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        analysis_id,
                        tag_id,
                        confidence,
                        str(tag_data.get("source") or "model") if isinstance(tag_data, Mapping) else "model",
                    ),
                )

            for feature in numeric_features:
                self._upsert_numeric_feature(analysis_id, feature)

            if canonical_requested:
                self._set_canonical(recording_id, analysis_id)
            self.connection.execute(
                "UPDATE recording SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (recording_id,)
            )
            if _rebuild_projection:
                self.rebuild_search_projection(recording_id)

        return {
            "recording_id": recording_id,
            "analysis_id": analysis_id,
            "idempotent": False,
            "canonical": canonical_requested,
        }

    def import_campaign_delivery(
        self, entries: Sequence[CampaignDeliveryEntry]
    ) -> dict[str, Any]:
        """Import an already validated KuGou canonical delivery atomically.

        The JSONL parser validates every physical line before this method is
        called.  This method adds database-level conflict protection and keeps
        the generic canonical importer plus provenance row in one SQLite
        transaction, so a late conflict cannot leave a partial delivery in the
        master database.
        """

        self._require_writer()
        if not entries:
            raise ValidationError("Campaign delivery must contain at least one entry")
        groups = group_campaign_delivery(entries)
        imported: list[dict[str, Any]] = []
        with self.connection:
            for group in groups:
                primary = group[0]
                self._assert_campaign_source_bytes(primary)
                payload = to_import_payload(group)
                # A retry of already-imported delivery evidence must be
                # idempotent, not re-promote an older revision over a newer
                # canonical selection.
                result = self.import_analysis(
                    payload,
                    _transactional=False,
                    _promote_existing=False,
                    _rebuild_projection=False,
                )
                recording_id = str(result["recording_id"])
                analysis_id = str(result["analysis_id"])
                result["canonical"] = self._reconcile_campaign_analysis_identity(
                    recording_id, analysis_id, primary
                )
                if result["idempotent"]:
                    # The generic importer correctly avoids duplicating the
                    # analysis revision, but its early-return path has not
                    # seen a newly delivered source alias. Add that identity
                    # metadata explicitly before publishing the provenance.
                    self._ensure_campaign_group_identity_metadata(
                        recording_id, payload, rebuild_projection=False
                    )
                    self._replace_parser_metadata(analysis_id, primary.output_text)
                for entry in group:
                    self._assert_campaign_source_identity(recording_id, entry)
                row = self.connection.execute(
                    "SELECT output_sha256 FROM analysis_revision WHERE id = ?", (analysis_id,)
                ).fetchone()
                if row is None or str(row["output_sha256"]) != primary.output_text_sha256:
                    raise ValidationError(
                        f"Campaign delivery {primary.delivery_id} did not retain its verified output hash"
                    )
                provenance_idempotent = [
                    self._upsert_campaign_delivery_provenance(analysis_id, entry) for entry in group
                ]
                imported.append(
                    {
                        "delivery_id": primary.delivery_id,
                        "manifest_index": primary.manifest_index,
                        **result,
                        "source_track_count": len(group),
                        "delivery_ids": [entry.delivery_id for entry in group],
                        "provenance_idempotent": all(provenance_idempotent),
                    }
                )
            # Keep business rows and their FTS projection in the same outer
            # transaction. Per-record deletes are quadratic at 100k scale, but
            # a single full rebuild here rolls back the delivery as well if
            # FTS5 rejects an insert.
            self._rebuild_all_search_projections_in_transaction()
        return {
            "count": len(entries),
            "recording_count": len(imported),
            "source_track_count": len(entries),
            "imports": imported,
            "canonical_sources": sorted({entry.canonical_source for entry in entries}),
            "attempt_ids": sorted({entry.attempt_id for entry in entries}),
        }

    def import_analyses(
        self, payloads: Iterable[Mapping[str, Any]], *, batch_size: int = DEFAULT_IMPORT_BATCH_SIZE
    ) -> dict[str, Any]:
        """Import a possibly-streaming generic analysis sequence in bounded batches.

        The old CLI loop rebuilt the FTS projection for every individual input
        row. At 100k scale that repeatedly scanned FTS5's unindexed
        ``recording_id`` field. This method holds only one payload batch plus
        bounded lookup/compatibility-result caches in memory, commits each
        batch atomically, and rebuilds FTS once at the end.

        A failed later batch intentionally leaves earlier batches durable (the
        same resume behavior as the prior row-by-row CLI). The persisted search
        projection state is marked dirty before those writes, so validation
        blocks snapshot publication until the import is retried or a full
        rebuild completes.
        """

        self._require_writer()
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 5_000:
            raise ValidationError("batch_size must be an integer between 1 and 5000")

        count = 0
        created_count = 0
        idempotent_count = 0
        canonical_count = 0
        batch_count = 0
        batch: list[Mapping[str, Any]] = []
        lookup_cache = _BoundedImportLookupCache()
        import_results: list[dict[str, Any]] = []
        imports_truncated = False

        def import_batch(items: list[Mapping[str, Any]]) -> None:
            nonlocal count, created_count, idempotent_count, canonical_count, batch_count
            nonlocal imports_truncated
            with self.connection:
                self._set_search_projection_state(SEARCH_PROJECTION_DIRTY)
                for item in items:
                    result = self.import_analysis(
                        item,
                        _transactional=False,
                        _rebuild_projection=False,
                        _lookup_cache=lookup_cache,
                    )
                    count += 1
                    idempotent_count += int(bool(result["idempotent"]))
                    created_count += int(not bool(result["idempotent"]))
                    canonical_count += int(bool(result["canonical"]))
                    if len(import_results) < MAX_IMPORT_RESULT_SAMPLE:
                        import_results.append(result)
                    else:
                        imports_truncated = True
            batch_count += 1

        for payload in payloads:
            if not isinstance(payload, Mapping):
                raise ValidationError("Each import record must be a JSON object")
            batch.append(payload)
            if len(batch) >= batch_size:
                import_batch(batch)
                batch = []
        if batch:
            import_batch(batch)

        if count:
            self.rebuild_all_search_projections()
        return {
            "count": count,
            "created_count": created_count,
            "idempotent_count": idempotent_count,
            "canonical_count": canonical_count,
            "batch_size": batch_size,
            "batch_count": batch_count,
            "search_projection_rebuilt": bool(count),
            # Preserve the original CLI's per-record shape for ordinary small
            # imports without retaining a 100k-result object in memory.
            "imports": import_results,
            "imports_returned": len(import_results),
            "imports_truncated": imports_truncated,
        }

    def enrich_campaign_tags(
        self, *, dry_run: bool = False, batch_size: int = DEFAULT_ENRICH_BATCH_SIZE
    ) -> dict[str, Any]:
        """Derive versioned tags for every current campaign canonical analysis.

        This is a publisher-only, idempotent backfill for deliveries imported
        before the deterministic parser existed. It never touches historical
        revisions, manually supplied tag assignments, or client snapshots.
        """

        self._require_writer()
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 5_000:
            raise ValidationError("batch_size must be an integer between 1 and 5000")

        analysis_count = 0
        tag_assignment_count = 0
        numeric_feature_count = 0
        inserted_tags = 0
        inserted_features = 0
        namespace_counts: Counter[str] = Counter()
        unique_tags: set[tuple[str, str]] = set()
        batch_count = 0
        last_analysis_id = ""
        while True:
            rows = list(
                self.connection.execute(
                    """
                    SELECT ar.id AS analysis_id, ar.recording_id, ar.raw_text
                    FROM analysis_revision ar
                    JOIN recording r ON r.canonical_analysis_id = ar.id
                    WHERE ar.id > ?
                      AND EXISTS (
                          SELECT 1 FROM campaign_delivery_provenance c
                          WHERE c.analysis_id = ar.id
                      )
                    ORDER BY ar.id LIMIT ?
                    """,
                    (last_analysis_id, batch_size),
                )
            )
            if not rows:
                break
            batch_count += 1

            derived = []
            for row in rows:
                tags, numeric_features = extract_music_flamingo_metadata(str(row["raw_text"]))
                analysis_count += 1
                tag_assignment_count += len(tags)
                numeric_feature_count += len(numeric_features)
                namespace_counts.update(str(tag["namespace"]) for tag in tags)
                unique_tags.update((str(tag["namespace"]), str(tag["name"])) for tag in tags)
                if not dry_run:
                    derived.append((str(row["analysis_id"]), str(row["recording_id"]), tags, numeric_features))

            if not dry_run:
                # One bounded transaction per batch keeps memory stable at
                # 100k scale and makes interrupted backfills safely resumable.
                with self.connection:
                    self._set_search_projection_state(SEARCH_PROJECTION_DIRTY)
                    for analysis_id, recording_id, tags, numeric_features in derived:
                        tag_count, feature_count = self._replace_parser_metadata(
                            analysis_id, tags=tags, numeric_features=numeric_features
                        )
                        inserted_tags += tag_count
                        inserted_features += feature_count
            last_analysis_id = str(rows[-1]["analysis_id"])

        summary = {
            "analysis_count": analysis_count,
            "tag_assignment_count": tag_assignment_count,
            "unique_tag_count": len(unique_tags),
            "namespace_counts": dict(sorted(namespace_counts.items())),
            "numeric_feature_count": numeric_feature_count,
            "parser_source": PARSER_SOURCE,
            "batch_size": batch_size,
            "batch_count": batch_count,
        }
        if dry_run:
            return {"dry_run": True, **summary}
        # A full rebuild does one FTS delete then inserts each current
        # projection. Calling the single-record replace path for every row
        # repeatedly scans FTS5's unindexed recording_id column at 100k scale.
        self.rebuild_all_search_projections()
        return {
            "dry_run": False,
            **summary,
            "inserted_tag_assignment_count": inserted_tags,
            "inserted_numeric_feature_count": inserted_features,
        }

    def _replace_parser_metadata(
        self,
        analysis_id: str,
        raw_text: str | None = None,
        *,
        tags: Sequence[Mapping[str, Any]] | None = None,
        numeric_features: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[int, int]:
        """Replace only this parser version's assignments for one analysis."""

        if tags is None or numeric_features is None:
            if raw_text is None:
                raise ValueError("raw_text is required when parser metadata is not supplied")
            tags, numeric_features = extract_music_flamingo_metadata(raw_text)
        self.connection.execute(
            "DELETE FROM analysis_tag WHERE analysis_id = ? AND source = ?",
            (analysis_id, PARSER_SOURCE),
        )
        self.connection.execute(
            "DELETE FROM numeric_feature WHERE analysis_id = ? AND source = ?",
            (analysis_id, PARSER_SOURCE),
        )
        inserted_tags = 0
        for tag_data in tags:
            tag_id, confidence = self._upsert_tag_from_payload(tag_data)
            cursor = self.connection.execute(
                """
                INSERT INTO analysis_tag(analysis_id, tag_id, confidence, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(analysis_id, tag_id) DO UPDATE SET
                    confidence = excluded.confidence,
                    source = excluded.source
                WHERE analysis_tag.source = ?
                """,
                (analysis_id, tag_id, confidence, PARSER_SOURCE, PARSER_SOURCE),
            )
            if cursor.rowcount > 0:
                inserted_tags += 1

        inserted_features = 0
        for feature in numeric_features:
            if not isinstance(feature, Mapping):
                raise ValidationError("Each numeric feature must be an object")
            name = require_text(feature.get("name"), "numeric_feature.name")
            try:
                value = float(feature.get("value"))
            except (TypeError, ValueError) as exc:
                raise ValidationError("numeric_feature.value must be a number") from exc
            unit = str(feature.get("unit") or "").strip()
            confidence = feature.get("confidence")
            if confidence is not None:
                try:
                    confidence = float(confidence)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("numeric_feature.confidence must be a number") from exc
            source = str(feature.get("source") or PARSER_SOURCE).strip()
            if source != PARSER_SOURCE:
                raise ValidationError("Parser numeric features must use the current parser source")
            cursor = self.connection.execute(
                """
                INSERT INTO numeric_feature(analysis_id, name, value, unit, confidence, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(analysis_id, name) DO UPDATE SET
                    value = excluded.value,
                    unit = excluded.unit,
                    confidence = excluded.confidence,
                    source = excluded.source
                WHERE numeric_feature.source = ?
                """,
                (analysis_id, normalized(name), value, unit, confidence, source, PARSER_SOURCE),
            )
            if cursor.rowcount > 0:
                inserted_features += 1
        return inserted_tags, inserted_features

    def _ensure_campaign_group_identity_metadata(
        self, recording_id: str, payload: Mapping[str, Any], *, rebuild_projection: bool = True
    ) -> None:
        """Attach new source aliases when a campaign import is analysis-idempotent."""

        recording_data = payload["recording"]
        assert isinstance(recording_data, Mapping)
        title = require_text(recording_data.get("title"), "recording.title")
        title_aliases = self._string_list(payload.get("title_aliases"))
        artists = self._parse_artists(payload)
        self._upsert_title_aliases(recording_id, [title, *title_aliases])
        for position, artist in enumerate(artists):
            artist_id = self._upsert_artist(artist["name"], artist["aliases"])
            self.connection.execute(
                """
                INSERT OR IGNORE INTO recording_artist(recording_id, artist_id, role, position)
                VALUES (?, ?, ?, ?)
                """,
                (recording_id, artist_id, artist["role"], position),
            )
        self._insert_identity_tags(recording_id, title, title_aliases, artists)
        source_tracks = payload.get("source_tracks")
        assert isinstance(source_tracks, list)
        for source in source_tracks:
            self._upsert_source_track(recording_id, source)
        self.connection.execute(
            "UPDATE recording SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (recording_id,)
        )
        if rebuild_projection:
            self.rebuild_search_projection(recording_id)

    def _assert_campaign_source_bytes(self, entry: CampaignDeliveryEntry) -> None:
        """The same content SHA-256 cannot truthfully have different byte sizes."""

        row = self.connection.execute(
            """
            SELECT source_bytes FROM campaign_delivery_provenance
            WHERE source_sha256 = ? LIMIT 1
            """,
            (entry.source_sha256,),
        ).fetchone()
        if row is not None and int(row["source_bytes"]) != entry.source_bytes:
            raise ValidationError(
                "Campaign delivery source SHA-256 already has a different source_bytes value",
                details={
                    "source_sha256": entry.source_sha256,
                    "existing_source_bytes": int(row["source_bytes"]),
                    "incoming_source_bytes": entry.source_bytes,
                },
            )

    def _reconcile_campaign_analysis_identity(
        self, recording_id: str, analysis_id: str, entry: CampaignDeliveryEntry
    ) -> bool:
        """Fill unambiguous generic gaps or reject conflicting campaign identity.

        A generic import may have independently stored the exact raw output.
        It becomes campaign-backed only if its recording hash and generated
        token count agree with the verified delivery; missing values are safe
        to fill from the signed delivery, conflicting values are not.
        """

        row = self.connection.execute(
            """
            SELECT r.audio_sha256, r.canonical_analysis_id,
                   ar.generated_token_count, ar.quality_state
            FROM recording r JOIN analysis_revision ar ON ar.recording_id = r.id
            WHERE r.id = ? AND ar.id = ?
            """,
            (recording_id, analysis_id),
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"Campaign delivery {entry.delivery_id} did not retain its verified recording/analysis identity"
            )

        if str(row["quality_state"]) != "passed":
            raise ValidationError(
                "A verified campaign delivery cannot attach to an existing analysis that is not passed",
                details={
                    "analysis_id": analysis_id,
                    "quality_state": str(row["quality_state"]),
                    "delivery_id": entry.delivery_id,
                },
            )

        current_audio_sha256 = row["audio_sha256"]
        if current_audio_sha256 is None:
            owner = self.connection.execute(
                "SELECT id FROM recording WHERE audio_sha256 = ?", (entry.source_sha256,)
            ).fetchone()
            if owner is not None and str(owner["id"]) != recording_id:
                raise ValidationError(
                    "Campaign delivery source audio is already owned by a different recording",
                    details={
                        "existing_recording_id": str(owner["id"]),
                        "incoming_recording_id": recording_id,
                        "source_sha256": entry.source_sha256,
                    },
                )
            self.connection.execute(
                "UPDATE recording SET audio_sha256 = ? WHERE id = ?",
                (entry.source_sha256, recording_id),
            )
        elif str(current_audio_sha256) != entry.source_sha256:
            raise ValidationError(
                f"Campaign delivery {entry.delivery_id} is already associated with different source audio",
                details={
                    "recording_id": recording_id,
                    "existing_source_sha256": current_audio_sha256,
                    "incoming_source_sha256": entry.source_sha256,
                },
            )

        current_generated_token_count = row["generated_token_count"]
        if current_generated_token_count is None:
            self.connection.execute(
                "UPDATE analysis_revision SET generated_token_count = ? WHERE id = ?",
                (entry.generated_token_count, analysis_id),
            )
        elif int(current_generated_token_count) != entry.generated_token_count:
            raise ValidationError(
                f"Campaign delivery {entry.delivery_id} has a different generated_token_count than its existing analysis",
                details={
                    "analysis_id": analysis_id,
                    "existing_generated_token_count": int(current_generated_token_count),
                    "incoming_generated_token_count": entry.generated_token_count,
                },
            )

        current_canonical = str(row["canonical_analysis_id"] or "")
        if not current_canonical:
            # A passed generic revision with exactly the verified output is
            # safe to promote when no canonical exists. Do not re-promote an
            # older matching revision if a later campaign revision is current.
            self._set_canonical(recording_id, analysis_id)
            self.rebuild_search_projection(recording_id)
            return True
        return current_canonical == analysis_id

    def _assert_campaign_source_identity(
        self, recording_id: str, entry: CampaignDeliveryEntry
    ) -> None:
        """Ensure each retained KuGou source row maps back to its recording."""

        row = self.connection.execute(
            """
            SELECT st.recording_id, r.audio_sha256
            FROM source_track st JOIN recording r ON r.id = st.recording_id
            WHERE st.source_name = 'kugou' AND st.source_track_id = ?
            """,
            (entry.delivery_id,),
        ).fetchone()
        if (
            row is None
            or str(row["recording_id"]) != recording_id
            or str(row["audio_sha256"] or "") != entry.source_sha256
        ):
            raise ValidationError(
                f"Campaign delivery ID {entry.delivery_id} is not bound to its verified KuGou recording",
                details={
                    "expected_recording_id": recording_id,
                    "existing_recording_id": str(row["recording_id"]) if row is not None else None,
                    "existing_source_sha256": row["audio_sha256"] if row is not None else None,
                    "incoming_source_sha256": entry.source_sha256,
                },
            )

    def _upsert_campaign_delivery_provenance(
        self, analysis_id: str, entry: CampaignDeliveryEntry
    ) -> bool:
        """Insert immutable delivery evidence and return whether it was already present."""

        provenance_id = _stable_id(
            "delivery",
            str(entry.delivery_schema_version),
            entry.campaign_id,
            entry.delivery_id,
            str(entry.manifest_index),
            entry.relative_audio_path,
            entry.source_sha256,
            str(entry.source_bytes),
            entry.output_text_sha256,
            str(entry.generated_token_count),
            str(entry.max_new_tokens),
            entry.contract,
            entry.attempt_id,
            entry.canonical_source,
            entry.provenance_json or "",
        )
        expected = {
            "id": provenance_id,
            "delivery_schema_version": entry.delivery_schema_version,
            "campaign_id": entry.campaign_id,
            "delivery_id": entry.delivery_id,
            "analysis_id": analysis_id,
            "manifest_index": entry.manifest_index,
            "source_title": entry.title,
            "source_artist": entry.artist,
            "relative_audio_path": entry.relative_audio_path,
            "source_sha256": entry.source_sha256,
            "source_bytes": entry.source_bytes,
            "output_text_sha256": entry.output_text_sha256,
            "generated_token_count": entry.generated_token_count,
            "max_new_tokens": entry.max_new_tokens,
            "contract": entry.contract,
            "attempt_id": entry.attempt_id,
            "canonical_source": entry.canonical_source,
            "provenance_json": entry.provenance_json,
        }
        existing = self.connection.execute(
            """
            SELECT id, delivery_schema_version, campaign_id, delivery_id,
                   analysis_id, manifest_index, source_title, source_artist,
                   relative_audio_path,
                   source_sha256, source_bytes, output_text_sha256,
                   generated_token_count, max_new_tokens, contract, attempt_id,
                   canonical_source, provenance_json
            FROM campaign_delivery_provenance WHERE id = ?
            """,
            (provenance_id,),
        ).fetchone()
        if existing is not None:
            observed = {key: existing[key] for key in expected}
            if observed != expected:
                raise ValidationError(
                    f"Campaign delivery provenance conflict for {entry.delivery_id}; immutable evidence differs",
                    details={"expected": expected, "observed": observed},
                )
            return True

        index_owner = self.connection.execute(
            """
            SELECT delivery_id FROM campaign_delivery_provenance
            WHERE campaign_id = ? AND canonical_source = ?
              AND manifest_index = ? AND attempt_id = ?
            """,
            (
                entry.campaign_id,
                entry.canonical_source,
                entry.manifest_index,
                entry.attempt_id,
            ),
        ).fetchone()
        if index_owner is not None:
            raise ValidationError(
                "Campaign delivery provenance conflict: campaign_id + canonical_source + "
                f"manifest_index + attempt_id already belongs to {index_owner['delivery_id']}"
            )
        self.connection.execute(
            """
            INSERT INTO campaign_delivery_provenance(
                id, delivery_schema_version, campaign_id, delivery_id, analysis_id,
                manifest_index, source_title, source_artist, relative_audio_path,
                source_sha256, source_bytes, output_text_sha256,
                generated_token_count, max_new_tokens, contract, attempt_id,
                canonical_source, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provenance_id,
                entry.delivery_schema_version,
                entry.campaign_id,
                entry.delivery_id,
                analysis_id,
                entry.manifest_index,
                entry.title,
                entry.artist,
                entry.relative_audio_path,
                entry.source_sha256,
                entry.source_bytes,
                entry.output_text_sha256,
                entry.generated_token_count,
                entry.max_new_tokens,
                entry.contract,
                entry.attempt_id,
                entry.canonical_source,
                entry.provenance_json,
            ),
        )
        return False

    def _parse_artists(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        artist_items: Any = payload.get("artists")
        if artist_items is None and payload.get("artist") is not None:
            artist_items = [payload.get("artist")]
        if isinstance(artist_items, str):
            artist_items = [artist_items]
        if not isinstance(artist_items, list) or not artist_items:
            raise ValidationError("At least one artist is required")
        parsed: list[dict[str, Any]] = []
        for item in artist_items:
            if isinstance(item, str):
                parsed.append({"name": require_text(item, "artist"), "aliases": [], "role": "primary"})
            elif isinstance(item, Mapping):
                parsed.append(
                    {
                        "name": require_text(item.get("name"), "artist.name"),
                        "aliases": self._string_list(item.get("aliases")),
                        "role": str(item.get("role") or "primary").strip() or "primary",
                    }
                )
            else:
                raise ValidationError("Each artist must be a string or object")
        return parsed

    @staticmethod
    def _is_feigua_marker(value: object) -> bool:
        if not isinstance(value, str):
            return False
        marker = normalized(value).replace(" ", "")
        return "feigua" in marker or "飞瓜" in marker

    def _reject_feigua_context(self, payload: Mapping[str, Any]) -> None:
        """Keep the music-analysis corpus separate from Feigua workflow data."""

        for key in ("feigua", "feigua_tags", "feigua_metadata", "hot_topic_tags"):
            value = payload.get(key)
            if value not in (None, "", [], {}):
                raise ValidationError(f"{key} is outside the Music Flamingo-only knowledge-base boundary")

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValidationError("Alias values must be strings or an array of strings")
        return [item.strip() for item in value if item.strip()]

    def _resolve_recording_id(self, recording_id: str, audio_sha256: str | None) -> str:
        if audio_sha256:
            same_audio = self.connection.execute(
                "SELECT id FROM recording WHERE audio_sha256 = ?", (audio_sha256,)
            ).fetchone()
            if same_audio is not None:
                return str(same_audio["id"])
        return recording_id

    def _upsert_recording(
        self, recording_id: str, title: str, version_label: str, audio_sha256: str | None
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO recording(id, canonical_title, normalized_title, version_label, audio_sha256)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                canonical_title = excluded.canonical_title,
                normalized_title = excluded.normalized_title,
                version_label = excluded.version_label,
                audio_sha256 = COALESCE(recording.audio_sha256, excluded.audio_sha256),
                updated_at = CURRENT_TIMESTAMP
            """,
            (recording_id, title, normalized(title), version_label, audio_sha256),
        )

    def _upsert_title_aliases(self, recording_id: str, aliases: Sequence[str]) -> None:
        for alias in aliases:
            if alias:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO title_alias(recording_id, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    """,
                    (recording_id, alias, normalized(alias)),
                )

    def _upsert_artist(
        self,
        name: str,
        aliases: Sequence[str],
        *,
        lookup_cache: _BoundedImportLookupCache | None = None,
    ) -> str:
        cache_key = (name, tuple(aliases))
        if lookup_cache is not None:
            cached = lookup_cache.artist(cache_key)
            if cached is not None:
                return cached
        key = normalized(name)
        artist_id = _stable_id("artist", key)
        self.connection.execute(
            """
            INSERT INTO artist(id, canonical_name, normalized_name)
            VALUES (?, ?, ?)
            ON CONFLICT(normalized_name) DO NOTHING
            """,
            (artist_id, name, key),
        )
        row = self.connection.execute("SELECT id FROM artist WHERE normalized_name = ?", (key,)).fetchone()
        assert row is not None
        artist_id = str(row["id"])
        for alias in [name, *aliases]:
            if alias:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO artist_alias(artist_id, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    """,
                    (artist_id, alias, normalized(alias)),
                )
        if lookup_cache is not None:
            lookup_cache.remember_artist(cache_key, artist_id)
        return artist_id

    def _ensure_tag(
        self,
        namespace: str,
        name: str,
        aliases: Sequence[str] = (),
        *,
        path: str = "",
        lifecycle_status: str = "candidate",
        suno_safe: bool = False,
        lookup_cache: _BoundedImportLookupCache | None = None,
    ) -> int:
        namespace = require_text(namespace, "tag.namespace")
        name = require_text(name, "tag.name")
        namespace_key = normalized(namespace)
        lifecycle_status = lifecycle_status or "candidate"
        if lifecycle_status not in {"candidate", "approved", "deprecated"}:
            raise ValidationError("tag.status must be candidate, approved, or deprecated")
        cache_key = (
            namespace_key,
            name,
            tuple(aliases),
            path or "",
            lifecycle_status,
            bool(suno_safe),
        )
        if lookup_cache is not None:
            cached = lookup_cache.tag(cache_key)
            if cached is not None:
                return cached
        if lookup_cache is None or not lookup_cache.has_namespace(namespace_key):
            self.connection.execute(
                "INSERT OR IGNORE INTO tag_namespace(name) VALUES (?)", (namespace_key,)
            )
            if lookup_cache is not None:
                lookup_cache.remember_namespace(namespace_key)
        upsert_sql = """
            INSERT INTO tag(namespace, canonical_name, normalized_name, path, lifecycle_status, suno_safe)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, normalized_name) DO UPDATE SET
                path = CASE WHEN excluded.path <> '' THEN excluded.path ELSE tag.path END,
                lifecycle_status = CASE
                    WHEN tag.lifecycle_status = 'approved' THEN tag.lifecycle_status
                    ELSE excluded.lifecycle_status
                END,
                suno_safe = MAX(tag.suno_safe, excluded.suno_safe)
        """
        upsert_params = (
            namespace_key,
            name,
            normalized(name),
            path or "",
            lifecycle_status,
            int(bool(suno_safe)),
        )
        if self._supports_returning:
            row = self.connection.execute(
                upsert_sql + " RETURNING id, canonical_name, normalized_name", upsert_params
            ).fetchone()
        else:  # pragma: no cover - Python 3.11 distributions normally bundle newer SQLite.
            self.connection.execute(upsert_sql, upsert_params)
            row = self.connection.execute(
                "SELECT id, canonical_name, normalized_name FROM tag WHERE namespace = ? AND normalized_name = ?",
                (namespace_key, normalized(name)),
            ).fetchone()
        assert row is not None
        tag_id = int(row["id"])
        canonical_key = str(row["normalized_name"])
        for alias in aliases:
            # The canonical name is already matched through ``tag``. Avoid
            # duplicating it in ``tag_alias`` for every unique title while
            # preserving genuinely alternate spellings for exact retrieval.
            if alias and normalized(alias) != canonical_key:
                self.connection.execute(
                    "INSERT OR IGNORE INTO tag_alias(tag_id, alias, normalized_alias) VALUES (?, ?, ?)",
                    (tag_id, alias, normalized(alias)),
                )
        if lookup_cache is not None:
            lookup_cache.remember_tag(cache_key, tag_id)
        return tag_id

    def _insert_identity_tags(
        self,
        recording_id: str,
        title: str,
        title_aliases: Sequence[str],
        artists: Sequence[Mapping[str, Any]],
        *,
        lookup_cache: _BoundedImportLookupCache | None = None,
    ) -> None:
        title_tag = self._ensure_tag(
            "title", title, title_aliases, lifecycle_status="approved", lookup_cache=lookup_cache
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO recording_tag(recording_id, tag_id, role) VALUES (?, ?, 'title')",
            (recording_id, title_tag),
        )
        for artist in artists:
            artist_tag = self._ensure_tag(
                "artist",
                str(artist["name"]),
                artist["aliases"],
                lifecycle_status="approved",
                lookup_cache=lookup_cache,
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO recording_tag(recording_id, tag_id, role) VALUES (?, ?, 'artist')",
                (recording_id, artist_tag),
            )

    def _upsert_source_track(self, recording_id: str, source: Any) -> None:
        if not isinstance(source, Mapping):
            raise ValidationError("Each source_track must be an object")
        source_name = normalized(
            require_text(source.get("source") or source.get("source_name"), "source_track.source")
        )
        source_track_id = require_text(source.get("source_track_id"), "source_track.source_track_id")
        source_url = _optional_source_url(source.get("source_url"))
        matches = list(
            self.connection.execute(
                """
                SELECT source_name, recording_id FROM source_track
                WHERE source_track_id = ?
                """,
                (source_track_id,),
            )
        )
        matching_namespaces = [
            row for row in matches if normalized(str(row["source_name"])) == source_name
        ]
        if len(matching_namespaces) > 1:
            raise ValidationError(
                "Multiple source-track rows normalize to the same immutable source identity",
                details={"source": source_name, "source_track_id": source_track_id},
            )
        existing = matching_namespaces[0] if matching_namespaces else None
        if existing is not None and str(existing["recording_id"]) != recording_id:
            raise ValidationError(
                "A source track is already bound to a different recording; source identities are immutable",
                details={
                    "source": source_name,
                    "source_track_id": source_track_id,
                    "existing_recording_id": str(existing["recording_id"]),
                    "incoming_recording_id": recording_id,
                },
            )
        if existing is not None:
            # Legacy generic imports may have stored `KuGou`/`KUGOU`; converge
            # those namespace spellings before campaign invariants query the
            # canonical lower-case source namespace.
            self.connection.execute(
                """
                UPDATE source_track
                SET source_name = ?,
                    source_title = COALESCE(?, source_title),
                    source_artist_credit = COALESCE(?, source_artist_credit),
                    source_url = COALESCE(?, source_url)
                WHERE source_name = ? AND source_track_id = ?
                """,
                (
                    source_name,
                    str(source.get("source_title") or "").strip() or None,
                    str(source.get("source_artist_credit") or "").strip() or None,
                    source_url,
                    str(existing["source_name"]),
                    source_track_id,
                ),
            )
            return
        source_id = _stable_id("src", normalized(source_name), source_track_id)
        self.connection.execute(
            """
            INSERT INTO source_track(id, recording_id, source_name, source_track_id, source_title, source_artist_credit, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, source_track_id) DO UPDATE SET
                source_title = COALESCE(excluded.source_title, source_track.source_title),
                source_artist_credit = COALESCE(excluded.source_artist_credit, source_track.source_artist_credit),
                source_url = COALESCE(excluded.source_url, source_track.source_url)
            """,
            (
                source_id,
                recording_id,
                source_name,
                source_track_id,
                str(source.get("source_title") or "").strip() or None,
                str(source.get("source_artist_credit") or "").strip() or None,
                source_url,
            ),
        )

    def backfill_source_links(self, chart_database: str | Path) -> dict[str, Any]:
        """Backfill source URLs from the authoritative Kugou chart database."""

        self._require_writer()
        chart_path = Path(chart_database).expanduser().resolve()
        if not chart_path.is_file():
            raise ValidationError(f"Kugou chart database does not exist: {chart_path}")
        external = sqlite3.connect(f"{chart_path.as_uri()}?mode=ro", uri=True)
        external.row_factory = sqlite3.Row
        try:
            rows = external.execute(
                """
                SELECT s.canonical_title AS title, s.canonical_artist AS artist, pt.play_link
                FROM songs s JOIN platform_tracks pt ON pt.song_id = s.song_id
                WHERE pt.platform = 'kugou' AND pt.play_link IS NOT NULL AND trim(pt.play_link) <> ''
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValidationError(f"Unable to read Kugou chart database: {exc}") from exc
        finally:
            external.close()

        links: dict[tuple[str, str], str] = {}
        for row in rows:
            url = _optional_source_url(row["play_link"])
            if url is None:
                continue
            key = (normalized(str(row["title"])), normalized(str(row["artist"])))
            previous = links.get(key)
            if previous is not None and previous != url:
                raise ValidationError(
                    "Kugou chart database has conflicting links for one title/artist",
                    details={"title": row["title"], "artist": row["artist"]},
                )
            links[key] = url

        source_rows = list(
            self.connection.execute(
                """
                SELECT source_track_id, source_title, source_artist_credit
                FROM source_track
                WHERE source_name = 'kugou'
                """
            )
        )
        matched = 0
        unresolved: list[str] = []
        with self.connection:
            for row in source_rows:
                key = (normalized(str(row["source_title"] or "")), normalized(str(row["source_artist_credit"] or "")))
                url = links.get(key)
                if url is None:
                    unresolved.append(str(row["source_track_id"]))
                    continue
                self.connection.execute(
                    "UPDATE source_track SET source_url = ? WHERE source_name = 'kugou' AND source_track_id = ?",
                    (url, row["source_track_id"]),
                )
                matched += 1
        return {
            "chart_database": str(chart_path),
            "chart_link_count": len(links),
            "kugou_source_track_count": len(source_rows),
            "matched": matched,
            "unresolved": unresolved,
        }

    def _upsert_tag_from_payload(
        self, tag: Any, *, lookup_cache: _BoundedImportLookupCache | None = None
    ) -> tuple[int, float | None]:
        if not isinstance(tag, Mapping):
            raise ValidationError("Each tag must be an object")
        self._validate_tag_boundary(tag)
        namespace = str(tag.get("namespace") or "")
        path = str(tag.get("path") or "")
        confidence = tag.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise ValidationError("tag.confidence must be a number") from exc
            if not 0 <= confidence <= 1:
                raise ValidationError("tag.confidence must be between 0 and 1")
        tag_id = self._ensure_tag(
            namespace,
            str(tag.get("name") or ""),
            self._string_list(tag.get("aliases")),
            path=path.strip(),
            lifecycle_status=str(tag.get("status") or "candidate").strip(),
            suno_safe=bool(tag.get("suno_safe", False)),
            lookup_cache=lookup_cache,
        )
        return tag_id, confidence

    def _validate_tag_boundary(self, tag: Any) -> None:
        if not isinstance(tag, Mapping):
            # The normal importer error is more useful for malformed tag types.
            return
        namespace = str(tag.get("namespace") or "")
        path = str(tag.get("path") or "")
        source = str(tag.get("source") or "model")
        if any(self._is_feigua_marker(value) for value in (namespace, path, source)):
            raise ValidationError("Feigua tags and workflow metadata cannot be imported into music-kb")

    def _upsert_numeric_feature(self, analysis_id: str, feature: Any) -> None:
        if not isinstance(feature, Mapping):
            raise ValidationError("Each numeric feature must be an object")
        name = require_text(feature.get("name"), "numeric_feature.name")
        try:
            value = float(feature.get("value"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("numeric_feature.value must be a number") from exc
        confidence = feature.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise ValidationError("numeric_feature.confidence must be a number") from exc
            if not 0 <= confidence <= 1:
                raise ValidationError("numeric_feature.confidence must be between 0 and 1")
        source = str(feature.get("source") or "model").strip()
        if not source:
            raise ValidationError("numeric_feature.source must be a non-empty string")
        self.connection.execute(
            """
            INSERT INTO numeric_feature(analysis_id, name, value, unit, confidence, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(analysis_id, name) DO UPDATE SET
                value = excluded.value,
                unit = excluded.unit,
                confidence = excluded.confidence,
                source = excluded.source
            """,
            (
                analysis_id,
                normalized(name),
                value,
                str(feature.get("unit") or "").strip(),
                confidence,
                source,
            ),
        )

    def _set_canonical(self, recording_id: str, analysis_id: str) -> None:
        self.connection.execute(
            """
            UPDATE analysis_revision
            SET status = 'superseded'
            WHERE recording_id = ? AND status = 'canonical' AND id <> ?
            """,
            (recording_id, analysis_id),
        )
        self.connection.execute(
            "UPDATE analysis_revision SET status = 'canonical' WHERE id = ?", (analysis_id,)
        )
        self.connection.execute(
            "UPDATE recording SET canonical_analysis_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (analysis_id, recording_id),
        )

    def _is_canonical(self, recording_id: str, analysis_id: str) -> bool:
        row = self.connection.execute(
            "SELECT canonical_analysis_id FROM recording WHERE id = ?", (recording_id,)
        ).fetchone()
        return row is not None and str(row["canonical_analysis_id"] or "") == analysis_id

    # -- canonical search projection -------------------------------------

    def rebuild_search_projection(self, recording_id: str) -> None:
        self._require_writer()
        self.connection.execute("DELETE FROM search_fts WHERE recording_id = ?", (recording_id,))
        self._insert_search_projection(recording_id)

    def _set_search_projection_state(self, state: str) -> None:
        self.connection.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (SEARCH_PROJECTION_STATE_KEY, state),
        )

    def _insert_search_projection(self, recording_id: str) -> None:
        """Insert one projection row when any prior row is already removed."""

        row = self.connection.execute(
            """
            SELECT r.id, r.canonical_title, ar.raw_text, ar.summary
            FROM recording r
            JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
            WHERE r.id = ?
            """,
            (recording_id,),
        ).fetchone()
        if row is None:
            return
        artists = self._artist_names(recording_id)
        aliases = self._aliases_for_recording(recording_id)
        tags = self._public_tag_names(recording_id)
        tag_search_terms = [*tags, *self._public_tag_aliases(recording_id)]
        analysis = "\n".join(value for value in [str(row["summary"]), str(row["raw_text"])] if value)
        self.connection.execute(
            """
            INSERT INTO search_fts(recording_id, title, artist, aliases, tags, analysis)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                recording_id,
                str(row["canonical_title"]),
                " ".join(artists),
                " ".join(aliases),
                " ".join(dict.fromkeys(tag_search_terms)),
                analysis,
            ),
        )

    def rebuild_all_search_projections(self) -> int:
        self._require_writer()
        with self.connection:
            return self._rebuild_all_search_projections_in_transaction()

    def _rebuild_all_search_projections_in_transaction(self) -> int:
        """Replace every public FTS row within an already-open transaction."""

        recording_ids = [
            str(row["id"])
            for row in self.connection.execute(
                "SELECT id FROM recording WHERE canonical_analysis_id IS NOT NULL"
            )
        ]
        self.connection.execute("DELETE FROM search_fts")
        for recording_id in recording_ids:
            self._insert_search_projection(recording_id)
        self._set_search_projection_state(SEARCH_PROJECTION_CURRENT)
        return len(recording_ids)

    def _artist_names(self, recording_id: str) -> list[str]:
        return [
            str(row["canonical_name"])
            for row in self.connection.execute(
                """
                SELECT a.canonical_name FROM recording_artist ra
                JOIN artist a ON a.id = ra.artist_id
                WHERE ra.recording_id = ?
                ORDER BY ra.position, a.canonical_name
                """,
                (recording_id,),
            )
        ]

    def _aliases_for_recording(self, recording_id: str) -> list[str]:
        title_aliases = [
            str(row["alias"])
            for row in self.connection.execute(
                "SELECT alias FROM title_alias WHERE recording_id = ? ORDER BY alias", (recording_id,)
            )
        ]
        artist_aliases = [
            str(row["alias"])
            for row in self.connection.execute(
                """
                SELECT aa.alias FROM recording_artist ra
                JOIN artist_alias aa ON aa.artist_id = ra.artist_id
                WHERE ra.recording_id = ? ORDER BY aa.alias
                """,
                (recording_id,),
            )
        ]
        return list(dict.fromkeys([*title_aliases, *artist_aliases]))

    def _public_tag_names(self, recording_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT t.canonical_name
            FROM recording r
            JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
            JOIN analysis_tag at ON at.analysis_id = ar.id
            JOIN tag t ON t.id = at.tag_id
            WHERE r.id = ?
            UNION
            SELECT DISTINCT t.canonical_name
            FROM recording_tag rt JOIN tag t ON t.id = rt.tag_id
            WHERE rt.recording_id = ?
            ORDER BY 1
            """,
            (recording_id, recording_id),
        )
        return [str(row["canonical_name"]) for row in rows]

    def _public_tag_aliases(self, recording_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT ta.alias
            FROM recording r
            JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
            JOIN analysis_tag at ON at.analysis_id = ar.id
            JOIN tag_alias ta ON ta.tag_id = at.tag_id
            WHERE r.id = ?
            UNION
            SELECT DISTINCT ta.alias
            FROM recording_tag rt
            JOIN tag_alias ta ON ta.tag_id = rt.tag_id
            WHERE rt.recording_id = ?
            ORDER BY 1
            """,
            (recording_id, recording_id),
        )
        return [str(row["alias"]) for row in rows]

    def _source_links(self, recording_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT source_name, source_track_id, source_title,
                   source_artist_credit, source_url
            FROM source_track
            WHERE recording_id = ? AND source_url IS NOT NULL AND trim(source_url) <> ''
            ORDER BY source_name, source_track_id
            """,
            (recording_id,),
        )
        return [
            {
                "source": str(row["source_name"]),
                "track_id": str(row["source_track_id"]),
                "title": row["source_title"],
                "artist": row["source_artist_credit"],
                "url": str(row["source_url"]),
            }
            for row in rows
        ]

    # -- lyrics ----------------------------------------------------------

    def _resolve_lyric_source_track(
        self, *, recording_id: str, source_track_row_id: str
    ) -> sqlite3.Row:
        row = self.connection.execute(
            """
            SELECT st.id, st.recording_id, st.source_name, st.source_track_id,
                   st.source_title, st.source_artist_credit, st.source_url,
                   r.canonical_analysis_id
            FROM source_track st
            JOIN recording r ON r.id = st.recording_id
            WHERE st.id = ?
            """,
            (source_track_row_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"Unknown lyric source-track row {source_track_row_id}")
        if str(row["recording_id"]) != recording_id:
            raise ValidationError(
                "Lyric source track belongs to a different recording",
                details={
                    "recording_id": recording_id,
                    "source_track_row_id": source_track_row_id,
                    "source_recording_id": str(row["recording_id"]),
                },
            )
        if row["canonical_analysis_id"] is None:
            raise ValidationError("Lyrics can be imported only for a canonical recording")
        return row

    @staticmethod
    def _lyric_evidence(
        value: object,
        *,
        status: str,
        source_track: sqlite3.Row,
    ) -> str:
        if not isinstance(value, Mapping):
            raise ValidationError("lyric.evidence must be an object")
        evidence = dict(value)
        source_name = normalized(
            require_text(evidence.get("source_name"), "lyric.evidence.source_name")
        )
        source_track_id = require_text(
            evidence.get("source_track_id"), "lyric.evidence.source_track_id"
        )
        if source_name != normalized(str(source_track["source_name"])):
            raise ValidationError("Lyric evidence source_name does not match source track")
        if source_track_id != str(source_track["source_track_id"]):
            raise ValidationError("Lyric evidence source_track_id does not match source track")
        if status in LYRIC_TERMINAL_STATUSES and not str(evidence.get("reason") or "").strip():
            raise ValidationError("Terminal lyric evidence requires a non-empty reason")
        try:
            return json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("lyric.evidence must be JSON serializable") from exc

    def _import_lyric(self, payload: Mapping[str, Any], *, transactional: bool) -> dict[str, Any]:
        self._require_writer()
        if not isinstance(payload, Mapping):
            raise ValidationError("Lyric import payload must be an object")
        recording_id = require_text(payload.get("recording_id"), "lyric.recording_id")
        source_track_row_id = require_text(
            payload.get("source_track_row_id"), "lyric.source_track_row_id"
        )
        status = str(payload.get("status") or "").strip().lower()
        if status not in LYRIC_STATUSES:
            raise ValidationError(
                "lyric.status must be pending, available, instrumental, or platform_unavailable"
            )
        source_track = self._resolve_lyric_source_track(
            recording_id=recording_id, source_track_row_id=source_track_row_id
        )
        evidence_json = self._lyric_evidence(
            payload.get("evidence"), status=status, source_track=source_track
        )
        normalizer_version = str(
            payload.get("normalizer_version") or LYRIC_NORMALIZER_VERSION
        ).strip()
        if not normalizer_version:
            raise ValidationError("lyric.normalizer_version must not be empty")

        lyric_text: str | None = None
        text_sha256: str | None = None
        raw_text = payload.get("lyric_text")
        if status == LYRIC_STATUS_AVAILABLE:
            lyric_text = normalize_lyric_text(raw_text)
            if not lyric_text:
                raise ValidationError("Available lyric text is empty after normalization")
            if not is_publishable_lyric_text(lyric_text):
                raise ValidationError(
                    "Available lyrics must not contain an HTML or failure-placeholder payload"
                )
            text_sha256 = lyric_text_sha256(lyric_text)
        elif raw_text not in (None, ""):
            raise ValidationError("Only available lyrics may contain lyric_text")

        existing = self.connection.execute(
            """
            SELECT source_track_row_id, status, lyric_text, text_sha256,
                   evidence_json, normalizer_version
            FROM recording_lyric WHERE recording_id = ?
            """,
            (recording_id,),
        ).fetchone()
        if existing is not None and str(existing["status"]) in LYRIC_TERMINAL_STATUSES and status == LYRIC_STATUS_PENDING:
            return {
                "recording_id": recording_id,
                "source_track_row_id": str(existing["source_track_row_id"]),
                "status": str(existing["status"]),
                "text_sha256": existing["text_sha256"],
                "idempotent": True,
                "preserved_terminal": True,
            }
        if existing is not None and (
            str(existing["source_track_row_id"]) == source_track_row_id
            and str(existing["status"]) == status
            and existing["lyric_text"] == lyric_text
            and existing["text_sha256"] == text_sha256
            and str(existing["evidence_json"]) == evidence_json
            and str(existing["normalizer_version"]) == normalizer_version
        ):
            return {
                "recording_id": recording_id,
                "source_track_row_id": source_track_row_id,
                "status": status,
                "text_sha256": text_sha256,
                "idempotent": True,
                "preserved_terminal": False,
            }

        transaction = self.connection if transactional else nullcontext()
        with transaction:
            self.connection.execute(
                """
                INSERT INTO recording_lyric(
                    recording_id, source_track_row_id, status, lyric_text,
                    text_sha256, evidence_json, normalizer_version, acquired_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(recording_id) DO UPDATE SET
                    source_track_row_id = excluded.source_track_row_id,
                    status = excluded.status,
                    lyric_text = excluded.lyric_text,
                    text_sha256 = excluded.text_sha256,
                    evidence_json = excluded.evidence_json,
                    normalizer_version = excluded.normalizer_version,
                    acquired_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    recording_id,
                    source_track_row_id,
                    status,
                    lyric_text,
                    text_sha256,
                    evidence_json,
                    normalizer_version,
                ),
            )
        return {
            "recording_id": recording_id,
            "source_track_row_id": source_track_row_id,
            "status": status,
            "text_sha256": text_sha256,
            "idempotent": False,
            "preserved_terminal": False,
        }

    def import_lyric(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Write one identity-bound lyric result into the publisher master."""

        return self._import_lyric(payload, transactional=True)

    def import_lyrics(self, payloads: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Import a bounded batch atomically while retaining terminal results."""

        self._require_writer()
        results: list[dict[str, Any]] = []
        with self.connection:
            for payload in payloads:
                results.append(self._import_lyric(payload, transactional=False))
        return {
            "count": len(results),
            "imported": sum(1 for result in results if not result["idempotent"]),
            "idempotent": sum(1 for result in results if result["idempotent"]),
            "results": results,
        }

    def _resolve_lyric_receipt_source_track(self, receipt: Mapping[str, Any]) -> sqlite3.Row:
        """Bind a worker receipt to exactly one canonical source-track row.

        The worker is allowed to use title/artist to locate a Kugou result, but
        the receipt may enter the database only through its platform source ID.
        Optional row/recording fields are assertions, never alternate lookup
        keys, so an old or mismatched queue cannot silently attach lyrics to a
        different recording.
        """

        source_name = normalized(
            require_text(receipt.get("source_name"), "lyric receipt.source_name")
        )
        source_track_id = require_text(
            receipt.get("source_track_id"), "lyric receipt.source_track_id"
        )
        rows = list(
            self.connection.execute(
                """
                SELECT st.id, st.recording_id, st.source_name, st.source_track_id,
                       st.source_title, st.source_artist_credit, st.source_url,
                       r.canonical_analysis_id
                FROM source_track st
                JOIN recording r ON r.id = st.recording_id
                WHERE st.source_track_id = ?
                """,
                (source_track_id,),
            )
        )
        matching = [row for row in rows if normalized(str(row["source_name"])) == source_name]
        if not matching:
            raise ValidationError(
                "Lyric receipt source identity is not present in the publisher database",
                details={"source_name": source_name, "source_track_id": source_track_id},
            )
        if len(matching) != 1:
            raise ValidationError(
                "Lyric receipt source identity is ambiguous in the publisher database",
                details={"source_name": source_name, "source_track_id": source_track_id},
            )
        source_track = matching[0]
        expected_recording_id = str(receipt.get("recording_id") or "").strip()
        if expected_recording_id and expected_recording_id != str(source_track["recording_id"]):
            raise ValidationError(
                "Lyric receipt recording_id does not match its source identity",
                details={
                    "receipt_recording_id": expected_recording_id,
                    "source_recording_id": str(source_track["recording_id"]),
                    "source_track_id": source_track_id,
                },
            )
        expected_row_id = str(receipt.get("source_track_row_id") or "").strip()
        if expected_row_id and expected_row_id != str(source_track["id"]):
            raise ValidationError(
                "Lyric receipt source_track_row_id does not match its source identity",
                details={
                    "receipt_source_track_row_id": expected_row_id,
                    "source_track_row_id": str(source_track["id"]),
                    "source_track_id": source_track_id,
                },
            )
        if source_track["canonical_analysis_id"] is None:
            raise ValidationError("Lyrics can be imported only for a canonical recording")
        return source_track

    def _import_lyric_receipt(
        self, receipt: Mapping[str, Any], *, transactional: bool
    ) -> dict[str, Any]:
        self._require_writer()
        if not isinstance(receipt, Mapping):
            raise ValidationError("Lyric receipt must be an object")
        source_track = self._resolve_lyric_receipt_source_track(receipt)
        evidence = receipt.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValidationError("Lyric receipt evidence must be an object")
        payload = {
            "recording_id": str(source_track["recording_id"]),
            "source_track_row_id": str(source_track["id"]),
            "status": receipt.get("status"),
            "lyric_text": receipt.get("lyric_text"),
            "evidence": dict(evidence),
            "normalizer_version": receipt.get("normalizer_version"),
        }
        result = self._import_lyric(payload, transactional=transactional)
        return {
            **result,
            "receipt_source_name": normalized(str(receipt.get("source_name") or "")),
            "receipt_source_track_id": str(receipt.get("source_track_id") or ""),
        }

    def import_lyric_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Import one worker receipt only after source-identity verification."""

        return self._import_lyric_receipt(receipt, transactional=True)

    def import_lyric_receipts(self, receipts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Import an auditable receipt batch atomically and idempotently."""

        self._require_writer()
        results: list[dict[str, Any]] = []
        with self.connection:
            for receipt in receipts:
                results.append(self._import_lyric_receipt(receipt, transactional=False))
        return {
            "count": len(results),
            "imported": sum(1 for result in results if not result["idempotent"]),
            "idempotent": sum(1 for result in results if result["idempotent"]),
            "results": results,
        }

    def import_lyric_receipt_file(self, path: str | Path) -> dict[str, Any]:
        """Read a CC JSONL receipt file and bind each row to canonical data."""

        source = Path(path).expanduser().resolve()
        result = self.import_lyric_receipts(load_lyric_receipts(source))
        return {**result, "receipt_file": str(source)}

    def repair_invalid_available_lyrics(self) -> dict[str, Any]:
        """Demote historical HTML/error placeholders so they are retried.

        This only corrects rows previously marked ``available`` that fail the
        same narrow publishability gate enforced at import time. It never
        upgrades or fabricates a lyric result.
        """

        self._require_writer()
        rows = list(
            self.connection.execute(
                """
                SELECT recording_id, lyric_text, evidence_json
                FROM recording_lyric
                WHERE status = ?
                ORDER BY recording_id
                """,
                (LYRIC_STATUS_AVAILABLE,),
            )
        )
        repaired: list[str] = []
        with self.connection:
            for row in rows:
                if is_publishable_lyric_text(row["lyric_text"]):
                    continue
                try:
                    evidence_value = json.loads(str(row["evidence_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    evidence_value = {}
                evidence = dict(evidence_value) if isinstance(evidence_value, Mapping) else {}
                evidence.update(
                    {
                        "reason": (
                            "Previously accepted lyric text was an HTML or failure placeholder; "
                            "a fresh exact-identity query is required."
                        ),
                        "response_kind": "invalid_lyric_payload_repair",
                    }
                )
                self.connection.execute(
                    """
                    UPDATE recording_lyric
                    SET status = ?, lyric_text = NULL, text_sha256 = NULL,
                        evidence_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE recording_id = ?
                    """,
                    (
                        LYRIC_STATUS_PENDING,
                        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        str(row["recording_id"]),
                    ),
                )
                repaired.append(str(row["recording_id"]))
        return {
            "scanned_available": len(rows),
            "demoted": len(repaired),
            "recording_ids": repaired,
        }

    def lyric_coverage(self) -> dict[str, int]:
        """Report coverage for the recordings that a public snapshot exposes."""

        row = self.connection.execute(
            """
            SELECT COUNT(*) AS canonical_recordings,
                   COALESCE(SUM(CASE WHEN rl.status = 'available' THEN 1 ELSE 0 END), 0) AS available,
                   COALESCE(SUM(CASE WHEN rl.status = 'instrumental' THEN 1 ELSE 0 END), 0) AS instrumental,
                   COALESCE(SUM(CASE WHEN rl.status = 'platform_unavailable' THEN 1 ELSE 0 END), 0) AS platform_unavailable,
                   COALESCE(SUM(CASE WHEN rl.status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                   COALESCE(SUM(CASE WHEN rl.recording_id IS NULL THEN 1 ELSE 0 END), 0) AS missing
            FROM recording r
            LEFT JOIN recording_lyric rl ON rl.recording_id = r.id
            WHERE r.canonical_analysis_id IS NOT NULL
            """
        ).fetchone()
        canonical_recordings = int(row["canonical_recordings"] or 0)
        available = int(row["available"] or 0)
        instrumental = int(row["instrumental"] or 0)
        platform_unavailable = int(row["platform_unavailable"] or 0)
        pending = int(row["pending"] or 0)
        missing = int(row["missing"] or 0)
        return {
            "canonical_recordings": canonical_recordings,
            "available": available,
            "instrumental": instrumental,
            "platform_unavailable": platform_unavailable,
            "pending": pending,
            "missing": missing,
            "unresolved": canonical_recordings - available - instrumental - platform_unavailable,
        }

    @staticmethod
    def _load_kugou_chart_play_link_map(
        chart_database: str | Path,
    ) -> tuple[Path, dict[str, str]]:
        """Read the immutable chart URL -> MixSongID bridge in read-only mode.

        The first 927 campaign deliveries predate the ``kugou-<MixSongID>``
        source-track convention.  Their source-track IDs are delivery keys,
        not Kugou IDs.  Their exact public ``source_url`` is, however, copied
        verbatim from ``platform_tracks.play_link`` in the authoritative chart
        database.  This bridge preserves that exact relationship; it never
        tries to infer an ID from title or artist text.
        """

        path = Path(chart_database).expanduser().resolve()
        if not path.is_file():
            raise ValidationError(f"Kugou chart database does not exist: {path}")
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT play_link, platform_track_key
                FROM platform_tracks
                WHERE platform = 'kugou'
                  AND play_link IS NOT NULL
                  AND trim(play_link) <> ''
                  AND platform_track_key IS NOT NULL
                  AND trim(platform_track_key) <> ''
                ORDER BY play_link, platform_track_key
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValidationError(
                "Kugou chart database cannot provide platform_tracks play-link identities",
                details={"chart_database": str(path)},
            ) from exc
        finally:
            connection.close()

        values_by_link: dict[str, set[str]] = {}
        for row in rows:
            link = str(row["play_link"])
            track_key = str(row["platform_track_key"]).strip()
            values_by_link.setdefault(link, set()).add(track_key)
        conflicts = {
            link: sorted(values)
            for link, values in values_by_link.items()
            if len(values) != 1
        }
        if conflicts:
            sample = [
                {"play_link": link, "platform_track_keys": values}
                for link, values in list(sorted(conflicts.items()))[:20]
            ]
            raise ValidationError(
                "Kugou chart database maps one public play link to multiple platform IDs",
                details={
                    "chart_database": str(path),
                    "conflict_count": len(conflicts),
                    "conflicts": sample,
                },
            )
        return path, {link: next(iter(values)) for link, values in values_by_link.items()}

    @staticmethod
    def _kugou_platform_identity(
        row: sqlite3.Row,
        *,
        chart_play_link_map: Mapping[str, str] | None,
    ) -> tuple[str, str] | None:
        """Return (MixSongID, audit method) for one exact source-track row."""

        source_track_id = str(row["source_track_id"] or "").strip()
        if source_track_id.casefold().startswith("kugou-"):
            mix_song_id = source_track_id[len("kugou-") :].strip()
            if mix_song_id:
                return mix_song_id, "source_track_id_kugou_prefix_v1"
            return None
        source_url = str(row["source_url"] or "")
        if chart_play_link_map is None:
            return None
        mix_song_id = chart_play_link_map.get(source_url)
        if not mix_song_id:
            return None
        return mix_song_id, "chart_play_link_exact_v1"

    def prepare_lyric_backfill_queue(
        self,
        *,
        unresolved_only: bool = True,
        chart_database: str | Path | None = None,
    ) -> dict[str, Any]:
        """Build the identity-bound no-audio queue for the CC lyric worker.

        The queue deliberately contains one selected, exact Kugou source track
        for each canonical recording. A title or artist can help the worker
        discover a result, but it cannot replace the ``MixSongID`` carried in
        ``platform_track_key``. New rows already carry that ID as
        ``kugou-<MixSongID>``. Historical rows use the read-only chart
        database's exact ``play_link`` bridge; they are never mapped by a
        title/artist guess.

        A small number of canonical recordings have several source identities
        whose campaign provenance proves that they produced the same canonical
        audio SHA-256. Those byte-identical aliases may be reduced to the
        deterministic lowest source-track ID. Any unproven multi-source case
        remains an explicit queue error rather than an arbitrary selection.
        """

        coverage = self.lyric_coverage()
        condition = ""
        parameters: tuple[str, ...] = ()
        if unresolved_only:
            condition = "AND (rl.recording_id IS NULL OR rl.status = ?)"
            parameters = (LYRIC_STATUS_PENDING,)
        rows = list(
            self.connection.execute(
                f"""
                SELECT r.id AS recording_id, r.canonical_title, r.version_label,
                       r.audio_sha256,
                       rl.status AS lyric_status,
                       st.id AS source_track_row_id, st.source_name,
                       st.source_track_id, st.source_title,
                       st.source_artist_credit, st.source_url,
                       EXISTS(
                           SELECT 1
                           FROM campaign_delivery_provenance c
                           WHERE c.analysis_id = r.canonical_analysis_id
                             AND c.delivery_id = st.source_track_id
                             AND c.source_sha256 = r.audio_sha256
                       ) AS exact_audio_provenance
                FROM recording r
                LEFT JOIN recording_lyric rl ON rl.recording_id = r.id
                JOIN source_track st ON st.recording_id = r.id
                WHERE r.canonical_analysis_id IS NOT NULL
                  {condition}
                ORDER BY r.id, st.source_name, st.source_track_id, st.id
                """,
                parameters,
            )
        )
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["recording_id"]), []).append(row)

        legacy_rows = [
            row
            for row in rows
            if normalized(str(row["source_name"] or "")) == "kugou"
            and not str(row["source_track_id"] or "").strip().casefold().startswith("kugou-")
        ]
        chart_path: Path | None = None
        chart_play_link_map: dict[str, str] | None = None
        if legacy_rows:
            if chart_database is not None:
                chart_path, chart_play_link_map = self._load_kugou_chart_play_link_map(chart_database)

        queue: list[dict[str, Any]] = []
        source_issues: list[dict[str, Any]] = []
        resolution_counts: Counter[str] = Counter()
        platform_recordings: dict[str, set[str]] = {}
        for recording_id, recording_rows in grouped.items():
            eligible: list[tuple[sqlite3.Row, str, str]] = []
            for row in recording_rows:
                source_name = normalized(str(row["source_name"] or ""))
                if source_name != "kugou":
                    continue
                identity = self._kugou_platform_identity(
                    row,
                    chart_play_link_map=chart_play_link_map,
                )
                if identity is not None:
                    mix_song_id, resolution_method = identity
                    eligible.append((row, mix_song_id, resolution_method))
            if not eligible:
                source_issues.append(
                    {
                        "recording_id": recording_id,
                        "candidate_source_tracks": [
                            {
                                "source_name": str(row["source_name"]),
                                "source_track_id": str(row["source_track_id"]),
                            }
                            for row in recording_rows
                        ],
                        "usable_kugou_source_count": 0,
                        "reason": (
                            "legacy source requires --chart-db with an exact Kugou play_link mapping"
                            if any(
                                normalized(str(row["source_name"] or "")) == "kugou"
                                and not str(row["source_track_id"] or "").strip().casefold().startswith("kugou-")
                                for row in recording_rows
                            )
                            else "recording has no exact Kugou platform identity"
                        ),
                    }
                )
                continue
            if len(eligible) > 1 and not all(
                bool(row["exact_audio_provenance"]) for row, _mix_song_id, _method in eligible
            ):
                source_issues.append(
                    {
                        "recording_id": recording_id,
                        "candidate_source_tracks": [
                            {
                                "source_name": str(row["source_name"]),
                                "source_track_id": str(row["source_track_id"]),
                                "platform_track_key": mix_song_id,
                                "identity_resolution": resolution_method,
                                "exact_audio_provenance": bool(row["exact_audio_provenance"]),
                            }
                            for row, mix_song_id, resolution_method in eligible
                        ],
                        "usable_kugou_source_count": len(eligible),
                        "reason": "multiple Kugou identities lack byte-identical canonical-audio provenance",
                    }
                )
                continue
            eligible.sort(
                key=lambda item: (
                    str(item[0]["source_track_id"]),
                    str(item[0]["source_track_row_id"]),
                )
            )
            row, mix_song_id, resolution_method = eligible[0]
            source_track_id = str(row["source_track_id"]).strip()
            platform_recordings.setdefault(mix_song_id, set()).add(recording_id)
            title = str(row["source_title"] or row["canonical_title"] or "").strip()
            artist = str(row["source_artist_credit"] or "").strip()
            queue.append(
                {
                    "schema_version": 1,
                    "recording_id": recording_id,
                    "source_track_row_id": str(row["source_track_row_id"]),
                    "source_name": str(row["source_name"]),
                    "source_track_id": source_track_id,
                    "identity_key": f"kugou:{mix_song_id}",
                    "platform": "kugou",
                    "platform_track_key": mix_song_id,
                    "title": title,
                    "artist": artist,
                    "source_url": row["source_url"],
                    "existing_lyric_status": str(row["lyric_status"] or "missing"),
                    "identity_resolution": resolution_method,
                    "source_identity_alias_count": len(eligible),
                }
            )
            resolution_counts[resolution_method] += 1
        conflicting_platform_ids = {
            mix_song_id: sorted(recording_ids)
            for mix_song_id, recording_ids in platform_recordings.items()
            if len(recording_ids) > 1
        }
        if conflicting_platform_ids:
            source_issues.append(
                {
                    "reason": "one exact Kugou platform identity resolves to multiple canonical recordings",
                    "platform_identity_conflicts": [
                        {"platform_track_key": key, "recording_ids": recording_ids}
                        for key, recording_ids in list(sorted(conflicting_platform_ids.items()))[:20]
                    ],
                    "platform_identity_conflict_count": len(conflicting_platform_ids),
                }
            )
        if source_issues:
            message = "Cannot prepare lyric backfill queue: canonical recordings lack a safe exact Kugou source identity"
            if legacy_rows and chart_database is None:
                message += "; legacy Kugou source rows require --chart-db"
            raise ValidationError(
                message,
                details={
                    "coverage": coverage,
                    "unqueueable_count": len(source_issues),
                    "unqueueable": source_issues,
                },
            )
        return {
            "schema_version": 1,
            "database": str(self.path),
            "unresolved_only": unresolved_only,
            "coverage": coverage,
            "queue_count": len(queue),
            "chart_database": str(chart_path) if chart_path is not None else None,
            "identity_resolution_counts": dict(sorted(resolution_counts.items())),
            "rows": queue,
        }

    def get_lyrics(self, recording_id: str) -> dict[str, Any]:
        """Fetch one selected recording's full lyric text without truncation."""

        row = self.connection.execute(
            """
            SELECT r.id, r.canonical_title, r.version_label,
                   rl.source_track_row_id, rl.status, rl.lyric_text,
                   rl.text_sha256, rl.evidence_json, rl.normalizer_version,
                   rl.acquired_at, rl.updated_at,
                   st.source_name, st.source_track_id, st.source_title,
                   st.source_artist_credit, st.source_url
            FROM recording r
            LEFT JOIN recording_lyric rl ON rl.recording_id = r.id
            LEFT JOIN source_track st ON st.id = rl.source_track_row_id
            WHERE r.id = ? AND r.canonical_analysis_id IS NOT NULL
            """,
            (recording_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"No canonical recording found for lyrics {recording_id}")
        if row["status"] is None:
            return {
                "recording_id": str(row["id"]),
                "title": str(row["canonical_title"]),
                "version_label": str(row["version_label"]),
                "status": LYRIC_STATUS_PENDING,
                "lyric_text": None,
                "source": None,
                "evidence": None,
            }
        evidence = json.loads(str(row["evidence_json"]))
        return {
            "recording_id": str(row["id"]),
            "title": str(row["canonical_title"]),
            "version_label": str(row["version_label"]),
            "status": str(row["status"]),
            "lyric_text": row["lyric_text"],
            "text_sha256": row["text_sha256"],
            "normalizer_version": str(row["normalizer_version"]),
            "acquired_at": str(row["acquired_at"]),
            "updated_at": str(row["updated_at"]),
            "source": {
                "source": str(row["source_name"]),
                "track_id": str(row["source_track_id"]),
                "title": row["source_title"],
                "artist": row["source_artist_credit"],
                "url": row["source_url"],
            },
            "evidence": evidence,
        }

    # -- reads ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        metadata = {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute("SELECT key, value FROM meta ORDER BY key")
        }
        lyric_coverage = self.lyric_coverage()
        counts = {
            "recordings": self.connection.execute("SELECT COUNT(*) FROM recording").fetchone()[0],
            "canonical_analyses": self.connection.execute(
                "SELECT COUNT(*) FROM recording WHERE canonical_analysis_id IS NOT NULL"
            ).fetchone()[0],
            "canonical_recordings": lyric_coverage["canonical_recordings"],
            "analysis_revisions": self.connection.execute("SELECT COUNT(*) FROM analysis_revision").fetchone()[0],
            "campaign_delivery_provenance": self.connection.execute(
                "SELECT COUNT(*) FROM campaign_delivery_provenance"
            ).fetchone()[0],
            "tags": self.connection.execute("SELECT COUNT(*) FROM tag").fetchone()[0],
            "source_tracks": self.connection.execute("SELECT COUNT(*) FROM source_track").fetchone()[0],
            "source_links": self.connection.execute(
                "SELECT COUNT(*) FROM source_track WHERE source_url IS NOT NULL AND trim(source_url) <> ''"
            ).fetchone()[0],
            "lyrics_available": lyric_coverage["available"],
            "lyrics_instrumental": lyric_coverage["instrumental"],
            "lyrics_platform_unavailable": lyric_coverage["platform_unavailable"],
            "lyrics_pending": lyric_coverage["pending"],
            "lyrics_missing": lyric_coverage["missing"],
            "lyrics_unresolved": lyric_coverage["unresolved"],
        }
        return {
            "path": str(self.path),
            "read_only": self.read_only,
            "schema_version": SCHEMA_VERSION,
            "metadata": metadata,
            "counts": counts,
        }

    def _search_filters(
        self,
        *,
        query: str = "",
        tags: Sequence[str] = (),
        title: str = "",
        artist: str = "",
    ) -> tuple[list[str], list[Any]]:
        clauses = ["r.canonical_analysis_id IS NOT NULL"]
        params: list[Any] = []
        if query.strip():
            candidate_sql, candidate_params = self._text_candidate_sql(query)
            clauses.append(f"r.id IN ({candidate_sql})")
            params.extend(candidate_params)
        if title.strip():
            candidate_sql, candidate_params = self._title_candidate_sql(title)
            clauses.append(f"r.id IN ({candidate_sql})")
            params.extend(candidate_params)
        if artist.strip():
            candidate_sql, candidate_params = self._artist_candidate_sql(artist)
            clauses.append(f"r.id IN ({candidate_sql})")
            params.extend(candidate_params)
        for tag in tags:
            if str(tag).strip():
                candidate_sql, candidate_params = self._tag_candidate_sql(str(tag))
                clauses.append(f"r.id IN ({candidate_sql})")
                params.extend(candidate_params)

        return clauses, params

    def search(
        self,
        *,
        query: str = "",
        tags: Sequence[str] = (),
        title: str = "",
        artist: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        clauses, params = self._search_filters(
            query=query,
            tags=tags,
            title=title,
            artist=artist,
        )

        rows = self.connection.execute(
            f"""
            SELECT r.id, r.canonical_title, r.version_label, ar.summary, ar.created_at
            FROM recording r JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.updated_at DESC, r.id LIMIT ?
            """,
            (*params, limit),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            recording_id = str(row["id"])
            source_links = self._source_links(recording_id)
            results.append(
                {
                    "recording_id": recording_id,
                    "title": str(row["canonical_title"]),
                    "version_label": str(row["version_label"]),
                    "artists": self._artist_names(recording_id),
                    "tags": self._public_tag_names(recording_id),
                    "summary": str(row["summary"]),
                    "canonical_created_at": str(row["created_at"]),
                    "listen_url": source_links[0]["url"] if source_links else None,
                    "source_links": source_links,
                }
            )
        return results

    def search_with_facets(
        self,
        *,
        query: str = "",
        tags: Sequence[str] = (),
        title: str = "",
        artist: str = "",
        limit: int = 10,
        per_namespace_limit: int = DEFAULT_SEARCH_FACETS_PER_NAMESPACE,
    ) -> dict[str, Any]:
        """Return bounded search rows plus canonical-tag counts for those rows."""

        applied_limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        results = self.search(
            query=query,
            tags=tags,
            title=title,
            artist=artist,
            limit=applied_limit,
        )
        recording_ids = [str(item["recording_id"]) for item in results]
        facet_limit = max(1, min(int(per_namespace_limit), MAX_FACET_LIMIT))
        return {
            "results": results,
            "count": len(results),
            "limit_applied": applied_limit,
            "facet_counts": self.tag_facet_counts(
                recording_ids,
                per_namespace_limit=facet_limit,
            ),
            "facet_scope": {
                "kind": "returned_results",
                "recording_count": len(results),
                "max_per_namespace": facet_limit,
            },
        }

    def discover(
        self,
        *,
        query: str = "",
        tags: Sequence[str] = (),
        title: str = "",
        artist: str = "",
        per_namespace_limit: int = DEFAULT_DISCOVERY_FACETS_PER_NAMESPACE,
    ) -> dict[str, Any]:
        """Return complete match counts and facets without serializing songs."""

        clauses, params = self._search_filters(
            query=query,
            tags=tags,
            title=title,
            artist=artist,
        )
        match_count = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM recording r WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()[0]
        )
        facet_limit = max(1, min(int(per_namespace_limit), MAX_FACET_LIMIT))
        facet_counts, facet_selection = self._tag_facet_counts_for_filters(
            clauses,
            params,
            per_namespace_limit=facet_limit,
            namespaces=DIVERSITY_NAMESPACES,
            include_cutoff_ties=True,
        )
        return {
            "match_count": match_count,
            "facet_counts": facet_counts,
            "facet_scope": {
                "kind": "all_matches",
                "recording_count": match_count,
                "facet_count": len(facet_counts),
                "namespaces": list(DIVERSITY_NAMESPACES),
                "per_namespace_target": facet_limit,
                "cutoff_ties_included": True,
                "truncated_namespaces": facet_selection["truncated_namespaces"],
            },
        }

    def recommend(
        self,
        *,
        query: str = "",
        tags: Sequence[str] = (),
        title: str = "",
        artist: str = "",
        limit: int = 5,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a compact, stable page chosen for relevance and bounded diversity."""

        applied_limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        applied_offset = max(0, int(offset))
        clauses, params = self._search_filters(
            query=query,
            tags=tags,
            title=title,
            artist=artist,
        )
        match_count = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM recording r WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()[0]
        )
        resolved_tags = self._resolved_search_tags(tags)
        excluded_tag_ids = {int(item["id"]) for item in resolved_tags}
        tag_frequency = self._candidate_tag_frequency(
            clauses,
            params,
            excluded_tag_ids=excluded_tag_ids,
        )

        selections = []
        block_offset = (applied_offset // REPRESENTATIVE_POOL_SIZE) * REPRESENTATIVE_POOL_SIZE
        within_block = applied_offset - block_offset
        while len(selections) < applied_limit and block_offset < match_count:
            ranked_block = self._ranked_candidate_block(
                clauses,
                params,
                excluded_tag_ids=excluded_tag_ids,
                offset=block_offset,
            )
            if not ranked_block:
                break
            evidence = self._candidate_evidence(
                ranked_block,
                tag_frequency=tag_frequency,
                excluded_tag_ids=excluded_tag_ids,
            )
            ordered = select_representative_candidates(evidence)
            remaining = applied_limit - len(selections)
            selections.extend(ordered[within_block : within_block + remaining])
            block_offset += REPRESENTATIVE_POOL_SIZE
            within_block = 0

        compact_results = self._compact_recommendations(
            selections,
            matched_tags=resolved_tags,
        )
        next_offset = applied_offset + len(compact_results)
        has_more = next_offset < match_count
        return {
            "results": compact_results,
            "count": len(compact_results),
            "match_count": match_count,
            "limit_applied": applied_limit,
            "offset_applied": applied_offset,
            "next_offset": next_offset if has_more else None,
            "has_more": has_more,
            "selection_scope": {
                "kind": "ranked_representative_results",
                "ranking": "required_match_then_group_representativeness",
                "diversity": "bounded_secondary_tag_coverage",
                "pool_block_size": REPRESENTATIVE_POOL_SIZE,
            },
        }

    def _tag_facet_counts_for_filters(
        self,
        clauses: Sequence[str],
        params: Sequence[Any],
        *,
        per_namespace_limit: int,
        namespaces: Sequence[str] = (),
        include_cutoff_ties: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        namespace_clause = ""
        namespace_params: list[Any] = []
        if namespaces:
            namespace_placeholders = ", ".join("?" for _ in namespaces)
            namespace_clause = f" AND t.namespace IN ({namespace_placeholders})"
            namespace_params.extend(namespaces)
        rows = self.connection.execute(
            f"""
            SELECT t.namespace, t.canonical_name AS name,
                   COUNT(DISTINCT r.id) AS tag_count
            FROM recording r
            JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
            JOIN analysis_tag at ON at.analysis_id = ar.id
            JOIN tag t ON t.id = at.tag_id
            WHERE {' AND '.join(clauses)}
              AND t.namespace NOT IN ('title', 'artist')
              {namespace_clause}
            GROUP BY t.namespace, t.canonical_name
            ORDER BY t.namespace, tag_count DESC, t.canonical_name
            """,
            (*params, *namespace_params),
        )
        returned_by_namespace: Counter[str] = Counter()
        available_by_namespace: Counter[str] = Counter()
        cutoff_by_namespace: dict[str, int] = {}
        facets: list[dict[str, Any]] = []
        for row in rows:
            namespace = str(row["namespace"])
            tag_count = int(row["tag_count"])
            available_by_namespace[namespace] += 1
            at_target = returned_by_namespace[namespace] >= per_namespace_limit
            tied_at_cutoff = (
                include_cutoff_ties
                and namespace in cutoff_by_namespace
                and tag_count == cutoff_by_namespace[namespace]
            )
            if at_target and not tied_at_cutoff:
                continue
            returned_by_namespace[namespace] += 1
            if returned_by_namespace[namespace] == per_namespace_limit:
                cutoff_by_namespace[namespace] = tag_count
            facets.append(
                {
                    "namespace": namespace,
                    "name": str(row["name"]),
                    "count": tag_count,
                }
            )
        truncated_namespaces = [
            {
                "namespace": namespace,
                "returned": returned_by_namespace[namespace],
                "available": available,
            }
            for namespace, available in sorted(available_by_namespace.items())
            if returned_by_namespace[namespace] < available
        ]
        return facets, {"truncated_namespaces": truncated_namespaces}

    def _resolved_search_tags(self, tags: Sequence[str]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for value in tags:
            key = normalized(str(value))
            if not key:
                continue
            rows = self.connection.execute(
                """
                SELECT DISTINCT t.id, t.namespace, t.canonical_name
                FROM tag t LEFT JOIN tag_alias ta ON ta.tag_id = t.id
                WHERE t.normalized_name = ? OR ta.normalized_alias = ?
                ORDER BY CASE WHEN t.normalized_name = ? THEN 0 ELSE 1 END,
                         t.namespace, t.canonical_name
                """,
                (key, key, key),
            )
            for row in rows:
                tag_id = int(row["id"])
                if tag_id in seen_ids:
                    continue
                seen_ids.add(tag_id)
                resolved.append(
                    {
                        "id": tag_id,
                        "namespace": str(row["namespace"]),
                        "name": str(row["canonical_name"]),
                    }
                )
        return resolved

    @staticmethod
    def _tag_filter_sql(excluded_tag_ids: set[int]) -> tuple[str, list[Any]]:
        namespace_placeholders = ", ".join("?" for _ in DIVERSITY_NAMESPACES)
        sql = f"t.namespace IN ({namespace_placeholders})"
        params: list[Any] = list(DIVERSITY_NAMESPACES)
        if excluded_tag_ids:
            tag_placeholders = ", ".join("?" for _ in excluded_tag_ids)
            sql += f" AND at.tag_id NOT IN ({tag_placeholders})"
            params.extend(sorted(excluded_tag_ids))
        return sql, params

    def _candidate_tag_frequency(
        self,
        clauses: Sequence[str],
        params: Sequence[Any],
        *,
        excluded_tag_ids: set[int],
    ) -> dict[int, EvidenceTag]:
        tag_filter_sql, tag_params = self._tag_filter_sql(excluded_tag_ids)
        rows = self.connection.execute(
            f"""
            WITH candidates AS (
                SELECT r.id AS recording_id, r.canonical_analysis_id AS analysis_id
                FROM recording r
                WHERE {' AND '.join(clauses)}
            )
            SELECT at.tag_id, t.namespace, t.canonical_name,
                   COUNT(DISTINCT c.recording_id) AS frequency
            FROM candidates c
            JOIN analysis_tag at ON at.analysis_id = c.analysis_id
            JOIN tag t ON t.id = at.tag_id
            WHERE {tag_filter_sql}
            GROUP BY at.tag_id, t.namespace, t.canonical_name
            """,
            (*params, *tag_params),
        )
        return {
            int(row["tag_id"]): EvidenceTag(
                namespace=str(row["namespace"]),
                name=str(row["canonical_name"]),
                frequency=int(row["frequency"]),
            )
            for row in rows
        }

    def _ranked_candidate_block(
        self,
        clauses: Sequence[str],
        params: Sequence[Any],
        *,
        excluded_tag_ids: set[int],
        offset: int,
    ) -> list[tuple[str, float]]:
        tag_filter_sql, tag_params = self._tag_filter_sql(excluded_tag_ids)
        rows = self.connection.execute(
            f"""
            WITH candidates AS (
                SELECT r.id AS recording_id, r.canonical_analysis_id AS analysis_id
                FROM recording r
                WHERE {' AND '.join(clauses)}
            ),
            tag_frequency AS (
                SELECT at.tag_id, COUNT(DISTINCT c.recording_id) AS frequency
                FROM candidates c
                JOIN analysis_tag at ON at.analysis_id = c.analysis_id
                JOIN tag t ON t.id = at.tag_id
                WHERE {tag_filter_sql}
                GROUP BY at.tag_id
            ),
            candidate_scores AS (
                SELECT c.recording_id,
                       COALESCE(SUM(tag_frequency.frequency), 0) AS representative_score
                FROM candidates c
                LEFT JOIN analysis_tag at ON at.analysis_id = c.analysis_id
                LEFT JOIN tag_frequency ON tag_frequency.tag_id = at.tag_id
                GROUP BY c.recording_id
            )
            SELECT recording_id, representative_score
            FROM candidate_scores
            ORDER BY representative_score DESC, recording_id
            LIMIT ? OFFSET ?
            """,
            (*params, *tag_params, REPRESENTATIVE_POOL_SIZE, max(0, int(offset))),
        )
        return [
            (str(row["recording_id"]), float(row["representative_score"]))
            for row in rows
        ]

    def _candidate_evidence(
        self,
        ranked_block: Sequence[tuple[str, float]],
        *,
        tag_frequency: Mapping[int, EvidenceTag],
        excluded_tag_ids: set[int],
    ) -> list[CandidateEvidence]:
        if not ranked_block:
            return []
        recording_ids = [recording_id for recording_id, _ in ranked_block]
        placeholders = ", ".join("?" for _ in recording_ids)
        tag_filter_sql, tag_params = self._tag_filter_sql(excluded_tag_ids)
        rows = self.connection.execute(
            f"""
            SELECT r.id AS recording_id, at.tag_id
            FROM recording r
            JOIN analysis_tag at ON at.analysis_id = r.canonical_analysis_id
            JOIN tag t ON t.id = at.tag_id
            WHERE r.id IN ({placeholders}) AND {tag_filter_sql}
            ORDER BY r.id, t.namespace, t.canonical_name
            """,
            (*recording_ids, *tag_params),
        )
        tags_by_recording: dict[str, list[EvidenceTag]] = {
            recording_id: [] for recording_id in recording_ids
        }
        for row in rows:
            tag = tag_frequency.get(int(row["tag_id"]))
            if tag is not None:
                tags_by_recording[str(row["recording_id"])].append(tag)
        return [
            CandidateEvidence(
                recording_id=recording_id,
                representative_score=representative_score,
                tags=tuple(tags_by_recording[recording_id]),
            )
            for recording_id, representative_score in ranked_block
        ]

    def _compact_recommendations(
        self,
        selections: Sequence[CandidateSelection],
        *,
        matched_tags: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not selections:
            return []
        recording_ids = [str(selection.recording_id) for selection in selections]
        placeholders = ", ".join("?" for _ in recording_ids)
        rows = self.connection.execute(
            f"""
            SELECT id, canonical_title, version_label
            FROM recording
            WHERE id IN ({placeholders})
            """,
            tuple(recording_ids),
        )
        recordings = {str(row["id"]): row for row in rows}
        public_matched_tags = [
            {"namespace": str(tag["namespace"]), "name": str(tag["name"])}
            for tag in matched_tags
        ]
        results: list[dict[str, Any]] = []
        for selection in selections:
            recording_id = str(selection.recording_id)
            row = recordings[recording_id]
            source_links = self._source_links(recording_id)
            results.append(
                {
                    "recording_id": recording_id,
                    "title": str(row["canonical_title"]),
                    "version_label": str(row["version_label"]),
                    "artists": self._artist_names(recording_id),
                    "matched_tags": public_matched_tags,
                    "representative_tags": [
                        {"namespace": tag.namespace, "name": tag.name}
                        for tag in selection.representative_tags
                    ],
                    "selection_basis": str(selection.selection_basis),
                    "listen_url": source_links[0]["url"] if source_links else None,
                }
            )
        return results

    def tag_facet_counts(
        self,
        recording_ids: Sequence[str],
        *,
        per_namespace_limit: int = DEFAULT_SEARCH_FACETS_PER_NAMESPACE,
    ) -> list[dict[str, Any]]:
        """Count canonical analysis tags over a bounded set of returned recordings.

        Identity-only ``recording_tag`` rows and tag aliases are intentionally
        excluded, so titles and artists cannot become musical facet evidence.
        """

        bounded_ids = list(
            dict.fromkeys(
                str(recording_id).strip()
                for recording_id in recording_ids
                if str(recording_id).strip()
            )
        )[:MAX_SEARCH_LIMIT]
        if not bounded_ids:
            return []

        per_namespace_limit = max(1, min(int(per_namespace_limit), MAX_FACET_LIMIT))
        placeholders = ", ".join("?" for _ in bounded_ids)
        rows = self.connection.execute(
            f"""
            SELECT t.namespace, t.canonical_name AS name,
                   COUNT(DISTINCT r.id) AS tag_count
            FROM recording r
            JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
            JOIN analysis_tag at ON at.analysis_id = ar.id
            JOIN tag t ON t.id = at.tag_id
            WHERE r.id IN ({placeholders})
              AND t.namespace NOT IN ('title', 'artist')
            GROUP BY t.namespace, t.canonical_name
            ORDER BY t.namespace, tag_count DESC, t.canonical_name
            """,
            tuple(bounded_ids),
        )
        counts_by_namespace: Counter[str] = Counter()
        facets: list[dict[str, Any]] = []
        for row in rows:
            namespace = str(row["namespace"])
            if counts_by_namespace[namespace] >= per_namespace_limit:
                continue
            counts_by_namespace[namespace] += 1
            facets.append(
                {
                    "namespace": namespace,
                    "name": str(row["name"]),
                    "count": int(row["tag_count"]),
                }
            )
        return facets

    def _text_candidate_sql(self, value: str) -> tuple[str, list[Any]]:
        key = normalized(value)
        wildcard = f"%{key}%"
        fallback_sql = """
            SELECT DISTINCT r_query.id
            FROM recording r_query
            LEFT JOIN title_alias ta_query ON ta_query.recording_id = r_query.id
            LEFT JOIN recording_artist ra_query ON ra_query.recording_id = r_query.id
            LEFT JOIN artist a_query ON a_query.id = ra_query.artist_id
            LEFT JOIN artist_alias aa_query ON aa_query.artist_id = a_query.id
            WHERE r_query.canonical_analysis_id IS NOT NULL AND (
                r_query.normalized_title LIKE ? OR ta_query.normalized_alias LIKE ?
                OR a_query.normalized_name LIKE ? OR aa_query.normalized_alias LIKE ?
            )
        """
        fallback_params: list[Any] = [wildcard, wildcard, wildcard, wildcard]
        query = fts_query(value)
        if query and self._fts_query_is_usable(query):
            # FTS is the scalable full-text path, but it tokenizes ``Rockstar``
            # separately from ``rock``. The normalized identity fallback must
            # remain a UNION, not a data-dependent fallback, or a raw-analysis
            # FTS hit would hide a partial title/artist alias match.
            return (
                "SELECT recording_id FROM search_fts WHERE search_fts MATCH ? UNION ALL " + fallback_sql,
                [query, *fallback_params],
            )
        return fallback_sql, fallback_params

    def _fts_query_is_usable(self, query: str) -> bool:
        try:
            self.connection.execute(
                "SELECT 1 FROM search_fts WHERE search_fts MATCH ? LIMIT 1", (query,)
            ).fetchone()
        except sqlite3.OperationalError:
            # Preserve normalized title/artist lookup if an SQLite build or
            # tokenizer rejects an otherwise sanitized FTS expression.
            return False
        return True

    @staticmethod
    def _title_candidate_sql(value: str) -> tuple[str, list[Any]]:
        key = normalized(value)
        wildcard = f"%{key}%"
        return (
            """
            SELECT DISTINCT r_title.id
            FROM recording r_title
            LEFT JOIN title_alias ta_title ON ta_title.recording_id = r_title.id
            WHERE r_title.canonical_analysis_id IS NOT NULL
              AND (r_title.normalized_title LIKE ? OR ta_title.normalized_alias LIKE ?)
            """,
            [wildcard, wildcard],
        )

    @staticmethod
    def _artist_candidate_sql(value: str) -> tuple[str, list[Any]]:
        key = normalized(value)
        wildcard = f"%{key}%"
        return (
            """
            SELECT DISTINCT r_artist.id
            FROM recording r_artist
            JOIN recording_artist ra_artist ON ra_artist.recording_id = r_artist.id
            JOIN artist a_artist ON a_artist.id = ra_artist.artist_id
            LEFT JOIN artist_alias aa_artist ON aa_artist.artist_id = a_artist.id
            WHERE r_artist.canonical_analysis_id IS NOT NULL
              AND (a_artist.normalized_name LIKE ? OR aa_artist.normalized_alias LIKE ?)
            """,
            [wildcard, wildcard],
        )

    @staticmethod
    def _tag_candidate_sql(value: str) -> tuple[str, list[Any]]:
        key = normalized(value)
        return (
            """
            SELECT ar_direct.recording_id
            FROM tag t_direct
            JOIN analysis_tag at_direct ON at_direct.tag_id = t_direct.id
            JOIN analysis_revision ar_direct ON ar_direct.id = at_direct.analysis_id
            JOIN recording r_direct ON r_direct.canonical_analysis_id = ar_direct.id
            WHERE t_direct.normalized_name = ?
            UNION ALL
            SELECT ar_alias.recording_id
            FROM tag_alias ta_analysis
            JOIN analysis_tag at_alias ON at_alias.tag_id = ta_analysis.tag_id
            JOIN analysis_revision ar_alias ON ar_alias.id = at_alias.analysis_id
            JOIN recording r_alias ON r_alias.canonical_analysis_id = ar_alias.id
            WHERE ta_analysis.normalized_alias = ?
            UNION ALL
            SELECT rt_direct.recording_id
            FROM tag t_identity_direct
            JOIN recording_tag rt_direct ON rt_direct.tag_id = t_identity_direct.id
            JOIN recording r_identity_direct ON r_identity_direct.id = rt_direct.recording_id
            WHERE r_identity_direct.canonical_analysis_id IS NOT NULL
              AND t_identity_direct.normalized_name = ?
            UNION ALL
            SELECT rt_alias.recording_id
            FROM tag_alias ta_identity
            JOIN recording_tag rt_alias ON rt_alias.tag_id = ta_identity.tag_id
            JOIN recording r_identity_alias ON r_identity_alias.id = rt_alias.recording_id
            WHERE r_identity_alias.canonical_analysis_id IS NOT NULL
              AND ta_identity.normalized_alias = ?
            """,
            [key, key, key, key],
        )

    def get_canonical_analysis(self, recording_id: str, *, max_chars: int = 24_000) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT r.id, r.canonical_title, r.version_label, r.audio_sha256,
                   ar.id AS analysis_id, ar.raw_text, ar.summary, ar.model_version,
                   ar.prompt_version, ar.generated_token_count, ar.quality_state, ar.created_at
            FROM recording r
            JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
            WHERE r.id = ?
            """,
            (recording_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"No canonical analysis found for recording {recording_id}")
        analysis_id = str(row["analysis_id"])
        raw_text = str(row["raw_text"])
        bounded = raw_text[: max(1, min(int(max_chars), 50_000))]
        tag_rows = self.connection.execute(
            """
            SELECT t.namespace, t.canonical_name, t.path, t.lifecycle_status,
                   at.confidence, at.source
            FROM analysis_tag at JOIN tag t ON t.id = at.tag_id
            WHERE at.analysis_id = ? ORDER BY t.namespace, t.canonical_name
            """,
            (analysis_id,),
        )
        numeric_rows = self.connection.execute(
            "SELECT name, value, unit, confidence, source FROM numeric_feature WHERE analysis_id = ? ORDER BY name",
            (analysis_id,),
        )
        provenance_rows = self.connection.execute(
            """
            SELECT delivery_schema_version, campaign_id, delivery_id,
                   manifest_index, source_title, source_artist,
                   relative_audio_path, source_sha256,
                   source_bytes, output_text_sha256, generated_token_count,
                   max_new_tokens, contract, attempt_id, canonical_source,
                   provenance_json, imported_at
            FROM campaign_delivery_provenance
            WHERE analysis_id = ?
            ORDER BY canonical_source, manifest_index, delivery_id
            """,
            (analysis_id,),
        )
        delivery_provenance = _rows_to_dicts(provenance_rows)
        for item in delivery_provenance:
            raw_provenance = item.pop("provenance_json")
            item["provenance"] = json.loads(str(raw_provenance)) if raw_provenance else None
        source_links = self._source_links(str(row["id"]))
        return {
            "recording_id": str(row["id"]),
            "title": str(row["canonical_title"]),
            "version_label": str(row["version_label"]),
            "audio_sha256": row["audio_sha256"],
            "artists": self._artist_names(str(row["id"])),
            "listen_url": source_links[0]["url"] if source_links else None,
            "source_links": source_links,
            "title_aliases": [
                str(alias["alias"])
                for alias in self.connection.execute(
                    "SELECT alias FROM title_alias WHERE recording_id = ? ORDER BY alias", (recording_id,)
                )
            ],
            "analysis": {
                "id": analysis_id,
                "summary": str(row["summary"]),
                "raw_text": bounded,
                "raw_text_truncated": len(bounded) < len(raw_text),
                "model_version": str(row["model_version"]),
                "prompt_version": str(row["prompt_version"]),
                "generated_token_count": row["generated_token_count"],
                "quality_state": str(row["quality_state"]),
                "created_at": str(row["created_at"]),
            },
            "tags": _rows_to_dicts(tag_rows),
            "numeric_features": _rows_to_dicts(numeric_rows),
            "campaign_delivery_provenance": delivery_provenance,
        }

    def tag_facets(
        self, *, namespace: str = "", prefix: str = "", limit: int = 30
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_FACET_LIMIT))
        clauses = []
        params: list[Any] = []
        if namespace.strip():
            clauses.append("t.namespace = ?")
            params.append(normalized(namespace))
        if prefix.strip():
            key = normalized(prefix)
            clauses.append("(t.normalized_name LIKE ? OR ta.normalized_alias LIKE ?)")
            params.extend([f"{key}%", f"{key}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT t.id, t.namespace, t.canonical_name, t.path,
                   t.lifecycle_status
            FROM tag t LEFT JOIN tag_alias ta ON ta.tag_id = t.id
            {where}
            ORDER BY t.namespace, t.canonical_name LIMIT ?
            """,
            (*params, limit),
        )
        result = _rows_to_dicts(rows)
        for item in result:
            item["aliases"] = [
                str(alias["alias"])
                for alias in self.connection.execute(
                    "SELECT alias FROM tag_alias WHERE tag_id = ? ORDER BY alias", (item["id"],)
                )
            ]
        return result

    # -- validation -------------------------------------------------------

    def validate(self, *, require_lyrics: bool = False) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        projection_state = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (SEARCH_PROJECTION_STATE_KEY,)
        ).fetchone()
        if projection_state is not None and str(projection_state["value"]) != SEARCH_PROJECTION_CURRENT:
            issues.append({"code": "search_projection_dirty", "state": str(projection_state["value"])})
        foreign_keys = list(self.connection.execute("PRAGMA foreign_key_check"))
        for row in foreign_keys:
            issues.append({"code": "foreign_key", "table": row[0], "rowid": row[1], "parent": row[2]})
        checks = [
            (
                "canonical_pointer_missing_or_wrong",
                """
                SELECT r.id FROM recording r
                LEFT JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
                WHERE r.canonical_analysis_id IS NOT NULL
                  AND (ar.id IS NULL OR ar.recording_id <> r.id OR ar.quality_state <> 'passed' OR ar.status <> 'canonical')
                """,
            ),
            (
                "multiple_canonical_revisions",
                "SELECT recording_id AS id FROM analysis_revision WHERE status = 'canonical' GROUP BY recording_id HAVING COUNT(*) > 1",
            ),
            (
                "orphan_canonical_status",
                """
                SELECT ar.recording_id AS id FROM analysis_revision ar
                JOIN recording r ON r.id = ar.recording_id
                WHERE ar.status = 'canonical' AND r.canonical_analysis_id <> ar.id
                """,
            ),
            (
                "missing_search_projection",
                """
                SELECT r.id
                FROM recording r
                WHERE r.canonical_analysis_id IS NOT NULL
                EXCEPT
                SELECT f.recording_id FROM search_fts f
                """,
            ),
            (
                "campaign_delivery_output_hash_mismatch",
                """
                SELECT c.delivery_id AS id
                FROM campaign_delivery_provenance c
                JOIN analysis_revision ar ON ar.id = c.analysis_id
                WHERE c.output_text_sha256 IS NOT ar.output_sha256
                """,
            ),
            (
                "campaign_delivery_source_hash_mismatch",
                """
                SELECT c.delivery_id AS id
                FROM campaign_delivery_provenance c
                JOIN analysis_revision ar ON ar.id = c.analysis_id
                JOIN recording r ON r.id = ar.recording_id
                WHERE c.source_sha256 IS NOT r.audio_sha256
                """,
            ),
            (
                "campaign_delivery_token_count_mismatch",
                """
                SELECT c.delivery_id AS id
                FROM campaign_delivery_provenance c
                JOIN analysis_revision ar ON ar.id = c.analysis_id
                WHERE c.generated_token_count IS NOT ar.generated_token_count
                """,
            ),
            (
                "campaign_delivery_source_track_mismatch",
                """
                SELECT c.delivery_id AS id
                FROM campaign_delivery_provenance c
                JOIN analysis_revision ar ON ar.id = c.analysis_id
                LEFT JOIN source_track st
                  ON st.recording_id = ar.recording_id
                 AND st.source_name = 'kugou'
                 AND st.source_track_id = c.delivery_id
                WHERE st.id IS NULL
                """,
            ),
            (
                "campaign_delivery_source_bytes_inconsistent",
                """
                SELECT source_sha256 AS id
                FROM campaign_delivery_provenance
                GROUP BY source_sha256
                HAVING MIN(source_bytes) <> MAX(source_bytes)
                """,
            ),
        ]
        for code, sql in checks:
            for row in self.connection.execute(sql):
                issues.append({"code": code, "recording_id": str(row[0])})
        lyric_rows = self.connection.execute(
            """
            SELECT rl.recording_id, rl.source_track_row_id, rl.status,
                   rl.lyric_text, rl.text_sha256, rl.evidence_json,
                   st.recording_id AS source_recording_id, st.source_name,
                   st.source_track_id
            FROM recording_lyric rl
            LEFT JOIN source_track st ON st.id = rl.source_track_row_id
            """
        )
        for row in lyric_rows:
            recording_id = str(row["recording_id"])
            if row["source_recording_id"] is None or str(row["source_recording_id"]) != recording_id:
                issues.append({"code": "lyric_source_track_mismatch", "recording_id": recording_id})
                continue
            if row["status"] == LYRIC_STATUS_AVAILABLE:
                lyric_text = str(row["lyric_text"] or "")
                if lyric_text_sha256(lyric_text) != str(row["text_sha256"] or ""):
                    issues.append({"code": "lyric_text_hash_mismatch", "recording_id": recording_id})
            try:
                evidence = json.loads(str(row["evidence_json"]))
            except (TypeError, json.JSONDecodeError):
                issues.append({"code": "lyric_evidence_invalid_json", "recording_id": recording_id})
                continue
            if not isinstance(evidence, Mapping):
                issues.append({"code": "lyric_evidence_not_object", "recording_id": recording_id})
                continue
            evidence_source = str(evidence.get("source_name") or "")
            evidence_track_id = str(evidence.get("source_track_id") or "")
            if (
                normalized(evidence_source) != normalized(str(row["source_name"]))
                or evidence_track_id != str(row["source_track_id"])
            ):
                issues.append({"code": "lyric_evidence_identity_mismatch", "recording_id": recording_id})
        lyric_coverage = self.lyric_coverage()
        if require_lyrics and lyric_coverage["unresolved"]:
            issues.append({"code": "lyrics_coverage_incomplete", **lyric_coverage})
        return {
            "valid": not issues,
            "issues": issues,
            "issue_count": len(issues),
            "lyrics_coverage": lyric_coverage,
        }


def _decode_jsonl_object(raw_line: bytes, line_number: int) -> dict[str, Any]:
    try:
        line = raw_line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"Invalid UTF-8 JSONL at line {line_number}") from exc
    try:
        item = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSONL at line {line_number}: {exc.msg}") from exc
    if not isinstance(item, dict):
        raise ValidationError(f"JSONL line {line_number} must be an object")
    return item


def iter_import_file(path: str | Path) -> Iterable[dict[str, Any]]:
    """Yield generic import records, streaming physical-LF JSONL by extension.

    A JSON object or array necessarily uses the conventional JSON decoder, but
    the scalable path is an input named ``.jsonl`` or ``.ndjson``. Those files
    are decoded one physical LF record at a time, preserving U+2028/U+2029
    within JSON strings and never materializing the full corpus.
    """

    source = Path(path)
    if not source.is_file():
        raise ValidationError(f"Import input does not exist: {source}")
    if source.suffix.casefold() not in {".jsonl", ".ndjson"}:
        yield from load_import_file(source)
        return

    use_legacy_decoder = False
    try:
        with source.open("rb") as handle:
            lines = enumerate(handle, 1)
            first: tuple[int, bytes] | None = None
            for line_number, raw_line in lines:
                if raw_line.strip():
                    first = (line_number, raw_line)
                    break
            if first is None:
                return
            first_line_number, first_raw_line = first
            # Preserve the old permissive behavior for a JSON array saved
            # under a .jsonl name. A real JSONL corpus always starts with an
            # object and remains fully streaming below.
            if first_raw_line.lstrip().startswith(b"["):
                use_legacy_decoder = True
            else:
                try:
                    first_item = _decode_jsonl_object(first_raw_line, first_line_number)
                except ValidationError:
                    # A pretty-printed single JSON object starts with `{` on
                    # its own physical line. Let the legacy whole-file parser
                    # retain that supported input shape without penalizing a
                    # valid physical-LF JSONL stream.
                    use_legacy_decoder = True
                else:
                    yield first_item
                    for line_number, raw_line in lines:
                        if raw_line.strip():
                            yield _decode_jsonl_object(raw_line, line_number)
    except OSError as exc:
        raise ValidationError(f"Unable to read import input: {source}") from exc
    if use_legacy_decoder:
        yield from load_import_file(source)


def load_import_file(path: str | Path) -> list[dict[str, Any]]:
    """Load one JSON object/list or a physical-line JSONL file safely."""

    source = Path(path)
    if not source.is_file():
        raise ValidationError(f"Import input does not exist: {source}")
    raw = source.read_text(encoding="utf-8")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        # split("\n") is deliberate: U+2028/U+2029 inside valid JSON strings
        # are not physical JSONL record boundaries.
        for line_number, line in enumerate(raw.split("\n"), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid JSONL at line {line_number}: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValidationError(f"JSONL line {line_number} must be an object")
            records.append(item)
        return records
    if isinstance(decoded, dict):
        return [decoded]
    if isinstance(decoded, list) and all(isinstance(item, dict) for item in decoded):
        return list(decoded)
    raise ValidationError("Import JSON must be an object, object array, or JSONL objects")
