# Import contract

`music-kb import-analysis` accepts one JSON object or a JSONL file. Every
record is an approved Music Flamingo analysis for one recording. The importer
creates an immutable revision, runs the canonical switch inside a transaction,
and rebuilds the search projection.

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
      "status": "approved",
      "suno_safe": true
    }
  ],
  "numeric_features": [
    {"name": "bpm", "value": 102, "unit": "bpm"}
  ],
  "source_tracks": [
    {"source": "kugou", "source_track_id": "123", "source_title": "夜航"}
  ]
}
```

## Required fields

- `recording.title`
- at least one artist (`artists`, or the legacy top-level `artist`)
- `analysis.raw_text`
- `analysis.quality_state: "passed"` when the revision is made canonical

## Tag rules

- `namespace` and `name` are mandatory. Tags are normalized and de-duplicated
  by namespace + normalized name, but all supplied aliases are preserved.
- `suno_safe: true` means that the tag is approved for the Suno compiler. It
  does **not** approve an artist name, title, lyric, melody, or imitation.
- Unknown/rare tags may use `status: "candidate"`; they remain searchable but
  are not automatically emitted in a Suno style prompt.
- Numeric values belong in `numeric_features`, not fake text tags.
- The importer rejects explicit Feigua fields and Feigua-marked tag
  namespaces/paths/sources. This database contains Music Flamingo analysis
  only; keep weekly hotspot context in the Feigua workflow.

## Canonical rule

The importer never overwrites a revision. If a new passed import targets the
same recording, the old canonical revision becomes `superseded` and the new
revision becomes the sole canonical analysis. A duplicate output hash for the
same recording is idempotent.
