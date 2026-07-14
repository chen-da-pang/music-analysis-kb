from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import DatabaseNotInitializedError, MusicKBError, ReadOnlyError


SCHEMA_VERSION = 5


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artist (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artist_alias (
    artist_id TEXT NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    PRIMARY KEY (artist_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_artist_alias_normalized ON artist_alias(normalized_alias);

CREATE TABLE IF NOT EXISTS recording (
    id TEXT PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    version_label TEXT NOT NULL DEFAULT '',
    audio_sha256 TEXT UNIQUE,
    canonical_analysis_id TEXT REFERENCES analysis_revision(id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_recording_normalized_title ON recording(normalized_title);
CREATE INDEX IF NOT EXISTS idx_recording_canonical_analysis ON recording(canonical_analysis_id);

CREATE TABLE IF NOT EXISTS title_alias (
    recording_id TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    PRIMARY KEY (recording_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_title_alias_normalized ON title_alias(normalized_alias);

CREATE TABLE IF NOT EXISTS recording_artist (
    recording_id TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL REFERENCES artist(id) ON DELETE RESTRICT,
    role TEXT NOT NULL DEFAULT 'primary',
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (recording_id, artist_id, role)
);
CREATE INDEX IF NOT EXISTS idx_recording_artist_artist ON recording_artist(artist_id);

CREATE TABLE IF NOT EXISTS source_track (
    id TEXT PRIMARY KEY,
    recording_id TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    source_track_id TEXT NOT NULL,
    source_title TEXT,
    source_artist_credit TEXT,
    UNIQUE(source_name, source_track_id)
);
CREATE INDEX IF NOT EXISTS idx_source_track_source_track_id ON source_track(source_track_id);

CREATE TABLE IF NOT EXISTS analysis_revision (
    id TEXT PRIMARY KEY,
    recording_id TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
    raw_text TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    generated_token_count INTEGER,
    quality_state TEXT NOT NULL CHECK(quality_state IN ('passed', 'needs_review', 'failed')),
    status TEXT NOT NULL CHECK(status IN ('candidate', 'canonical', 'superseded', 'rejected')),
    output_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recording_id, output_sha256)
);
CREATE INDEX IF NOT EXISTS idx_analysis_revision_recording ON analysis_revision(recording_id);
CREATE INDEX IF NOT EXISTS idx_analysis_revision_status ON analysis_revision(status);
CREATE INDEX IF NOT EXISTS idx_analysis_revision_recording_status
    ON analysis_revision(recording_id, status);

-- Immutable, campaign-specific evidence for canonical KuGou deliveries.
-- This stays normalized rather than being folded into the model text so a
-- publisher can audit the exact source/output/runner contract that produced a
-- public analysis without exposing a second mutable analysis representation.
CREATE TABLE IF NOT EXISTS campaign_delivery_provenance (
    id TEXT PRIMARY KEY,
    delivery_schema_version INTEGER NOT NULL CHECK(delivery_schema_version = 1),
    campaign_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    analysis_id TEXT NOT NULL REFERENCES analysis_revision(id) ON DELETE CASCADE,
    manifest_index INTEGER NOT NULL CHECK(manifest_index >= 0),
    source_title TEXT NOT NULL,
    source_artist TEXT NOT NULL,
    relative_audio_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_bytes INTEGER NOT NULL CHECK(source_bytes > 0),
    output_text_sha256 TEXT NOT NULL,
    generated_token_count INTEGER NOT NULL CHECK(generated_token_count >= 0),
    max_new_tokens INTEGER NOT NULL CHECK(max_new_tokens > 0),
    contract TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    canonical_source TEXT NOT NULL,
    provenance_json TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(generated_token_count <= max_new_tokens),
    -- The deterministic provenance ID is the immutable identity. Manifest
    -- coordinates are scoped to the producing attempt: a later campaign or
    -- corrected attempt may reuse an index, while two contradictory rows from
    -- the same declared attempt cannot coexist.
    UNIQUE(campaign_id, canonical_source, manifest_index, attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_campaign_delivery_analysis
    ON campaign_delivery_provenance(analysis_id);
CREATE INDEX IF NOT EXISTS idx_campaign_delivery_delivery_id
    ON campaign_delivery_provenance(delivery_id);
CREATE INDEX IF NOT EXISTS idx_campaign_delivery_source_index
    ON campaign_delivery_provenance(campaign_id, canonical_source, manifest_index, attempt_id);

CREATE TABLE IF NOT EXISTS tag_namespace (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tag (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL REFERENCES tag_namespace(name) ON DELETE RESTRICT,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    lifecycle_status TEXT NOT NULL DEFAULT 'candidate'
        CHECK(lifecycle_status IN ('candidate', 'approved', 'deprecated')),
    -- Retained solely to preserve historical import rows. The current
    -- retrieval-only plugin neither reads nor exposes this legacy flag.
    suno_safe INTEGER NOT NULL DEFAULT 0 CHECK(suno_safe IN (0, 1)),
    UNIQUE(namespace, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_tag_namespace_name ON tag(namespace, normalized_name);
CREATE INDEX IF NOT EXISTS idx_tag_normalized_name ON tag(normalized_name);

CREATE TABLE IF NOT EXISTS tag_alias (
    tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    PRIMARY KEY (tag_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_tag_alias_normalized ON tag_alias(normalized_alias);

CREATE TABLE IF NOT EXISTS recording_tag (
    recording_id TEXT NOT NULL REFERENCES recording(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    PRIMARY KEY (recording_id, tag_id, role)
);
CREATE INDEX IF NOT EXISTS idx_recording_tag_tag ON recording_tag(tag_id);

CREATE TABLE IF NOT EXISTS analysis_tag (
    analysis_id TEXT NOT NULL REFERENCES analysis_revision(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE RESTRICT,
    confidence REAL,
    source TEXT NOT NULL DEFAULT 'model',
    PRIMARY KEY (analysis_id, tag_id),
    CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX IF NOT EXISTS idx_analysis_tag_tag ON analysis_tag(tag_id);

CREATE TABLE IF NOT EXISTS numeric_feature (
    analysis_id TEXT NOT NULL REFERENCES analysis_revision(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    confidence REAL,
    source TEXT NOT NULL DEFAULT 'model',
    PRIMARY KEY (analysis_id, name),
    CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX IF NOT EXISTS idx_numeric_feature_name_value ON numeric_feature(name, value);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    recording_id UNINDEXED,
    title,
    artist,
    aliases,
    tags,
    analysis,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS canonical_analysis_same_recording
BEFORE UPDATE OF canonical_analysis_id ON recording
WHEN NEW.canonical_analysis_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM analysis_revision ar
    WHERE ar.id = NEW.canonical_analysis_id AND ar.recording_id = NEW.id
 )
BEGIN
    SELECT RAISE(ABORT, 'canonical analysis must belong to the recording');
END;

CREATE TRIGGER IF NOT EXISTS canonical_analysis_must_pass_quality
BEFORE UPDATE OF canonical_analysis_id ON recording
WHEN NEW.canonical_analysis_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM analysis_revision ar
    WHERE ar.id = NEW.canonical_analysis_id AND ar.quality_state = 'passed'
 )
BEGIN
    SELECT RAISE(ABORT, 'canonical analysis must have passed quality');
END;
"""


def _path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open an SQLite connection with safe defaults for this package."""

    database = _path(path)
    if read_only:
        if not database.is_file():
            raise DatabaseNotInitializedError(f"Database does not exist: {database}")
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
    else:
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if read_only:
        connection.execute("PRAGMA query_only = ON")
    return connection


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Rebuild the v2 provenance table without losing immutable evidence.

    Version 2 used a global ``(canonical_source, manifest_index)`` uniqueness
    boundary and did not retain source title/artist in provenance. SQLite
    cannot alter either constraint in place, so copy into the v3 table inside
    one transaction. Existing source-track metadata is the only authoritative
    title/artist source available to v2, and is preserved as the migration
    backfill.
    """

    legacy_table = "campaign_delivery_provenance_v2_legacy"
    with connection:
        connection.execute(
            f"ALTER TABLE campaign_delivery_provenance RENAME TO {legacy_table}"
        )
        for index in (
            "idx_campaign_delivery_analysis",
            "idx_campaign_delivery_delivery_id",
            "idx_campaign_delivery_source_index",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        connection.execute(
            """
            CREATE TABLE campaign_delivery_provenance (
                id TEXT PRIMARY KEY,
                delivery_schema_version INTEGER NOT NULL CHECK(delivery_schema_version = 1),
                campaign_id TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                analysis_id TEXT NOT NULL REFERENCES analysis_revision(id) ON DELETE CASCADE,
                manifest_index INTEGER NOT NULL CHECK(manifest_index >= 0),
                source_title TEXT NOT NULL,
                source_artist TEXT NOT NULL,
                relative_audio_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_bytes INTEGER NOT NULL CHECK(source_bytes > 0),
                output_text_sha256 TEXT NOT NULL,
                generated_token_count INTEGER NOT NULL CHECK(generated_token_count >= 0),
                max_new_tokens INTEGER NOT NULL CHECK(max_new_tokens > 0),
                contract TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                canonical_source TEXT NOT NULL,
                provenance_json TEXT,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(generated_token_count <= max_new_tokens),
                UNIQUE(campaign_id, canonical_source, manifest_index, attempt_id)
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO campaign_delivery_provenance(
                id, delivery_schema_version, campaign_id, delivery_id, analysis_id,
                manifest_index, source_title, source_artist, relative_audio_path,
                source_sha256, source_bytes, output_text_sha256,
                generated_token_count, max_new_tokens, contract, attempt_id,
                canonical_source, provenance_json, imported_at
            )
            SELECT c.id, c.delivery_schema_version, c.campaign_id, c.delivery_id, c.analysis_id,
                   c.manifest_index,
                   COALESCE(st.source_title, ''),
                   COALESCE(st.source_artist_credit, ''),
                   c.relative_audio_path,
                   c.source_sha256, c.source_bytes, c.output_text_sha256,
                   c.generated_token_count, c.max_new_tokens, c.contract, c.attempt_id,
                   c.canonical_source, c.provenance_json, c.imported_at
            FROM {legacy_table} c
            LEFT JOIN analysis_revision ar ON ar.id = c.analysis_id
            LEFT JOIN source_track st
              ON st.recording_id = ar.recording_id
             AND st.source_name = 'kugou'
             AND st.source_track_id = c.delivery_id
            """
        )
        connection.execute(f"DROP TABLE {legacy_table}")
        # Commit the version transition with the rebuilt table. If process
        # startup stops before the later idempotent SCHEMA_SQL pass, the next
        # `init` sees v3 and only creates any missing indexes/triggers instead
        # of attempting a second v2 table rename.
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("3",),
        )


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Add provenance ownership to numeric parser features.

    Analysis tags already carry a source label; numeric features need the same
    distinction so a deterministic BPM backfill can replace its own value
    without overwriting a manual/editorial measurement.
    """

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(numeric_feature)")
    }
    with connection:
        if "source" not in columns:
            connection.execute(
                "ALTER TABLE numeric_feature ADD COLUMN source TEXT NOT NULL DEFAULT 'model'"
            )
            # The column default applies to every pre-v4 row at ALTER time.
            # Mark those rows explicitly as legacy, while retaining the same
            # `model` default as a fresh v4 database for future generic imports.
            connection.execute("UPDATE numeric_feature SET source = 'legacy'")
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("4",),
        )


def _migrate_v1_to_v4(connection: sqlite3.Connection) -> None:
    """Upgrade the original plugin schema without falsely stamping v4.

    Schema v1 predates campaign provenance and has the same old
    ``numeric_feature`` shape (without ``source``). Current ``SCHEMA_SQL`` can
    create the missing provenance tables idempotently, but it cannot add a
    column to an existing numeric table. Perform that explicit alteration
    before recording v4.
    """

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(numeric_feature)")
    }
    with connection:
        if "source" not in columns:
            connection.execute(
                "ALTER TABLE numeric_feature ADD COLUMN source TEXT NOT NULL DEFAULT 'model'"
            )
            connection.execute("UPDATE numeric_feature SET source = 'legacy'")
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("4",),
        )


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    """Install scale-critical indexes for every already-published v4 master.

    Version 4 databases predate the generic 100k import path.  The new
    indexes are performance requirements rather than optional optimizations:
    without them a canonical switch can scan every canonical revision and a
    rare exact tag may scan the complete tag catalogue.  Recording this as a
    schema migration forces the publisher to run ``music-kb init`` before a
    v4 master is accepted by the v5 CLI/MCP process.
    """

    with connection:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tag_normalized_name ON tag(normalized_name)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_analysis_revision_recording_status
            ON analysis_revision(recording_id, status)
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('search_projection_state', 'current')"
        )
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("5",),
        )


def initialize_database(path: str | Path) -> Path:
    database = _path(path)
    version: int | None = None
    if database.is_file():
        existing = connect(database, read_only=True)
        try:
            try:
                version = _stored_schema_version(existing)
            except DatabaseNotInitializedError:
                version = None
            if version is not None and version > SCHEMA_VERSION:
                raise DatabaseNotInitializedError(
                    f"Unsupported schema version {version}; this music-kb build supports up to {SCHEMA_VERSION}."
                )
            if version is not None:
                kind = existing.execute("SELECT value FROM meta WHERE key = 'database_kind'").fetchone()
                if kind is not None and str(kind["value"]) == "snapshot":
                    raise ReadOnlyError("Client snapshots cannot be initialized or converted into publisher databases.")
        finally:
            existing.close()
    connection = connect(database)
    try:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            if version == 1:
                _migrate_v1_to_v4(connection)
                version = 4
            if version == 2:
                _migrate_v2_to_v3(connection)
                version = 3
            if version == 3:
                _migrate_v3_to_v4(connection)
                version = 4
            if version == 4:
                _migrate_v4_to_v5(connection)
                version = 5
            connection.executescript(SCHEMA_SQL)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).casefold():
                raise MusicKBError(
                    "SQLite on this machine was built without FTS5; music-kb requires FTS5."
                ) from exc
            raise
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('database_kind', 'master')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('search_projection_state', 'current')"
            )
    finally:
        connection.close()
    return database


def ensure_initialized(connection: sqlite3.Connection) -> None:
    version = _stored_schema_version(connection)
    if version != SCHEMA_VERSION:
        raise DatabaseNotInitializedError(
            f"Unsupported schema version {version}; expected {SCHEMA_VERSION}."
        )


def _stored_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError as exc:
        raise DatabaseNotInitializedError("Database is not a music-kb database; run music-kb init.") from exc
    if row is None:
        raise DatabaseNotInitializedError("Database is not initialized; run music-kb init.")
    try:
        return int(row["value"])
    except (TypeError, ValueError) as exc:
        raise DatabaseNotInitializedError("Database schema version is invalid; run music-kb init.") from exc
