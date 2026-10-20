# Audience fixtures

Synthetic, offline-only inputs for the schema-v9 audience tests and for the
#142 backtest. No file here is a production chart snapshot or a real report.

- `shanfeng.jsonl` — one `full_lifecycle` research profile (`target_platform=douyin`)
  for 山风山风等等我 / 万海东. The age/gender/city-tier values and the coverage
  note come from `research/audience-137-prototype/sample-shanfeng.jsonl`; the
  `cover` row in `entries` is synthetic and exists only to pin the documented
  "unknown performer/released_at are stored as empty strings" rule. It carries
  no real report path.
- `chart-evidence.jsonl` — two synthetic weekly `chart_snapshot` profiles for
  the same recording, demonstrating chart evidence exactly as the importer
  expects it: `method_id=chart_signal_v1`, `time_window=chart_snapshot`,
  `target_platform=unspecified`, `3_proxy` observations with ranks, and
  `1_platform_public` sources keyed `chart:<rank_id>:<YYYY-MM-DD>` with a
  publisher-local `local_path`. The two dates exercise latest-profile trimming.

Fixture paths are relative placeholders; the real chart JSON and research
reports stay on the publisher machine and are never committed.
