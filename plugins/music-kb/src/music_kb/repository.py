from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import NotFoundError, ValidationError
from .normalization import fts_query, normalized, require_text
from .schema import SCHEMA_VERSION, connect, ensure_initialized


MAX_SEARCH_LIMIT = 50
MAX_FACET_LIMIT = 100


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class MusicKBRepository:
    """All database operations. MCP uses only its read methods."""

    def __init__(self, database: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(database).expanduser().resolve()
        self.read_only = read_only
        self.connection = connect(self.path, read_only=read_only)
        ensure_initialized(self.connection)

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

    # -- importer ---------------------------------------------------------

    def import_analysis(self, payload: Mapping[str, Any]) -> dict[str, Any]:
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
        raw_text = require_text(analysis.get("raw_text") or payload.get("raw_text"), "analysis.raw_text")
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

        with self.connection:
            existing = self.connection.execute(
                "SELECT id FROM analysis_revision WHERE recording_id = ? AND output_sha256 = ?",
                (recording_id, output_sha256),
            ).fetchone()
            if existing:
                existing_id = str(existing["id"])
                if canonical_requested:
                    self._set_canonical(recording_id, existing_id)
                    self.rebuild_search_projection(recording_id)
                return {
                    "recording_id": recording_id,
                    "analysis_id": existing_id,
                    "idempotent": True,
                    "canonical": canonical_requested,
                }

            self._upsert_recording(recording_id, title, version_label, audio_sha256)
            self._upsert_title_aliases(recording_id, [title, *title_aliases])
            for position, artist in enumerate(artists):
                artist_id = self._upsert_artist(artist["name"], artist["aliases"])
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO recording_artist(recording_id, artist_id, role, position)
                    VALUES (?, ?, ?, ?)
                    """,
                    (recording_id, artist_id, artist["role"], position),
                )

            self._insert_identity_tags(recording_id, title, title_aliases, artists)
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
                tag_id, confidence = self._upsert_tag_from_payload(tag_data)
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
            self.rebuild_search_projection(recording_id)

        return {
            "recording_id": recording_id,
            "analysis_id": analysis_id,
            "idempotent": False,
            "canonical": canonical_requested,
        }

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

    def _upsert_artist(self, name: str, aliases: Sequence[str]) -> str:
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
    ) -> int:
        namespace = require_text(namespace, "tag.namespace")
        name = require_text(name, "tag.name")
        namespace_key = normalized(namespace)
        lifecycle_status = lifecycle_status or "candidate"
        if lifecycle_status not in {"candidate", "approved", "deprecated"}:
            raise ValidationError("tag.status must be candidate, approved, or deprecated")
        self.connection.execute(
            "INSERT OR IGNORE INTO tag_namespace(name) VALUES (?)", (namespace_key,)
        )
        self.connection.execute(
            """
            INSERT INTO tag(namespace, canonical_name, normalized_name, path, lifecycle_status, suno_safe)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, normalized_name) DO UPDATE SET
                path = CASE WHEN excluded.path <> '' THEN excluded.path ELSE tag.path END,
                lifecycle_status = CASE
                    WHEN tag.lifecycle_status = 'approved' THEN tag.lifecycle_status
                    ELSE excluded.lifecycle_status
                END,
                suno_safe = MAX(tag.suno_safe, excluded.suno_safe)
            """,
            (namespace_key, name, normalized(name), path or "", lifecycle_status, int(bool(suno_safe))),
        )
        row = self.connection.execute(
            "SELECT id FROM tag WHERE namespace = ? AND normalized_name = ?",
            (namespace_key, normalized(name)),
        ).fetchone()
        assert row is not None
        tag_id = int(row["id"])
        for alias in [name, *aliases]:
            if alias:
                self.connection.execute(
                    "INSERT OR IGNORE INTO tag_alias(tag_id, alias, normalized_alias) VALUES (?, ?, ?)",
                    (tag_id, alias, normalized(alias)),
                )
        return tag_id

    def _insert_identity_tags(
        self,
        recording_id: str,
        title: str,
        title_aliases: Sequence[str],
        artists: Sequence[Mapping[str, Any]],
    ) -> None:
        title_tag = self._ensure_tag("title", title, title_aliases, lifecycle_status="approved")
        self.connection.execute(
            "INSERT OR IGNORE INTO recording_tag(recording_id, tag_id, role) VALUES (?, ?, 'title')",
            (recording_id, title_tag),
        )
        for artist in artists:
            artist_tag = self._ensure_tag(
                "artist", str(artist["name"]), artist["aliases"], lifecycle_status="approved"
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO recording_tag(recording_id, tag_id, role) VALUES (?, ?, 'artist')",
                (recording_id, artist_tag),
            )

    def _upsert_source_track(self, recording_id: str, source: Any) -> None:
        if not isinstance(source, Mapping):
            raise ValidationError("Each source_track must be an object")
        source_name = require_text(source.get("source") or source.get("source_name"), "source_track.source")
        source_track_id = require_text(source.get("source_track_id"), "source_track.source_track_id")
        source_id = _stable_id("src", normalized(source_name), source_track_id)
        self.connection.execute(
            """
            INSERT INTO source_track(id, recording_id, source_name, source_track_id, source_title, source_artist_credit)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, source_track_id) DO UPDATE SET
                recording_id = excluded.recording_id,
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

    def _upsert_tag_from_payload(self, tag: Any) -> tuple[int, float | None]:
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
        self.connection.execute(
            """
            INSERT OR REPLACE INTO numeric_feature(analysis_id, name, value, unit, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (analysis_id, normalized(name), value, str(feature.get("unit") or "").strip(), confidence),
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

    # -- canonical search projection -------------------------------------

    def rebuild_search_projection(self, recording_id: str) -> None:
        self._require_writer()
        self.connection.execute("DELETE FROM search_fts WHERE recording_id = ?", (recording_id,))
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
                " ".join(tags),
                analysis,
            ),
        )

    def rebuild_all_search_projections(self) -> int:
        self._require_writer()
        recording_ids = [
            str(row["id"])
            for row in self.connection.execute(
                "SELECT id FROM recording WHERE canonical_analysis_id IS NOT NULL"
            )
        ]
        with self.connection:
            self.connection.execute("DELETE FROM search_fts")
            for recording_id in recording_ids:
                self.rebuild_search_projection(recording_id)
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
        candidate_ids: set[str] | None = None
        if query.strip():
            candidate_ids = self._intersect(candidate_ids, self._text_ids(query))
        if title.strip():
            candidate_ids = self._intersect(candidate_ids, self._title_ids(title))
        if artist.strip():
            candidate_ids = self._intersect(candidate_ids, self._artist_ids(artist))
        for tag in tags:
            if str(tag).strip():
                candidate_ids = self._intersect(candidate_ids, self._tag_ids(str(tag)))
        if candidate_ids is not None and not candidate_ids:
            return []

        if candidate_ids is None:
            rows = self.connection.execute(
                """
                SELECT r.id, r.canonical_title, r.version_label, ar.summary, ar.created_at
                FROM recording r JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
                ORDER BY r.updated_at DESC, r.id LIMIT ?
                """,
                (limit,),
            )
        else:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = self.connection.execute(
                f"""
                SELECT r.id, r.canonical_title, r.version_label, ar.summary, ar.created_at
                FROM recording r JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
                WHERE r.id IN ({placeholders})
                ORDER BY r.updated_at DESC, r.id LIMIT ?
                """,
                (*sorted(candidate_ids), limit),
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

    @staticmethod
    def _intersect(current: set[str] | None, incoming: set[str]) -> set[str]:
        return incoming if current is None else current.intersection(incoming)

    def _text_ids(self, value: str) -> set[str]:
        key = normalized(value)
        ids: set[str] = set()
        query = fts_query(value)
        if query:
            try:
                ids.update(
                    str(row["recording_id"])
                    for row in self.connection.execute(
                        "SELECT recording_id FROM search_fts WHERE search_fts MATCH ?", (query,)
                    )
                )
            except sqlite3.OperationalError:
                # FTS syntax/tokenizer differences must not make exact alias
                # search fail. The normalized lookup below remains available.
                pass
        wildcard = f"%{key}%"
        ids.update(
            str(row["id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT r.id FROM recording r
                LEFT JOIN title_alias ta ON ta.recording_id = r.id
                LEFT JOIN recording_artist ra ON ra.recording_id = r.id
                LEFT JOIN artist a ON a.id = ra.artist_id
                LEFT JOIN artist_alias aa ON aa.artist_id = a.id
                WHERE r.canonical_analysis_id IS NOT NULL AND (
                    r.normalized_title LIKE ? OR ta.normalized_alias LIKE ?
                    OR a.normalized_name LIKE ? OR aa.normalized_alias LIKE ?
                )
                """,
                (wildcard, wildcard, wildcard, wildcard),
            )
        )
        return ids

    def _title_ids(self, value: str) -> set[str]:
        key = normalized(value)
        wildcard = f"%{key}%"
        return {
            str(row["id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT r.id FROM recording r
                LEFT JOIN title_alias ta ON ta.recording_id = r.id
                WHERE r.canonical_analysis_id IS NOT NULL
                  AND (r.normalized_title LIKE ? OR ta.normalized_alias LIKE ?)
                """,
                (wildcard, wildcard),
            )
        }

    def _artist_ids(self, value: str) -> set[str]:
        key = normalized(value)
        wildcard = f"%{key}%"
        return {
            str(row["id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT r.id FROM recording r
                JOIN recording_artist ra ON ra.recording_id = r.id
                JOIN artist a ON a.id = ra.artist_id
                LEFT JOIN artist_alias aa ON aa.artist_id = a.id
                WHERE r.canonical_analysis_id IS NOT NULL
                  AND (a.normalized_name LIKE ? OR aa.normalized_alias LIKE ?)
                """,
                (wildcard, wildcard),
            )
        }

    def _tag_ids(self, value: str) -> set[str]:
        key = normalized(value)
        return {
            str(row["recording_id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT ar.recording_id
                FROM analysis_revision ar
                JOIN analysis_tag at ON at.analysis_id = ar.id
                JOIN tag t ON t.id = at.tag_id
                LEFT JOIN tag_alias ta ON ta.tag_id = t.id
                JOIN recording r ON r.canonical_analysis_id = ar.id
                WHERE t.normalized_name = ? OR ta.normalized_alias = ?
                UNION
                SELECT DISTINCT rt.recording_id
                FROM recording_tag rt
                JOIN tag t ON t.id = rt.tag_id
                LEFT JOIN tag_alias ta ON ta.tag_id = t.id
                JOIN recording r ON r.id = rt.recording_id
                WHERE r.canonical_analysis_id IS NOT NULL
                  AND (t.normalized_name = ? OR ta.normalized_alias = ?)
                """,
                (key, key, key, key),
            )
        }

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
                   t.suno_safe, at.confidence, at.source
            FROM analysis_tag at JOIN tag t ON t.id = at.tag_id
            WHERE at.analysis_id = ? ORDER BY t.namespace, t.canonical_name
            """,
            (analysis_id,),
        )
        numeric_rows = self.connection.execute(
            "SELECT name, value, unit, confidence FROM numeric_feature WHERE analysis_id = ? ORDER BY name",
            (analysis_id,),
        )
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
                   t.lifecycle_status, t.suno_safe
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
            item["suno_safe"] = bool(item["suno_safe"])
        return result

    def compile_suno_style(
        self,
        *,
        recording_ids: Sequence[str] = (),
        selected_tags: Sequence[str] = (),
        max_tags: int = 24,
    ) -> dict[str, Any]:
        """Compile only approved audible descriptors; never expose identity text."""

        max_tags = max(1, min(int(max_tags), 48))
        safe_tags: dict[int, dict[str, Any]] = {}
        for recording_id in recording_ids:
            rows = self.connection.execute(
                """
                SELECT DISTINCT t.id, t.namespace, t.canonical_name, t.path
                FROM recording r
                JOIN analysis_revision ar ON ar.id = r.canonical_analysis_id
                JOIN analysis_tag at ON at.analysis_id = ar.id
                JOIN tag t ON t.id = at.tag_id
                WHERE r.id = ? AND t.suno_safe = 1
                """,
                (recording_id,),
            )
            for row in rows:
                safe_tags[int(row["id"])] = dict(row)
        for tag_name in selected_tags:
            key = normalized(str(tag_name))
            rows = self.connection.execute(
                """
                SELECT DISTINCT t.id, t.namespace, t.canonical_name, t.path
                FROM tag t LEFT JOIN tag_alias ta ON ta.tag_id = t.id
                WHERE t.suno_safe = 1 AND (t.normalized_name = ? OR ta.normalized_alias = ?)
                """,
                (key, key),
            )
            for row in rows:
                safe_tags[int(row["id"])] = dict(row)
        selected = sorted(safe_tags.values(), key=lambda tag: (str(tag["namespace"]), str(tag["canonical_name"])))[:max_tags]
        descriptors = [str(tag["canonical_name"]) for tag in selected]
        return {
            "style_prompt": ", ".join(descriptors),
            "tags": [
                {"namespace": tag["namespace"], "name": tag["canonical_name"], "path": tag["path"]}
                for tag in selected
            ],
            "excluded": [
                "artist names",
                "song titles",
                "lyrics",
                "recoverable melodies",
                "unapproved candidate tags",
            ],
        }

    # -- validation -------------------------------------------------------

    def validate(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
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
                SELECT r.id FROM recording r
                WHERE r.canonical_analysis_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM search_fts f WHERE f.recording_id = r.id)
                """,
            ),
        ]
        for code, sql in checks:
            for row in self.connection.execute(sql):
                issues.append({"code": code, "recording_id": str(row[0])})
        return {"valid": not issues, "issues": issues, "issue_count": len(issues)}


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
