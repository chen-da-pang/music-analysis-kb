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
| `search_fts` | FTS5 projection of only the canonical public analysis. |

## Search order

1. Exact normalized tag or alias match in `tag`/`tag_alias`.
2. Title/artist alias matching.
3. FTS5 across title, artist aliases, tags, summary, and canonical analysis
   text.
4. Optional vector retrieval later, only for intentionally fuzzy queries.

The first three are deterministic. Vector search is not a substitute for rare
tag retrieval and is not part of this first release.

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
- Schema v7 accepts publisher databases created by every released prior schema
  (v1–v4). Upgrading a v4 publisher master with `music-kb init --db ...` is a
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
  identity-bound receipt.
