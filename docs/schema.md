# Schema and retrieval design

The schema is intentionally relational rather than a single JSON blob. It
preserves arbitrary tag granularity while keeping exact rare-tag recall stable
at 100k-recording scale.

## Core tables

| Table | Purpose |
| --- | --- |
| `recording` | Recording identity, title/version metadata, audio hash, and one `canonical_analysis_id`. |
| `artist`, `artist_alias`, `recording_artist` | Canonical artist identities, aliases/pinyin/initials supplied by the importer, and roles. |
| `title_alias` | Title variants, subtitle forms, punctuation-normalized forms, and supplied transliterations. |
| `source_track` | Platform identity, source-facing title/artist, and optional public listening URL. |
| `recording_lyric` | One source-identity-bound lyric outcome per canonical recording: normal text/hash for `available`, or auditable evidence for `instrumental` / `platform_unavailable`. |
| `analysis_revision` | Immutable Music Flamingo output, model/prompt provenance, quality state, and output hash. |
| `campaign_delivery_provenance` | Immutable KuGou canonical-delivery evidence: delivery schema/campaign IDs, source title/artist, source/output hashes, bytes, manifest index, contract, attempt, canonical source, plus canonicalized model/runner/prompt/generation metadata. |
| `tag_namespace`, `tag`, `tag_alias` | Namespaced, hierarchical tags and aliases without an arbitrary quantity cap. |
| `analysis_tag`, `recording_tag` | Analysis-derived tags plus first-class title/artist identity tags. |
| `numeric_feature` | BPM, duration, energy, and other numeric values, including a source label so parser backfills cannot overwrite manual/editorial measurements. |
| `embedding_model` | Versioned model/recipe/modality identity, dimension, cosine metric, weight license and JSON metadata (schema v8). |
| `recording_embedding` | Audio-SHA-bound little-endian float32 BLOB and norm, keyed by recording/model/factor; cascade on recording deletion, restrict on model deletion. |
| `audience_source` | Public audience evidence source: platform, natural `source_key`, type, fetch time, evidence level, coverage note, publisher-local path; `UNIQUE(source_platform, source_key)` and upsert-on-reimport (schema v9). |
| `audience_profile` | One recording × `target_platform` × `time_window` × `method_version` audience synthesis: window bounds, method, status, confidence, proxy flag, research date, coverage/payload notes (schema v9). |
| `audience_observation` | Dimension/bucket evidence under a profile: optional 0–1 share interval, chart rank, sample size, evidence level, source reference; `ON DELETE CASCADE` from the profile (schema v9). |
| `audience_entry` | Version-entry evidence for a recording: entry type, performer, release date, platform, relation to original, weight basis; recording-scoped `UNIQUE(recording_id, entry_type, performer, released_at)` (schema v9). |
| `search_fts` | FTS5 projection of only the canonical public analysis. |

## Search order

1. Exact normalized tag or alias match in `tag`/`tag_alias`.
2. Title/artist alias matching.
3. FTS5 across title, artist aliases, tags, summary, and canonical analysis
   text.
4. Independent audio nearest-neighbor retrieval via `similar`, not mixed into the first three paths.

The first three are deterministic. Vector search is not a substitute for rare
tag retrieval. Schema v8 stores independent overall, rhythm and timbre channels;
cosine distance is ordered ascending with recording ID as the tie breaker.
`meta.default_embedding_model` selects the overall model. No melody vectors or
fused scores are stored. See ADR-0003 for migration and license boundaries.

SQLite snapshots include both embedding tables. Canonical-only pruning removes
excluded recordings' vectors via foreign-key cascade; snapshot verification runs
the embedding integrity checks in `validate`. Manifest counts include embedding
models and distinct recordings with embeddings. Clients query installed v9
snapshots read-only through CLI `similar` or MCP `music_kb_similar`; MCP status
includes embedding counts and coverage. Old v8 snapshots fail the strict schema
version check rather than being migrated by a query.

## Audience records (schema v9)

Audience evidence is deliberately separate from `analysis_revision`: importing,
replacing, or trimming audience records never changes the public canonical
analysis. Four additive tables hold one research profile or chart-snapshot
profile per canonical recording:

- `audience_source` is the evidence layer. `source_key` is the natural key
  (`<url>`, `<path>#section`, a research artifact key, or
  `chart:<rank_id>:<YYYY-MM-DD>` for a chart snapshot) and is unique per
  `source_platform`; re-importing the same artifact upserts the row instead of
  accumulating duplicates. `local_path` points at a publisher-local artifact and
  is cleared from client snapshots.
- `audience_profile` declares `target_platform` and `time_window`. The v1 rule
  is fixed: research (`method_id='version-matrix'`) is `target_platform=douyin`,
  chart evidence (`method_id='chart_signal_v1'`) is `target_platform=unspecified`
  with `time_window=chart_snapshot`. The idempotent key is
  `(recording_id, target_platform, time_window, method_version, researched_at)`.
- `audience_observation` stores dimensions and buckets, never a standalone
  audience percentage. Shares, when present, are 0–1 intervals with
  `value_low ≤ value_mid ≤ value_high`; chart observations instead carry a
  `rank`, `evidence_level='3_proxy'`, and `is_proxy=1`. Gaps are recorded in
  `coverage_note`, not fabricated as empty observations.
- `audience_entry` models versions as first-class evidence. Unknown performers
  and release dates are stored as empty strings (never NULL) so the recording
  scoped unique key keeps working.

`validate` checks audience foreign keys, enums, the target-platform rule, bucket
non-emptiness, share bounds/order, and entry identity; it deliberately has **no**
`require_audience` publication gate because v1 coverage is partial. `doctor` /
MCP status counts include `audience_sources`, `audience_profiles`,
`audience_observations`, and `audience_entries`.

A client snapshot keeps only the newest profile per
`(recording_id, target_platform, time_window, method_id)` ordered by
`researched_at DESC, method_version DESC, id DESC`; the master keeps the full
history. Entries for all canonical recordings are carried. Sources referenced by
neither an observation nor an entry are pruned, and `local_path` is cleared.
Snapshot `manifest.json` counts therefore include the four `audience_*` values.

## Audio import and client compatibility

`import-embeddings` transactionally imports model metadata and SHA-bound JSONL
vectors; it performs no inference. `overall` uses MuQ-MuLan v3 (512d), while
`rhythm` and `timbre` use independent MERIT/MERT heads (128d each). Results
fold normalized titles, expose `more_versions`, and display distance to three
decimals, never percentages or threshold judgments. Only recordings with
vectors participate; status counts `recordings_with_embeddings / recordings`
describe coverage (initially 434/2005), not quality.

Migrate/bootstrap the master, create/verify and install a v9 snapshot **before**
upgrading clients: new clients cannot open v8 snapshots. The external
[audio runner and Suno evaluation scripts](../runners/audio-embed/README.md)
keep inference outside plugin dependencies. MuQ-MuLan/MERT weights are
CC-BY-NC-4.0 and MERIT heads MIT; internal research only until authorization
or replacement for commercial use.

## Scale notes

- Keep direct lookup indexes on normalized tag names/aliases, title aliases,
  artist aliases, canonical pointers, and tag assignments.
- Canonical promotion uses a `(recording_id, status)` revision index. This
  avoids scanning every canonical revision as a generic importer grows from
  10k toward 100k records.
- `meta.search_projection_state` is `current` only after the full public FTS5
  projection is complete. Interrupted resumable generic/backfill batches mark
  it `dirty`; validation blocks snapshot publishing until `rebuild-search` or
  the completing importer restores it.
- Projection validation compares the canonical recording IDs with the FTS IDs
  as two sets, rather than performing a correlated scan of FTS5's deliberately
  unindexed `recording_id` column.
- Keep client databases read-only and use one publisher writer. SQLite WAL is
  used on the master; release files are copied with the SQLite backup API.
- Benchmark with a 100k-recording synthetic data set before adding
  `sqlite-vec`, splitting a database, or introducing a server database.
- Run `uv run python scripts/benchmark_100k.py` from `plugins/music-kb` for a
  reproducible physical-LF JSONL import/read benchmark. It generates only
  synthetic data in a temporary directory by default.
- The deterministic `music_flamingo_parser_v1` source can backfill rich
  analysis tags without changing canonical raw text. It replaces only its own
  assignments, so manual editorial tags remain durable across parser reruns.
- Schema v9 accepts publisher databases created by prior schemas (v1–v8).
  Upgrading a v4 publisher master with `music-kb init --db ...` is a
  required one-time gate before the new 100k paths are used: it installs the
  canonical-switch and exact-tag indexes and records the FTS projection state.
  Existing numeric measurements are labelled `legacy` during migration; new
  generic imports default to `model` and parser BPM values carry their parser
  version, so ownership remains explicit. The v5-to-v6 migration adds the
  nullable `source_track.source_url` field; it does not depend on local audio
  files. The v6-to-v7 migration creates an empty `recording_lyric` table; it
  deliberately does not fabricate historical lyrics. A snapshot requires every
  canonical recording to have exactly one terminal lyric outcome, so pending or
  missing rows block publication until the CC/Kugou backfill imports an
  identity-bound receipt. The v8-to-v9 migration only adds the four audience
  tables, their indexes, and their foreign keys; it never rewrites existing
  analyses, tags, lyrics, or embeddings and is idempotent when run again
  (`CREATE TABLE IF NOT EXISTS` plus the unconditional schema replay in `init`).
