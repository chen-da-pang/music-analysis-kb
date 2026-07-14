from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .campaign_delivery import CampaignDeliveryEntry, group_campaign_delivery, to_import_payload
from .errors import NotFoundError, ValidationError
from .normalization import fts_query, normalized, require_text
from .schema import SCHEMA_VERSION, connect, ensure_initialized
from .tagging import PARSER_SOURCE, extract_music_flamingo_metadata


MAX_SEARCH_LIMIT = 50
MAX_FACET_LIMIT = 100
DEFAULT_ENRICH_BATCH_SIZE = 500
DEFAULT_IMPORT_BATCH_SIZE = 500
IMPORT_LOOKUP_CACHE_SIZE = 8_192
MAX_IMPORT_RESULT_SAMPLE = 1_000
SEARCH_PROJECTION_STATE_KEY = "search_projection_state"
SEARCH_PROJECTION_CURRENT = "current"
SEARCH_PROJECTION_DIRTY = "dirty"


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
                    source_artist_credit = COALESCE(?, source_artist_credit)
                WHERE source_name = ? AND source_track_id = ?
                """,
                (
                    source_name,
                    str(source.get("source_title") or "").strip() or None,
                    str(source.get("source_artist_credit") or "").strip() or None,
                    str(existing["source_name"]),
                    source_track_id,
                ),
            )
            return
        source_id = _stable_id("src", normalized(source_name), source_track_id)
        self.connection.execute(
            """
            INSERT INTO source_track(id, recording_id, source_name, source_track_id, source_title, source_artist_credit)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, source_track_id) DO UPDATE SET
                source_title = COALESCE(excluded.source_title, source_track.source_title),
                source_artist_credit = COALESCE(excluded.source_artist_credit, source_track.source_artist_credit)
            """,
            (
                source_id,
                recording_id,
                source_name,
                source_track_id,
                str(source.get("source_title") or "").strip() or None,
                str(source.get("source_artist_credit") or "").strip() or None,
            ),
        )

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

    # -- reads ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        metadata = {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute("SELECT key, value FROM meta ORDER BY key")
        }
        counts = {
            "recordings": self.connection.execute("SELECT COUNT(*) FROM recording").fetchone()[0],
            "canonical_analyses": self.connection.execute(
                "SELECT COUNT(*) FROM recording WHERE canonical_analysis_id IS NOT NULL"
            ).fetchone()[0],
            "analysis_revisions": self.connection.execute("SELECT COUNT(*) FROM analysis_revision").fetchone()[0],
            "campaign_delivery_provenance": self.connection.execute(
                "SELECT COUNT(*) FROM campaign_delivery_provenance"
            ).fetchone()[0],
            "tags": self.connection.execute("SELECT COUNT(*) FROM tag").fetchone()[0],
        }
        return {
            "path": str(self.path),
            "read_only": self.read_only,
            "schema_version": SCHEMA_VERSION,
            "metadata": metadata,
            "counts": counts,
        }

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
            results.append(
                {
                    "recording_id": recording_id,
                    "title": str(row["canonical_title"]),
                    "version_label": str(row["version_label"]),
                    "artists": self._artist_names(recording_id),
                    "tags": self._public_tag_names(recording_id),
                    "summary": str(row["summary"]),
                    "canonical_created_at": str(row["created_at"]),
                }
            )
        return results

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
        return {
            "recording_id": str(row["id"]),
            "title": str(row["canonical_title"]),
            "version_label": str(row["version_label"]),
            "audio_sha256": row["audio_sha256"],
            "artists": self._artist_names(str(row["id"])),
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

    def validate(self) -> dict[str, Any]:
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
        return {"valid": not issues, "issues": issues, "issue_count": len(issues)}


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
