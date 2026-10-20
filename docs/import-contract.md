# Import contract

`music-kb import-analysis` accepts one JSON object or a JSONL file. Every
record is an approved Music Flamingo analysis for one recording. The importer
creates an immutable revision, runs the canonical switch inside a transaction,
and rebuilds the search projection.

For a large generic update, use a file named `.jsonl` or `.ndjson`: it is read
one **physical LF** line at a time and is never loaded as a 100k-record Python
list. JSON object and JSON-array inputs remain supported for small/manual
imports, but they intentionally use the normal whole-file JSON decoder. For
backward compatibility, a JSON array or pretty-printed JSON object saved with
a `.jsonl`/`.ndjson` suffix also uses that legacy whole-file decoder; only
one-object-per-physical-LF files take the streaming path.

```json
{
  "recording": {
    "id": "rec_optional_stable_id",
    "title": "夜航",
    "version_label": "studio",
    "audio_sha256": "optional-audio-sha256"
  },
  "artists": [
    {
      "name": "示例艺人",
      "aliases": ["Shi Li Yi Ren", "SLYR"],
      "role": "primary"
    }
  ],
  "title_aliases": ["夜航 (Studio Version)", "Ye Hang"],
  "analysis": {
    "raw_text": "Music Flamingo analysis text…",
    "summary": "A short searchable analysis summary.",
    "model_version": "music-flamingo-version",
    "prompt_version": "taxonomy-v1",
    "generated_token_count": 993,
    "quality_state": "passed"
  },
  "tags": [
    {
      "namespace": "production",
      "name": "granular vocal chop",
      "path": "production/vocal/granular-chop",
      "aliases": ["颗粒人声切片"],
      "confidence": 0.91,
      "status": "approved"
    }
  ],
  "numeric_features": [
    {"name": "bpm", "value": 102, "unit": "bpm"}
  ],
  "source_tracks": [
    {"source": "kugou", "source_track_id": "123", "source_title": "夜航",
     "source_url": "https://www.kugou.com/mixsong/agent_gateway/example.html"}
  ]
}
```

## Required fields

- `recording.title`
- at least one artist (`artists`, or the legacy top-level `artist`)
- `analysis.raw_text`
- `analysis.quality_state: "passed"` when the revision is made canonical

`source_url` is optional source metadata. When present it must be an absolute
`http` or `https` URL; the read-only search and get tools expose it as a
clickable listening link.

## Tag rules

- `namespace` and `name` are mandatory. Tags are normalized and de-duplicated
  by namespace + normalized name, but all supplied aliases are preserved.
- Unknown/rare tags may use `status: "candidate"`; they remain searchable but
  remain first-class retrieval terms.
- Historical records may contain `suno_safe`. The retrieval-only plugin accepts
  it for backward-compatible import, but ignores it in MCP/CLI retrieval
  output and exposes no prompt compiler.
- Numeric values belong in `numeric_features`, not fake text tags.
- The importer rejects explicit Feigua fields and Feigua-marked tag
  namespaces/paths/sources. This database contains Music Flamingo analysis
  only; keep weekly hotspot context in the Feigua workflow.

## Canonical rule

The importer never overwrites a revision. If a new passed import targets the
same recording, the old canonical revision becomes `superseded` and the new
revision becomes the sole canonical analysis. A duplicate output hash for the
same recording is idempotent.

## Generic 100k-scale import and recovery

Use the generic importer only for normal Music Flamingo records. Its default
`--batch-size 500` keeps importer memory bounded and commits one durable batch
at a time; valid values are 1–5000.

```bash
music-kb import-analysis \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/new-analyses.jsonl \
  --batch-size 500
```

The final FTS5 projection is rebuilt once after all generic batches rather
than once per song. If a later input row or final rebuild fails, earlier
committed batches remain durable and their revisions are safely idempotent on
retry. The database records `search_projection_state=dirty`; `music-kb
validate` and snapshot creation then fail closed until either the corrected
import finishes or the publisher explicitly restores the projection:

```bash
music-kb rebuild-search --db "$HOME/.music-kb/music-master.sqlite"
music-kb validate --db "$HOME/.music-kb/music-master.sqlite"
```

Do not create or sync a release while validation reports
`search_projection_dirty`. The JSON result from a large generic import is a
bounded count summary (`created_count`, `idempotent_count`, `batch_count`), not
a 100k-element list of individual rows. For compatibility with the original
small-import CLI shape it also includes `imports`, but only the first 1000
per-record results. Callers must check `imports_truncated`; when it is `true`,
the count fields are authoritative and the omitted result rows were still
committed normally.

## KuGou canonical campaign delivery

Use `music-kb import-campaign-delivery` for the signed-off CNB/Music Flamingo
campaign export. It is deliberately **not** a permissive variant of
`import-analysis`: it accepts UTF-8 **LF JSONL only** (one object per physical
line, no CRLF/blank lines, final LF required), validates the entire delivery
before database writes, then imports it as one transaction.

```bash
music-kb import-campaign-delivery \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/kugou-canonical-delivery.jsonl \
  --expected-count 927
```

`--expected-count` is optional so that a small verified subset can be imported
in development; use `927` when publishing the complete original campaign.
The command never expects or stores audio bytes—`relative_audio_path` is audit
metadata only.

Each JSONL object requires at least these fields:

```json
{
  "schema_version": 1,
  "campaign_id": "kugou-20260706",
  "id": "kugou-source-id",
  "manifest_index": 0,
  "title": "Example title",
  "artist": "Example artist",
  "relative_audio_path": "audio/0000-example.mp3",
  "source_sha256": "64 lowercase hex characters",
  "source_bytes": 1234567,
  "output_text": "Exact Music Flamingo output text",
  "output_text_sha256": "SHA-256 of UTF-8 output_text",
  "generated_token_count": 993,
  "max_new_tokens": 1400,
  "contract": "generation-contract identifier",
  "attempt_id": "CNB attempt identifier",
  "canonical_source": "canonical delivery source identifier",
  "provenance": {"optional": "additional immutable audit metadata"}
}
```

The adapter verifies the output hash, `generated_token_count <= max_new_tokens`,
safe relative paths, and unique `id`, `manifest_index`, and relative audio path
throughout the delivery. It rejects Feigua names, tags, or metadata anywhere in
the envelope. A repeated source SHA-256 is accepted only as a source alias: all
of its rows must have identical bytes and exact output. They are aggregated
into one stable recording/canonical analysis, with every source row retained as
a source track, alternate titles retained as title aliases, and alternate
artist credits retained as artist identities. A source SHA-256 becomes the
stable recording identity; the KuGou ID remains a source-track ID. The
resulting canonical analysis keeps immutable provenance for all delivery fields in
`campaign_delivery_provenance`, including the delivery schema/campaign IDs,
source title/artist, runner contract, attempt ID, and canonicalized producer
metadata. Non-core producer fields (for example model/runtime/prompt hashes,
generation controls, clip duration, and truncation status) are preserved inside
that immutable provenance JSON even when the producer did not wrap them in a
`provenance` object.

`manifest_index` is a per-campaign, per-attempt delivery coordinate: a later
campaign or a corrected later attempt may reuse the same index and
canonical-source label without colliding with earlier provenance, while two
contradictory rows that claim the same campaign/source/index/attempt are
rejected.

Real delivery JSONL, audio, lyrics, production SQLite files, and private paths
must remain outside Git.

## Deterministic campaign tag enrichment

`music-kb enrich-campaign-tags` derives a versioned retrieval layer from the
canonical Music Flamingo prose. It recognizes structured sections and explicit
musical terms for genre, tempo/meter, rhythm, instruments, production/mix,
harmony, vocal treatment, mood, and form; it also records an explicit BPM when
the analysis states exactly one plausible value. It does **not** rewrite the
raw analysis or make a second model/API call.

```bash
music-kb enrich-campaign-tags --db "$HOME/.music-kb/music-master.sqlite" --dry-run
music-kb enrich-campaign-tags --db "$HOME/.music-kb/music-master.sqlite" --batch-size 500
```

The command is publisher-only, idempotent, and works only on current canonical
campaign analyses. It replaces only its own parser-version assignments, keeps
manual/other-source tags intact, and rejects client snapshot write targets.
New campaign imports use the same parser automatically.

Backfill processes bounded transactions (`--batch-size`, 1–5000, default 500)
rather than materializing the corpus in memory, so a 100k-scale master can be
resumed safely after an interruption.

A campaign delivery rebuilds its FTS projection once, inside the same SQLite
transaction as the immutable delivery rows: an FTS failure rolls back the
delivery rather than publishing a partially searchable campaign. Backfill
batches are intentionally resumable; rerunning `enrich-campaign-tags` after an
interruption refreshes its parser assignments and performs the final full FTS
rebuild again.

All tag families remain searchable. This stage has no model-output-to-generation-prompt
conversion: parser-derived tags are stored as retrieval candidates, including
lyric/theme and structural labels. Song-title and artist identity tags are
stored separately by the importer and remain available to exact-tag, title,
and artist retrieval. The parser avoids treating an identity field such as
`Title: Rock` as a genre, or a quoted lyric word such as `drop` as production,
because those are false retrieval classifications rather than usable music
descriptors.

Each numeric feature records a `source`. Campaign BPM values use
`music_flamingo_parser_v1`; a parser rerun replaces only that source and never
overwrites `manual`, `model`, or migrated `legacy` measurements.

## Audience import contract

Use `music-kb import-audience` for audience evidence. It writes the four
schema-v9 tables (`audience_source`, `audience_profile`, `audience_observation`,
`audience_entry`) and never touches `analysis_revision` or the canonical public
analysis. There are two record kinds:

```bash
music-kb import-audience \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/audience-shanfeng.jsonl \
  --kind profile

music-kb import-audience \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/chart-evidence.jsonl \
  --kind chart_evidence
```

`--kind profile` (default) is the SOP research picture: one profile per
recording × time window with age/gender/city-tier/scene/... observations.
`--kind chart_evidence` is a chart snapshot: one profile per recording × snapshot
date with `generation_hint` / `scene` / `tribe` observations derived from the
charts a song appeared on. A record may also carry `"kind"`, but it must match
the command line; mixed kinds in one file are rejected per record.

### Record shape

One JSONL record = one profile plus nested `sources`, `observations`, and
optional `entries`:

```json
{
  "recording": {"title": "山风山风等等我", "primary_artist": "万海东"},
  "profile": {
    "target_platform": "douyin",
    "time_window": "full_lifecycle",
    "window_start": "2026-07-20",
    "window_end": "2026-10-19",
    "method_id": "version-matrix",
    "method_version": "v1.1",
    "status": "available",
    "confidence": "low",
    "is_proxy": 1,
    "researched_at": "2026-10-20",
    "basis_note": "public pages + KuGou ownercount weights",
    "coverage_note": "no platform audience report; age left blank until recomputed"
  },
  "sources": [
    {
      "source_platform": "kugou",
      "source_key": "kugou-search:song:<title>:2026-10-19",
      "source_type": "kugou_search",
      "fetched_at": "2026-10-19",
      "evidence_level": "1_platform_public",
      "url": "http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword=..."
    }
  ],
  "observations": [
    {
      "dimension": "age", "bucket": "31-40",
      "value_low": 0.3, "value_mid": 0.32, "value_high": 0.34,
      "evidence_level": "4_inferred", "source_id": "src_shanfeng_kugou_search"
    }
  ],
  "entries": [
    {"entry_type": "original", "performer": "万海东",
     "released_at": "2026-07-20", "platform": "douyin",
     "relation_to_original": "same_master", "weight_basis": "等权"}
  ]
}
```

`recording` accepts `recording_id`/`id`, or `title` + `primary_artist` (the
importer resolves normalized title + artist and refuses ambiguous matches). The
recording must already exist with a canonical analysis.

### Required fields and validation

- `profile.target_platform`, `profile.time_window`, `profile.method_id`,
  `profile.method_version`, `profile.status`, `profile.confidence`,
  `profile.researched_at` (ISO date).
- `target_platform` rule: `method_id='chart_signal_v1'` requires
  `unspecified`; every other method requires `douyin`.
- Chart evidence enforces `method_id='chart_signal_v1'`,
  `method_version='chart-hint-v1'`, `time_window='chart_snapshot'`,
  `target_platform='unspecified'`, `status='available'`.
- Each source requires `source_platform`, `source_key`, `source_type`,
  `fetched_at`, `evidence_level`. Chart sources additionally require
  `source_platform='kugou'`, `source_type='chart_snapshot'`,
  `evidence_level='1_platform_public'`, `source_key` of the form
  `chart:<rank_id>:<YYYY-MM-DD>`, and a publisher-local `local_path`.
- Each observation requires `dimension`, non-empty `bucket`, `evidence_level`.
  Chart observations require `dimension ∈ generation_hint|scene|tribe`,
  `evidence_level='3_proxy'`, `is_proxy=1`, and a positive `rank`.
- Non-null share values must satisfy `0 ≤ value_low ≤ value_mid ≤ value_high ≤ 1`
  (adjacent non-null values only). `status='unavailable'` may have no
  observations; gaps are written to `coverage_note` instead of fabricated rows.
- `window_start`/`window_end` are optional ISO dates; when both are present
  `window_end ≥ window_start`.
- Every observation/entry `source_id` must reference a source in the same record
  (by its `id` or `source_key`) or an existing `audience_source.id`.
- `entries[].entry_type` is required; unknown `performer` / `released_at` are
  stored as empty strings (never NULL) so
  `UNIQUE(recording_id, entry_type, performer, released_at)` keeps working.
- Enum values follow the schema comments; unknown `entry_type`,
  `relation_to_original`, dimension, source type, evidence level, status, or
  confidence rejects that record only.

### Idempotency and output

- Sources upsert by `(source_platform, source_key)`; a rerun updates metadata and
  never accumulates duplicate source rows.
- A repeated profile whose
  `(recording_id, target_platform, time_window, method_version, researched_at)`
  already exists updates it in place and replaces its observations. When the
  record contains an `entries` key, the recording's entries are replaced with
  that list (an empty list clears them); when the key is absent, existing entries
  are left untouched.
- A record with validation errors is rolled back individually; other lines stay
  committed. The command prints a bounded JSON summary and exits `1` when any
  line failed:

```json
{"profiles": 1, "observations": 8, "sources": 2, "entries": 2,
 "duplicates": 1, "errors": 0, "error_details": [],
 "kind": "profile", "batch_count": 1}
```

`errors` is the count of rejected lines; `error_details` lists
`{"line", "code", "message", "details"}` for each. `duplicates` counts source
natural-key updates plus observation/entry rows deduplicated inside one record.
`--batch-size` (1–5000, default 500) bounds the transaction; per-record failures
roll back to a savepoint inside the batch.

### Reading and data boundary

```bash
music-kb get-audience <recording_id> [--window full_lifecycle] [--method version-matrix]
```

The read returns `groups` split into `research` (调研画像) and `chart_evidence`
(榜单证据); each group has `profiles`, `observations`, `sources`, `entries`,
`tags`, and `coverage_gaps`. `entries`/`tags` are recording-scoped and therefore
also returned at the top level with `counts`. `--window` filters by
`time_window` and rejects unknown values; `--method` filters by `method_id`.
Chart evidence always carries a note that it does not represent audience share.

Raw chart JSON and research reports stay on the publisher machine: only derived
records, source references, and `payload_path`/`local_path` strings enter the
database, and `local_path` is cleared from client snapshots. Tests use the
synthetic fixtures in `plugins/music-kb/tests/fixtures/audience/`; never commit a
real chart snapshot or a real research report.
