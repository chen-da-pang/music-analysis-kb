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
   comparison, or the small shortlist needed to verify a branch. Preserve the
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
- For each candidate, show `歌名 — 艺人`, the returned matching evidence, and a
  Markdown listening link when `listen_url` is non-empty. Use the runtime URL
  exactly; never fabricate or substitute a missing link.
- Keep recording IDs, full tag dumps, provenance, and raw canonical text hidden
  unless the user asks. Do not claim popularity, mood, genre, or lyric meaning
  that is absent from returned evidence.
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
