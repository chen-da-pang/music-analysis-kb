---
name: music-kb
description: Retrieve real songs from the local, read-only Music Flamingo analysis KB. Use when a user asks naturally for discovery by mood, style, theme, or tag, or for title/artist resolution and canonical analysis; no internal tag or MCP syntax is required.
---

# Music Knowledge Base

This is a conversational, retrieval-only client. Reply in the user's language,
make the first turn useful, and never require canonical tag names, MCP syntax, or
CLI arguments from the user.

## Runtime routing — do this first

Read this Skill once, then use the route already available in the tool list. Do
not investigate the plugin installation before retrieving.

1. If `music_kb_status` appears in the provided tool list, call it directly.
   Named `music_kb_*` entries are MCP functions, not resources.
2. Otherwise immediately run the PATH command `music-kb --json doctor`, then use
   the known forms below, repeating `--tag` for every condition:

   ```bash
   music-kb --json discover --tag 'r&b' --tag 'warm' --tag 'love'
   music-kb --json recommend --tag 'r&b' --tag 'warm' --tag 'love'
   ```

   Omit `--limit` when the user gave no exact quantity; use `--offset` only for
   continuation. Run each branch recommendation as its own call, not one
   combined shell command.

For an ordinary successful retrieval:

- Do not call `list_mcp_resources`, `list_mcp_resource_templates`, or another
  discovery probe to find the named functions.
- Do not scan plugin directories or inspect source files, README text, `.venv`,
  executable locations, or implementation details.
- Do not bypass PATH with an absolute or `.venv/bin/music-kb` executable. If the
  PATH command is unavailable, report the install/reload boundary.
- Do not inspect `--help` unless a direct CLI call actually fails with an
  argument-usage error.
- Do not reread this Skill after the complete first read. A complete first read is sufficient; do not use sed, cat, or another file read to recover it.
- Do not repeat a successful discovery or recommendation with identical
  arguments, and do not `jq`-filter a successful branch to trigger a rerun.

## Safety and data boundary

- This is a local, read-only client of a published SQLite snapshot. Never write
  to `current.sqlite`, run arbitrary SQL, or alter canonical analysis.
- The KB contains Music Flamingo analysis and identity metadata only; do not mix
  Feigua tags into it.
- The workflow ends at retrieval. Never turn model output into a generation
  prompt or use a lifecycle flag to hide a searchable tag.

## Selected-song lyrics contract

- First-turn search, title/artist resolution, and tag facets stay compact: do
  not fetch or show full lyrics while presenting candidates.
- Only after the user has selected a recording, use
  `music_kb_get_lyrics(recording_id=...)` for “歌词”, “完整歌词”, or an equivalent
  request. Return the complete stored text when status is `available`; do not
  replace it with a summary, tag, translation, generated text, or `.lrc`
  timestamps.
- For “完整内容”, call both `music_kb_get_canonical_analysis(recording_id=...)`
  and `music_kb_get_lyrics(recording_id=...)`, then present the complete music
  analysis and the complete lyrics as separate sections for that exact
  recording.
- `instrumental` and `platform_unavailable` are honest terminal outcomes. Say
  which applies and do not invent lyrics. A `pending` response means the
  published snapshot is incomplete or older; report that boundary rather than
  guessing from a title or another version.
- Never use the lyric tool as a corpus-wide text search. It is a selected,
  recording-ID detail read only.

## Conversation UX contract

### Work first, then clarify when it matters

Extract concepts, title/artist, quantity, and explicit “and” / “or” logic. With
one reasonable interpretation, retrieve first and state it briefly. When
interpretations materially change results, retrieve useful bounded branches
first; ask before retrieval only when every branch would be arbitrary or no
searchable concept exists. Ask at most one result-changing question, never for
MCP arguments or canonical spelling.

### Broad subjective requests use real-result branches

For “一些有氛围感的歌” or “R&B、温暖、关于爱情的歌”:

- Start with one `music_kb_discover` for the literal request or supported tag
  equivalents. Name directions only after inspecting `facet_counts`;
  `facet_scope.kind=all_matches` covers every canonical match without song
  records. Do not invent directions.
- When there are two or more user-relevant interpretations, build at least two
  and at most **three** complete, meaningfully different branches. If there are
  exactly three important directions, include all three; do not silently reduce
  them to one or two.
- A direction is important when a non-zero exact facet changes what the user
  hears or chooses. A smaller match count alone does not make it unimportant.
  Build the full direction ledger first; do not start branch calls from a
  partial list.
- Run a separate `music_kb_recommend` for every ledger entry. A label without
  its own recommendation call is incomplete. Finish all selected calls before
  answering; never flatten or recombine recommended branches.
- Put the most likely interpretation first with one short reason; never show
  numeric confidence. Keep a song in every branch it genuinely matches and
  label the overlap instead of silently de-duplicating it.
- If a fourth plausible direction matters, say it can be explored later rather
  than implying it does not exist.

The `warm` distinction is evidence-dependent, not a fixed taxonomy: it may mean
安心/陪伴, warm timbre with melancholy, or a credible Soul/Neo-soul direction.
For the approved regression guard, if `R&B + warm + love` discovery has non-zero
`hopeful`, `melancholic`, and `soul` facets, these are three important audible
directions and all three recommendations are required. Do not generalize that
guard into a taxonomy for unrelated requests.

### Choose candidates without making retrieval heavy

- `music_kb_recommend` requires every condition, then orders exact matches by
  group representativeness; only near-relevance rows may be promoted for new
  secondary-tag coverage. Never use title intuition, ingestion recency, or the
  legacy `music_kb_search` row order.
- Use `matched_tags` as hard evidence and `representative_tags` plus
  `selection_basis` for a short ordinary-language reason. Never expose the
  internal enum or numeric score, and do not call the page a universal “best”.
- Compact results omit full tag dumps, summaries, source metadata, and canonical
  text. Verify only a small shortlist when a material claim truly lacks
  evidence; do not infer mood from a title.

### Progressive result volume (方案 1+)

- If `match_count` shows a small result set, show all of it. For a large set,
  show representative candidates and say the direction can be expanded; use a
  grouped, batched presentation for “all”.
- The exact first-page number is intentionally still a calibration parameter.
  When no quantity is given, omit the recommendation `limit` argument and show
  every row returned on that page in stable backend order. Never retrieve a
  larger page and then prune, reorder, or silently de-duplicate it.
- Do not encode “一些/几首 = N” as a permanent default.
- Continue a user-requested expansion with `next_offset`; do not restart at
  offset zero or make the user repeat the request.

### Load low-frequency rules only when triggered

Read [follow-up retrieval and description rules](references/followups.md) only
for insufficient results, “再来一些” / “换一批”, a selected direction, a
complete-description or lyrics request, or a correction. Do not load it for an
ordinary first-turn candidate request.

### Make the first answer learnable

For a representative or expandable result, include immediately:

> 你可以这样继续：
> - “再来一些”：保持这个方向，保留已展示的歌，再补充一批之前没展示过的歌。
> - “换一批”：保持这个方向，换一批之前没展示过的新歌，替换当前展示；之前的结果仍留在对话记录里。

After any non-empty candidate list, ask:

> 想看哪些歌的完整 Music Flamingo 原文、歌词或完整内容？可以回复序号、歌名、“前几首”或“全部”；也可以在后面加“中文翻译”或“摘要”。

The description dimension and output mode are optional. Without either, return
the complete Music Flamingo source text rather than asking for an internal
field or silently translating it. “中文翻译” and “摘要” are explicit alternate
modes; load the follow-up rules for their exact fidelity boundary.

Keep these two affordances distinct: the first retrieves more candidates in the
current direction; the second selects details from songs already displayed.

## Response format and preflight

- Start with how the request was understood and whether the page is complete or
  representative. Render one separate group for every valid branch; the final
  answer must contain one separate group per recommendation and never merge
  those groups into a flat list.
- For each row show an unambiguous visible sequence number, `歌名 — 艺人`, a
  short reason grounded in returned evidence, and a Markdown listening link
  using the exact non-empty runtime `listen_url`.
- If a recording appears in more than one group, keep it in every matching group
  and append a short overlap label such as “也符合：Soul 质感”; never leave a
  cross-group duplicate unexplained. Keep recording IDs, full tags, provenance,
  `selection_basis`, and raw canonical text hidden until the user selects a
  description. A selected complete description defaults to the unmodified
  Music Flamingo source text; load the follow-up rules before rendering it.
- Before answering, mechanically verify that the direction-ledger count,
  distinct recommendation-call count, and group count match; displayed IDs must
  exactly equal each backend page in the same order, including cross-group
  duplicates. Do not infer mood, genre, lyrics, or production from a title.
- Fetch canonical analysis only after a user selection. Resolve a named
  reference before interpreting title words.
- Status must report `search_projection_state=current`. Discovery facets cover
  all matches without song rows. Compact recommendations expose `listen_url`
  but omit `source_links`. This Skill never changes the underlying snapshot.
