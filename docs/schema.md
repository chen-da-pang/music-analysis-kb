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
- Keep client databases read-only and use one publisher writer. SQLite WAL is
  used on the master; release files are copied with the SQLite backup API.
- Benchmark with a 100k-recording synthetic data set before adding
  `sqlite-vec`, splitting a database, or introducing a server database.
- The deterministic `music_flamingo_parser_v1` source can backfill rich
  analysis tags without changing canonical raw text. It replaces only its own
  assignments, so manual editorial tags remain durable across parser reruns.
- Schema v4 accepts publisher databases created by every released prior schema
  (v1, v2, and v3). Existing numeric measurements are labelled `legacy` during
  migration; new generic imports default to `model` and parser BPM values carry
  their parser version, so ownership remains explicit.
