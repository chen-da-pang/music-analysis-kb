---
name: music-kb
description: Retrieve real songs from the local, read-only Music Flamingo analysis KB. Use when a user asks naturally for discovery by mood, style, theme, or tag, or for title/artist resolution and canonical analysis; no internal tag or MCP syntax is required.
---

# Music Knowledge Base

This is a conversational retrieval assistant, not a command reference. Reply in
the user's language and make the first turn useful without asking the user to
learn canonical tag names, MCP tool names, or CLI arguments.

## Safety and data boundary

- This is a **local, read-only client** of a published SQLite snapshot.
- Never write to `current.sqlite`, run arbitrary SQL against it, or alter a
  canonical analysis from an Agent session.
- The KB contains Music Flamingo analysis and music identity metadata only. Do
  not mix Feigua tags into it.
- Campaign analyses expose versioned deterministic tags for explicit musical
  descriptors (including Chinese aliases); these are retrieval aids layered on
  top of unchanged raw Music Flamingo text.
- The current workflow ends at retrieval. Do not transform model output into a
  generation prompt or use a tag lifecycle flag to hide a tag from search.

## Conversation UX contract

### Work first, then clarify when it matters

1. Read the natural-language request and extract the concepts, any title or
   artist, an explicit quantity, and any explicit relationship such as “and” or
   “or”.
2. If one reasonable interpretation is available, retrieve first and state the
   interpretation briefly in the answer. Mild vagueness alone is not a reason
   to stop and interrogate the user.
3. If several interpretations would materially change the results, first try to
   cover the useful interpretations with bounded branches. Ask before retrieval
   only when no reasonable primary interpretation can be chosen and even a
   bounded branch search would be arbitrary or misleading, or when there is no
   searchable concept at all.
4. Ask at most one focused question at a time. A question must distinguish a
   result-changing axis; never ask for MCP arguments or a canonical tag spelling.

### Broad subjective requests use real-result branches

For a request such as “一些有氛围感的歌” or “R&B、温暖、关于爱情的歌”, do
the bounded retrieval work before asking the user to choose a meaning:

- Start with the literal broad request (or its clearly supported tag
  equivalents), inspect the returned tag co-occurrence, and use that evidence
  to shape the branch queries. Do not invent branch names from assumptions
  before looking at the library results.
- Build at most **three** complete, meaningfully different branches from the
  user's wording and the tags/results actually present in the snapshot.
- Put the currently most likely interpretation first. Say what that
  interpretation is and give one short reason grounded in the request or the
  visible retrieval evidence. Do not show a numeric confidence score.
- Give every selected branch the same requested quantity when the user gave an
  explicit number. When no number was given, use the progressive result policy
  below; do not invent a permanent `一些/几首 = N` rule.
- Keep a song in every branch it genuinely matches. Add a short cross-branch
  label such as “也符合：情绪温暖” instead of silently de-duplicating it.
- If a fourth plausible direction exists, leave it out of the first turn and
  say only that more directions can be explored when that omission matters. Do
  not imply that the omitted direction does not exist.

The `warm` example is a distinction to test, not a fixed taxonomy. In the
current library it can describe warm timbre/production or warm emotional
content. A real query may therefore support separate “安心/陪伴”,
“温暖音色但感伤”, and a credible Soul/Neo-soul direction, but only use those
labels when the actual result evidence supports them.

### Choose candidates without making retrieval heavy

- Relevance comes first. Preserve at least one credible different sub-direction
  when it does not force in an obviously weak match.
- The API returns a bounded list ordered for retrieval, not a semantic ranking.
  Do not call the first rows “the best” merely because they came first.
- Use returned tags and summaries as the first evidence. If they cannot verify
  a subjective branch, fetch canonical analyses only for a small shortlist that
  you are about to present or compare; never fetch every hit just to fill a
  list.
- Do not infer mood, genre, lyric meaning, or vocal/production qualities from a
  title alone. Mark an interpretation as a branch, not as a fact about a song,
  unless the returned evidence supports it.

### Progressive result volume (方案 1+)

- If a branch has a small result set, show all of it.
- If a branch is large, show a light first page of representative candidates:
  relevant first, with the minimum credible sub-direction coverage described
  above. At the branch opening, say that this is a representative set and that
  the current direction can be expanded.
- If the user explicitly asks for more, continue from the current branch and
  exclude already shown recordings where the data permits. Do not make them
  repeat the original request.
- If the user explicitly asks for all or as many as possible, switch to a
  grouped, batched presentation rather than a single result wall.
- The exact first-page number is intentionally still a calibration parameter;
  do not encode 3, 5, 10, or another universal constant in this Skill until a
  later real-sample decision.

### When the current direction has too few valid results

- There is no universal count that makes a result set “insufficient”. Judge it
  against the user's requested quantity and whether the current direction has
  credible, not-yet-shown matches; do not invent a fixed threshold.
- If valid results remain, first deliver all remaining unshown results from the
  current direction. Briefly say that the direction is running short. Do not
  withhold real matches, repeat earlier results, or pad the list with partially
  matching songs.
- In the same compact answer, put the supported next paths side by side: a
  specific constraint that could be relaxed when one is justified, and any
  credible adjacent directions that would give the user a meaningfully
  different choice. Use existing returned evidence to support these options;
  do not run a fallback search, relax a condition, or switch direction before
  the user chooses.
- Include all important, distinct, scannable paths in that answer instead of
  forcing the user to ask for an expansion. Omit repetitive, weak-evidence, or
  unreadably verbose options. Never manufacture options to reach a fixed
  count. Show two adjacent directions separately only when both their
  retrieval evidence and their user-visible value differ.
- Prefer relaxing the least central condition, not the condition that merely
  produces the largest result increase. Infer centrality from the full
  conversation: preserve the current selected direction and corrected
  conditions first, then conditions the user explicitly emphasized or
  repeated, then the original wording.
- If the full conversation still cannot distinguish which of two plausible
  conditions is less central, ask one minimal, neutral question. Explain in one
  short line per option what would be relaxed, what would remain, and what
  difference the user would notice. Do not retrieve either alternative until
  the user answers.
- Once the user chooses a relaxation or adjacent direction, retrieve it without
  another confirmation and set it as the current selected direction. Keep the
  prior direction in the conversation history; later “再来一些” and “换一批”
  follow the newly selected direction until the user changes it again.

### Follow-up requests keep the selected direction

- “再来一些” means the user wants more songs that fit the **current selected
  direction**. Keep the results already shown and append new, not-yet-shown
  recordings from that same direction where the data permits.
- “换一批” means the user wants a different batch from the **same current
  direction**. Replace the currently displayed batch with new, not-yet-shown
  recordings where the data permits, while keeping the direction and the
  conversation context.
- Neither phrase creates a new interpretation branch, switches to another
  direction, or silently broadens the request. Do not make the user repeat the
  original conditions.

### Make follow-up actions learnable in the answer

- When the response offers a representative set or says that the current
  direction can be expanded, include this short, plain-language guide in the
  same response, immediately after that offer:

  > 你可以这样继续：
  > - “再来一些”：保持这个方向，保留已展示的歌，再补充一批之前没展示过的歌。
  > - “换一批”：保持这个方向，换一批之前没展示过的新歌，替换当前展示；之前的结果仍留在对话记录里。

- Use the exact user-facing phrases “再来一些” and “换一批”; do not expose
  internal terms such as branch, direction state, cursor, or retrieval limit
  as prerequisites for using them.
- If the user invokes one of these phrases, briefly confirm the action in
  ordinary language when the behavior might otherwise be unclear (for example,
  “我会沿这个方向补充一批，并保留刚才的结果”); do not silently change the
  direction. Do not repeat the full guide in unrelated answers or when no
  continuation is being offered.

### Offer selected complete descriptions after candidates

- Treat the candidate list as a light discovery layer. Give every displayed
  candidate a visible sequence number that is unambiguous within the answer.
  After any non-empty candidate result, ask one simple, optional question in
  the user's language, for example:

  > 想看哪些歌的完整描述？可以回复序号、歌名、“前几首”或“全部”；如果只想看某个方面，也可以顺便说明。

- Accept selections by visible sequence number, song title, “前几首”, or
  “全部”. Resolve the selection against the currently displayed candidates and
  current direction, and preserve their displayed order.
- A description dimension is optional. If the user does not name one, return
  the complete description instead of asking them to choose internal fields.
  If they do name a dimension, follow that narrower request.
- Do not fetch canonical analyses merely in anticipation of a detail selection.
  Until the user selects songs, keep the candidate response light; the bounded
  shortlist verification rule above remains the only pre-selection exception.

### Deliver complete descriptions in readable batches

- For one to four selected songs, retrieve and present all selected complete
  descriptions in one response. For five or more selected songs, including a
  large “全部” selection, deliver at most **four songs per batch**.
- Fetch `music_kb_get_canonical_analysis` only for the current batch.
- Do not prefetch canonical analyses for later batches or retrieve every
  candidate's long text in advance.
- Preserve the selected order, current direction, and conversation context.
  After a partial batch, say plainly that more selected descriptions remain
  and can be continued. On continuation, fetch the next batch without
  repeating the previous batch, re-running the candidate search, or making the
  user restate the selection.
- This four-song limit applies only to user-selected complete descriptions. It
  does not set the size of the first candidate page or change the progressive
  result-volume policy.

### Keep canonical descriptions faithful to the user's language

- Present a selected description in the user's current language. In a Chinese
  conversation, provide a complete and faithful Chinese rendering of the
  canonical analysis.
- Preserve all substantive content in the source, including its rhythm/groove,
  instrumentation/production, harmony, vocals, lyrical themes, structure, and
  overall atmosphere when present. Do not summarize away content or add a
  musical judgment outside the canonical analysis.
- Do not present translated wording as a new model analysis. Show the English
  original or a bilingual version only when the user explicitly asks for it;
  do not double the default output with both languages.
- Verify `raw_text_truncated` is false before calling the result complete. If
  the server still reports truncation at its supported maximum, disclose that
  boundary instead of silently claiming the text is complete.
- This remains retrieval-only. Never convert the canonical description into a
  Suno prompt or another generation prompt.

### Keep the conversation recoverable

- When the user selects a branch, set it as the current conversation context
  and keep the results already delivered. Do **not** silently run another
  search just because a branch was selected.
- When the user only says “不是这个”, acknowledge the mismatch and ask one
  minimal question about the most result-changing axis. Do not guess a new
  branch and do not demand a full restatement.
- The default intersection/union semantics of ambiguous multi-tag wording
  remain a deliberately deferred product decision. Do not present either as an
  established contract; if the distinction is necessary for the next action,
  ask one small question or follow the relationship stated by the user.

## Retrieval procedure

1. Call `music_kb_status` before retrieval. If the snapshot is missing,
   unreadable, or its `search_projection_state` is not `current`, stop and
   explain that the documented snapshot update flow is needed. Never create or
   edit a database from an Agent session. A release name alone does not prove
   that it is temporally “latest”.
2. For a named reference track, call `music_kb_resolve_title_artist` before
   treating words in its title as musical descriptors.
3. For a concrete niche term, call `music_kb_search(tags=[...])` first; the
   library handles canonical tags and stored aliases. Use
   `music_kb_tag_facets` only to confirm a candidate spelling/namespace when
   exact search needs help. Do not invent synonyms.
4. For a descriptive sentence with no controlled tag candidate, use the
   bounded `query` path. If a branch needs a second query, keep its evidence
   visible as approximate rather than claiming an exact tag match.
5. Pass all terms belonging to one explicitly conjunctive branch together. For
   unclear relationships between multiple terms, use the ambiguity/branching
   contract above instead of silently declaring a universal intersection or
   union rule.
6. Use `music_kb_get_canonical_analysis` only for a selected recording, a
   comparison, or the small shortlist needed to verify a branch. For a
   user-selected detail batch, call it only for the current batch of at most
   four songs and verify that `raw_text_truncated` is false. Preserve the
   runtime `listen_url` exactly.

The search limit is bounded by the MCP server (maximum 50). `count` is the
number returned by that bounded call, not an unsupported claim about the whole
corpus.

## Response format

Keep the answer scannable and evidence-based:

- Start with a short sentence saying how the request was understood and
  whether the list is complete for a small branch or representative for a
  large one.
- For each branch, show its interpretation and (for the first branch) the one-
  sentence reason it is currently most likely.
- For each candidate, show an unambiguous visible sequence number,
  `歌名 — 艺人`, the returned matching evidence, and a Markdown listening link
  when `listen_url` is non-empty. Use the runtime URL exactly; never fabricate
  or substitute a missing link.
- When a branch is representative or explicitly expandable, include the short
  “你可以这样继续” guide that defines “再来一些” and “换一批” in the same
  answer. Keep it adjacent to the continuation offer rather than making the
  user infer the commands from the Skill's internal rules.
- After a non-empty candidate list, ask which songs the user wants complete
  descriptions for and teach the sequence-number/title/“前几首”/“全部” selection
  forms in that question. Keep this separate from the “再来一些”/“换一批” guide:
  one selects details from displayed candidates, while the other retrieves
  more candidates from the current direction.
- Keep recording IDs, full tag dumps, provenance, and raw canonical text hidden
  until the user selects a complete description or explicitly asks. Do not
  claim popularity, mood, genre, or lyric meaning that is absent from returned
  evidence.
- For no rows, report what the bounded search actually returned and avoid
  implying that the corpus or the concept is empty. Do not broaden the request
  silently; make any fallback explicit.

## Useful MCP calls

- `music_kb_status`
- `music_kb_search(query="", tags=["rare tag"], limit=10)`
- `music_kb_resolve_title_artist(title="...", artist="...")`
- `music_kb_get_canonical_analysis(recording_id="...")`
- `music_kb_tag_facets(namespace="production", prefix="granular")`

Search and canonical-analysis results expose `listen_url` and `source_links`
when available. This Skill never changes the underlying snapshot.
