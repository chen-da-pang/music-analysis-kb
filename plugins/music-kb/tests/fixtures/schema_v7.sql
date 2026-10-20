
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
    source_url TEXT,
    UNIQUE(source_name, source_track_id)
);
CREATE INDEX IF NOT EXISTS idx_source_track_source_track_id ON source_track(source_track_id);

CREATE TABLE IF NOT EXISTS recording_lyric (
    recording_id TEXT PRIMARY KEY REFERENCES recording(id) ON DELETE CASCADE,
    source_track_row_id TEXT NOT NULL REFERENCES source_track(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('pending', 'available', 'instrumental', 'platform_unavailable')),
    lyric_text TEXT,
    text_sha256 TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    normalizer_version TEXT NOT NULL DEFAULT 'lrc-v1',
    acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        (status = 'available'
         AND lyric_text IS NOT NULL
         AND length(trim(lyric_text)) > 0
         AND text_sha256 IS NOT NULL
         AND length(trim(text_sha256)) = 64)
        OR (status = 'pending' AND lyric_text IS NULL AND text_sha256 IS NULL)
        OR (status IN ('instrumental', 'platform_unavailable')
            AND lyric_text IS NULL
            AND text_sha256 IS NULL
            AND length(trim(evidence_json)) > 2)
    )
);
CREATE INDEX IF NOT EXISTS idx_recording_lyric_source_track
    ON recording_lyric(source_track_row_id);
CREATE INDEX IF NOT EXISTS idx_recording_lyric_status
    ON recording_lyric(status);

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

INSERT INTO meta VALUES ('schema_version', '7');
INSERT INTO meta VALUES ('database_kind', 'master');
INSERT INTO meta VALUES ('search_projection_state', 'current');
INSERT INTO artist VALUES ('artist_981f685c2a36b0b9b081b6a3', '示例乐队', '示例乐队', '2026-10-17 18:49:58');
INSERT INTO artist_alias VALUES ('artist_981f685c2a36b0b9b081b6a3', '示例乐队', '示例乐队');
INSERT INTO artist_alias VALUES ('artist_981f685c2a36b0b9b081b6a3', 'Shi Li Yue Dui', 'shi li yue dui');
INSERT INTO artist_alias VALUES ('artist_981f685c2a36b0b9b081b6a3', 'SLYD', 'slyd');
INSERT INTO recording VALUES ('rec_neon_night_studio', '霓虹夜航', '霓虹夜航', 'studio', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'anl_46335b646648443f4e383acb', '2026-10-17 18:49:58', '2026-10-17 18:49:58');
INSERT INTO title_alias VALUES ('rec_neon_night_studio', '霓虹夜航', '霓虹夜航');
INSERT INTO title_alias VALUES ('rec_neon_night_studio', 'Neon Night Flight', 'neon night flight');
INSERT INTO title_alias VALUES ('rec_neon_night_studio', 'Ni Hong Ye Hang', 'ni hong ye hang');
INSERT INTO recording_artist VALUES ('rec_neon_night_studio', 'artist_981f685c2a36b0b9b081b6a3', 'primary', 0);
INSERT INTO source_track VALUES ('src_60c688ab8bbf52493b02642f', 'rec_neon_night_studio', 'fixture', 'fixture-001', '霓虹夜航', '示例乐队', 'https://music.example/listen/fixture-001');
INSERT INTO recording_lyric VALUES ('rec_neon_night_studio', 'src_60c688ab8bbf52493b02642f', 'available', 'Synthetic fixture lyric for rec_neon_night_studio.', 'bc5db5a801ebf21e69013b946145761ac4ceb6089528b08ab291f9a1787b142b', '{"query_method":"fixture_seed","reason":"synthetic test fixture lyric","source_name":"fixture","source_track_id":"fixture-001"}', 'lrc-v1', '2026-10-17 18:49:58', '2026-10-17 18:49:58');
INSERT INTO analysis_revision VALUES ('anl_46335b646648443f4e383acb', 'rec_neon_night_studio', '102 BPM electronic pop with a granular vocal chop, syncopated rimshot, wide sidechained pads, and a restrained nocturnal build.', 'Nocturnal electronic pop with granular vocal treatment and syncopated percussion.', 'music-flamingo-fixture-v1', 'taxonomy-v1', 993, 'passed', 'canonical', 'dc339bec4f5974d15beadc86d1f01c85581be2a6d1282e7d7b158fb4c636d62d', '2026-10-17 18:49:58');
INSERT INTO tag_namespace VALUES ('title', '');
INSERT INTO tag_namespace VALUES ('artist', '');
INSERT INTO tag_namespace VALUES ('genre', '');
INSERT INTO tag_namespace VALUES ('production', '');
INSERT INTO tag_namespace VALUES ('drum', '');
INSERT INTO tag VALUES (1, 'title', '霓虹夜航', '霓虹夜航', '', 'approved', 0);
INSERT INTO tag VALUES (2, 'artist', '示例乐队', '示例乐队', '', 'approved', 0);
INSERT INTO tag VALUES (3, 'genre', 'electronic pop', 'electronic pop', 'genre/electronic-pop', 'approved', 1);
INSERT INTO tag VALUES (4, 'production', 'granular vocal chop', 'granular vocal chop', 'production/vocal/granular-chop', 'approved', 1);
INSERT INTO tag VALUES (5, 'drum', 'syncopated rimshot', 'syncopated rimshot', 'drum/snare/syncopated-rimshot', 'candidate', 0);
INSERT INTO tag_alias VALUES (1, 'Neon Night Flight', 'neon night flight');
INSERT INTO tag_alias VALUES (1, 'Ni Hong Ye Hang', 'ni hong ye hang');
INSERT INTO tag_alias VALUES (2, 'Shi Li Yue Dui', 'shi li yue dui');
INSERT INTO tag_alias VALUES (2, 'SLYD', 'slyd');
INSERT INTO tag_alias VALUES (3, '电子流行', '电子流行');
INSERT INTO tag_alias VALUES (4, '颗粒人声切片', '颗粒人声切片');
INSERT INTO tag_alias VALUES (5, '切分边击', '切分边击');
INSERT INTO analysis_tag VALUES ('anl_46335b646648443f4e383acb', 3, 0.96, 'model');
INSERT INTO analysis_tag VALUES ('anl_46335b646648443f4e383acb', 4, 0.91, 'model');
INSERT INTO analysis_tag VALUES ('anl_46335b646648443f4e383acb', 5, 0.83, 'model');
INSERT INTO recording_tag VALUES ('rec_neon_night_studio', 1, 'title');
INSERT INTO recording_tag VALUES ('rec_neon_night_studio', 2, 'artist');
INSERT INTO numeric_feature VALUES ('anl_46335b646648443f4e383acb', 'bpm', 102.0, 'bpm', 0.97, 'model');
INSERT INTO search_fts VALUES ('rec_neon_night_studio', '霓虹夜航', '示例乐队', 'Neon Night Flight Ni Hong Ye Hang 霓虹夜航 SLYD Shi Li Yue Dui 示例乐队', 'electronic pop granular vocal chop syncopated rimshot 示例乐队 霓虹夜航 Neon Night Flight Ni Hong Ye Hang SLYD Shi Li Yue Dui 切分边击 电子流行 颗粒人声切片', 'Nocturnal electronic pop with granular vocal treatment and syncopated percussion.
102 BPM electronic pop with a granular vocal chop, syncopated rimshot, wide sidechained pads, and a restrained nocturnal build.');
