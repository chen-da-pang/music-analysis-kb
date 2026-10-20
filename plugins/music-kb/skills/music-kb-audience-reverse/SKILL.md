---
name: music-kb-audience-reverse
description: Reconstruct one song's Douyin audience direction from public evidence with SOP v1.1, consume the chart evidence already stored by `get-audience`, and emit an audience-v2.1 JSONL profile that `music-kb import-audience` accepts. Use for 受众调研 / 受众画像 / 受众反推 / audience research on a named song, or when existing chart evidence must become a defensible direction plus cited gaps. Publisher machine only.
---

# Music KB Audience Reverse

Give one song a defensible audience **direction** — who is plausibly in the audience, which version entries carry it, and what cannot be supported — plus one importable audience-v2.1 JSONL profile. The result is evidence: never a platform report, never a single "audience score"（受众分）.

Method base: `docs/audience-method.md` (SOP v1.1 detail, evidence levels, confidence and bucket mapping, backtest). Import contract: `docs/import-contract.md` § "Audience import contract" (exact fields, enums, error codes). Reach for the method base when a step question is not settled here, and re-read the contract at Step 6 only when a field is in doubt.

## Invocation（触发条件与显式命令）

- **Explicit invocation** — name the skill: `用 music-kb-audience-reverse 调研《<song>》/ <artist>`, or `run the music-kb-audience-reverse skill for <song> / <artist>`. Research is agent work; no CLI subcommand performs it.
- **Automatic invocation** — the frontmatter description carries the trigger branches (受众调研 / 受众画像 / 受众反推 / audience research / chart evidence → direction). Auto-triggering is verified by human spot-check (#143), not a machine gate.
- **Publisher machine only** — needs the repo checkout, a writable master DB and public web access. On a read-only client machine, stop and point at `music-kb --json get-audience`.

## Inputs and ground rules

Input: song title + artist (or an existing recording id); optional chart evidence — read it from `music-kb --json get-audience <recording_id>` or take a caller-provided evidence file. Everything else is public web pages and public endpoints.

The output is judged against these rules:

- **Every number carries a `source_id`.** A number without a real retrieved source becomes a gap in `coverage_note`; it is never invented.
- **Quotes are retrieved text.** Search snippets and model-generated quotes are not citable.
- **A chart hit is 上榜, not 受众占比.** It supports "this song entered that pool"; it never sizes a share and is never averaged across charts.
- **Public sources only.** No login-state sources, no 飞瓜 CLI, no chart capture in this skill (the weekly chart producer owns capture).
- **Stay inside the task inputs.** Do not read local history, other sessions, or unrelated files; do not write canonical analysis or tags.
- **Research artifacts stay outside git.** Write raw notes and the JSONL to an operational path such as `data/audience-research/<YYYY-MM-DD>/<recording_id>.jsonl`; the DB stores derived records, source references and paths only.

## Step 0 — Resolve the recording

Completion: one `recording_id` whose canonical analysis exists, or a reported boundary.

1. Run `music-kb --json search --title '<title>' --artist '<artist>'`. If several versions match, list the candidates and ask which one; never pick silently.
2. Read the existing evidence for that recording: `music-kb --json get-audience <recording_id>`. A miss is data: record `榜单证据缺失` for later and continue to web research.
3. If no recording has a canonical analysis, report the boundary and stop; `import-audience` rejects the profile anyway.

## Step 1 — Consume chart evidence first

Completion: a cited evidence list (per hit: 榜名, 名次, snapshot date) or a recorded gap.

Read the `chart_evidence` group from Step 0 (or a supplied evidence file). Keep for each hit: `source_platform='kugou'`, `source_key='chart:<rank_id>:<YYYY-MM-DD>'`, `title`（榜名）, `fetched_at`, `bucket`（`gen/00后`、`scene/shortvideo`、`tribe/...`）, `rank`, `local_path`.

Use chart evidence for exactly two things:

1. **Version/hypothesis 旁证 (③).** "Entered the 90后 chart" raises an age-direction hypothesis for the matching version; it never becomes a share.
2. **A cited `source` in the output.** Copy the consumed row into the profile's `sources[]` with the same `source_key` / `source_platform` / `fetched_at` / `title` and `source_type='chart_snapshot'`, `evidence_level='1_platform_public'`, `coverage_note='榜内热度≠人口构成'`, plus `local_path` when present. The observation that leans on it stays `evidence_level='4_inferred'`; a recorded rank stays in `rank`, never in `value_*`.

In the final report write "上过 X 榜（第 n，快照日期）", never "X 类用户占 Y%".

## Step 2 — L0 anchor

Completion: identity settled, version entries listed, both time windows declared.

- **Version matrix（版本矩阵）**: original, duet/chorus, cover (male/female), DJ/remix, children, clip, soundtrack, variety, KTV, dialect, instrumental. For each: performer, release date, platform, relation to original, weight basis. These become `entries[]`; unknown performer/release date are written as empty strings.
- Exclude same-title-different-song and mis-identified covers.
- **Windows**: `first_wave` (roughly 2–3 weeks after release) and `full_lifecycle`; answer them separately. A window that cannot be computed is written as `status='unavailable'` plus a `coverage_note`, never patched with numbers.

## Step 3 — L1 evidence layers（证据等级）

Completion: every conclusion carries a ①/②/③/④ label and a real `source_id`.

| Layer | What to gather | What it can support |
|---|---|---|
| 1 发行排期 | Version order, label schedule, KTV/伴奏 sync, platform premieres, DJ re-releases | Which audience each version was aimed at |
| 2 关联艺人受众池 | Public audience profile of every collaborator/cover artist | Population prior migrated to that version |
| 3 平台结构 | Presence on 酷狗/酷我/汽水/QQ/网易云/抖音/快手/视频号 | Platform-user priors (city tier / age direction) |
| 4 物料形态 | KTV, car/8D, DJ, dance, gesture-dance, parent-child, spoken-word, film/TV clips, dialect, challenge | Scene evidence only, never audience size |
| 5 需求侧信号 | Charts, search index, favorites, shares, usage, comments | "Someone is using/listening"; direction checks |
| 6 大盘基准 | Same-topic public reports and platform disclosures | Direction-only comparison (older/younger, higher/lower tier) |

Write each retained source into `sources[]`: `source_platform` ∈ `kugou|qq|netease|bilibili|qishui|douyin|web`; `source_key` = the URL, `<path>#section`, or a chart key; `source_type` from the import-contract enum; `fetched_at`; `evidence_level` = the label of what the page actually is (1 platform-public, 2 media, 3 comment/chart proxy). A chart snapshot page itself is `1_platform_public`; any inference drawn from it is `4_inferred`.

## Step 4 — L2 per-version weighting

Completion: per-version table + weighted total + stated error direction.

- Give every version its own hypothesis first, then aggregate. Weight priority, strongest first: version-level usage/play/favorite/chart data → version count × release order → equal weight. Record what was used, what is missing, and which way the error leans.
- Keep only directional buckets in `observations`; every share is a 0–1 interval (`value_low ≤ value_mid ≤ value_high`), all `4_inferred`, all with a source.
- Name versions whose audience contradicts the total instead of letting the average hide them.

## Step 5 — L3 anti-bias check（八条反偏见检查）

Completion: eight answers (是/否/无法判断), each naming the evidence that settles it; every "是" is fixed before output.

1. Is on-camera/creator gender being read as listener gender?
2. Is province inferred from one performance, one account, or the singer's birthplace?
3. Is content form mapped directly to age (广场舞→中老年, 童声→宝妈, DJ→司机)?
4. Is the follow-on viral wave being read as the first wave?
5. Are platform measures mixed (网易云/QQ ≠ 抖音; singer fans ≠ song listeners)?
6. Is a cover/renamed version's audience counted into the original?
7. Is any quote/comment not actually retrieved (search summary or model-generated)?
8. Is any number missing a source?

Write **上榜 ≠ 受众占比** into the check explicitly: chart rows raise hypotheses, they never size the audience.

## Step 6 — Emit the audience-v2.1 JSONL（输出契约）

Completion: one or two records (`first_wave`, optional `full_lifecycle`) that pass `import-audience --kind profile` with `errors=0`.

Record shape (one JSON per line; full contract in `docs/import-contract.md`):

```json
{"kind":"profile","recording":{"id":"<recording_id>"},
 "profile":{"target_platform":"douyin","time_window":"full_lifecycle",
   "window_start":"<ISO date>","window_end":"<ISO date>",
   "method_id":"version-matrix","method_version":"v1.1","status":"available",
   "confidence":"low","is_proxy":1,"researched_at":"<ISO date>",
   "basis_note":"<materials + weights>","coverage_note":"<gaps + error direction>"},
 "sources":[{"id":"src_<key>","source_platform":"web","source_key":"<url>",
   "source_type":"media","fetched_at":"<ISO date>","evidence_level":"2_media"}],
 "observations":[{"dimension":"age","bucket":"31-40","value_low":0.30,
   "value_mid":0.32,"value_high":0.34,"evidence_level":"4_inferred",
   "is_proxy":1,"source_id":"src_<key>"}],
 "entries":[{"entry_type":"original","performer":"<name>","released_at":"<ISO date>",
   "platform":"douyin","relation_to_original":"same_master","weight_basis":"等权"}]}
```

Rules:

- `profile.target_platform` is `douyin` for research profiles; `method_id='version-matrix'`, `method_version='v1.1'`, `is_proxy=1`. A window with no computable data is `status='unavailable'` with the reason in `coverage_note`.
- `observations`: `dimension` ∈ `age|gender|province|city_tier|life_stage|scene|creator|emotion`; `bucket` non-empty. Shares only as ordered 0–1 intervals; when a number is not defensible, record a gap instead of inventing one. Chart-cited rows stay `4_inferred`.
- `entries`: `entry_type` required; unknown `performer` / `released_at` written as `""`.
- **Bucket mapping（标签桶映射，spec §7）**: v1 bucket vocabulary: age `18-23|24-30|31-40|41-50|50+`; gender `male|female`; city-tier buckets `一线/新一线`, `二线`, `三线及以下+县域乡镇` (projection maps them to `high|mid|low`); chart hints keep the `get-audience` buckets (`gen/...`, `scene/...`, `tribe/...`). v1 has no KTV or popularity bucket.
- **Confidence mapping（置信度映射，spec §5）**: chart evidence 1 chart = `low`, 2–3 = `medium`, ≥4 or (代际 2 榜 + one scene/tribe) = `high`. A research profile stays `low` by default; it may be `medium` only when every reported dimension has a cited source and both windows were processed, and `high` only with a ① platform export, which public research normally does not have.

Write the file outside git.

## Step 7 — Import, read back, report

Completion: an import summary with `errors=0` (or every rejected line reported with its `error_details` entry), plus a read-back confirming the research and chart-evidence groups.

```bash
music-kb --json import-audience --db "$HOME/.music-kb/music-master.sqlite" \
  --input data/audience-research/<date>/<recording_id>.jsonl --kind profile
music-kb --json get-audience <recording_id> --db "$HOME/.music-kb/music-master.sqlite"
```

- Import into the publisher master only; a staging copy is fine for a dry run. Never import into a client snapshot.
- Rejected lines are fixed and re-imported; `error_details` is read, never dropped.
- Report: one-line direction; per-window table; chart evidence actually used (榜名 + 名次 + snapshot date); gaps and "what this cannot support"; import summary. No single audience score; no platform-report wording.
- Tag projection and search belong to #140; write no tags by hand.

## Reference

- `docs/audience-method.md` — method base: SOP v1.1 detail, evidence levels, confidence rules, bucket mapping, backtest.
- `docs/import-contract.md` — audience import contract, enums, error codes.
- `plugins/music-kb/tests/fixtures/audience/` — synthetic 山风 profile and chart evidence for offline dry runs; never commit a real chart snapshot or report.
- Spec v2.1 — https://github.com/chen-da-pang/music-analysis-kb/issues/137#issuecomment-6286448586
