from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import DatabaseNotInitializedError, MusicKBError, ReadOnlyError


SCHEMA_VERSION = 1


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
    suno_safe INTEGER NOT NULL DEFAULT 0 CHECK(suno_safe IN (0, 1)),
    UNIQUE(namespace, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_tag_namespace_name ON tag(namespace, normalized_name);

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


def initialize_database(path: str | Path) -> Path:
    database = _path(path)
    if database.is_file():
        existing = connect(database, read_only=True)
        try:
            try:
                ensure_initialized(existing)
            except DatabaseNotInitializedError:
                pass
            else:
                kind = existing.execute("SELECT value FROM meta WHERE key = 'database_kind'").fetchone()
                if kind is not None and str(kind["value"]) == "snapshot":
                    raise ReadOnlyError("Client snapshots cannot be initialized or converted into publisher databases.")
        finally:
            existing.close()
    connection = connect(database)
    try:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
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
    finally:
        connection.close()
    return database


def ensure_initialized(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError as exc:
        raise DatabaseNotInitializedError("Database is not a music-kb database; run music-kb init.") from exc
    if row is None:
        raise DatabaseNotInitializedError("Database is not initialized; run music-kb init.")
    if int(row["value"]) != SCHEMA_VERSION:
        raise DatabaseNotInitializedError(
            f"Unsupported schema version {row['value']}; expected {SCHEMA_VERSION}."
        )
