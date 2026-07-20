---
name: music-kb
description: Search the local, read-only Music Flamingo knowledge base. Use when users ask in natural language (including Chinese “找几首带某个标签的歌”) for music retrieval, granular tag/style search, title/artist resolution, or canonical analysis. Users need not know internal tags or MCP/CLI syntax.
---

# Music Knowledge Base

## Safety boundary

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

## Start here

1. Call `music_kb_status` to confirm that a local snapshot is available.
2. Use `music_kb_search` with exact tags first when the request contains a
   niche term; aliases are searched too.
3. Use `music_kb_get_canonical_analysis` only for selected recordings.

## User-facing interaction contract

This Skill is a conversational retrieval assistant, not a command reference.
Accept a natural-language request as-is; do not make the user translate a
Chinese or colloquial descriptor into an internal English tag before searching.
Reply in the user's language and keep the matched canonical tag in parentheses
when that helps them learn the vocabulary.

### Read the request before choosing a call

- Extract the searchable concept(s), an optional title/artist, an optional
  quantity, and whether the user means **and** or **or**. “一些/几首” means
  up to 10 results when no number is supplied; honor an explicit number up to
  the MCP limit of 50.
- If the request contains a concrete tag-like term, search it as supplied.
  The user should not have to know whether the stored form is English or
  Chinese. If the request is a descriptive sentence rather than a tag, use the
  full-text query path instead of pretending it is a controlled tag.
- For multiple `and` terms, pass all terms in one `tags` list (the search is
  conjunctive). For `or`, make separate bounded searches, merge by
  `recording_id`, and remove duplicates before presenting results.
- Ask at most one focused clarification only when there is no searchable
  concept, title, or artist at all. Do not ask the user to provide MCP
  arguments or a canonical tag name.

### Search and disclose the match mode

Use this order so a rare tag remains exact without making ordinary language
hard to use:

1. Search the supplied term with `music_kb_search(tags=[...])`. This covers a
   canonical tag or an alias.
2. If that returns no rows, call `music_kb_tag_facets` with the non-empty term
   as `prefix`. Retry only with canonical names or aliases actually returned
   by the facet call; do not invent synonyms.
3. If no controlled tag candidate is found, optionally retry with
   `music_kb_search(query=...)`. Mark those rows as **全文命中/近似检索**, not
   as proof that the requested tag exists.

Tell the user which path produced the rows: `标签命中` (input term or a
verified library alias), `规范标签/别名命中` (a facet-confirmed candidate), or
`全文命中/近似检索`. A fallback must never be presented as an exact tag match.

Do not call `music_kb_get_canonical_analysis` for every hit just to fill space.
Fetch it only when the user selects a song, asks “为什么匹配/详细分析”, or
requests a comparison.

## Response contract

Keep the first response useful without requiring a second lesson:

1. Open with the match mode and the bounded result count, for example:
   “按「侧链」做标签检索，本次返回 5 首（显示上限 5）。” Do not call that
   number the total in the database; MCP `count` is the number returned by that
   bounded call.
   Use a short list by default: `歌名（试听链接）— 艺人` followed by
   `匹配依据：<returned matching tag(s)>`; render the parenthesized link as
   Markdown using the runtime URL described below.
2. For each result, show the title, artist(s), and only the returned evidence
   relevant to the request (usually the matching tag). Do not infer a mood,
   genre, popularity, or reason that is absent from `tags`/`summary`.
3. When `listen_url` is non-empty, render it as a Markdown link using the
   runtime URL exactly. When it is empty, say that this record has no
   available listening link; never fabricate or substitute a URL.
4. Keep the full tag dump, recording ID, provenance, and raw analysis hidden
   unless the user asks for them. Offer the canonical-analysis lookup when a
   particular result needs explanation, rather than returning every analysis.

For zero rows, say what was actually tried: no verified controlled-tag match,
whether a facet candidate existed, and whether the text fallback also returned
nothing. Do not imply that the corpus is empty or that the song concept is
impossible. If the fallback found rows, present them as approximate and keep
the original requested term visible.

Before any retrieval, treat `music_kb_status` as a gate. If the snapshot is
missing, unreadable, or reports `search_projection_state` other than `current`,
stop and explain that the documented snapshot update flow is needed. A release
name alone proves the version being queried, not that it is temporally “latest”.
Do not edit or create a database from an Agent session.

For a named reference track, use `music_kb_resolve_title_artist` before treating
any words in its title as musical descriptors.

## Useful MCP calls

- `music_kb_search(query="", tags=["rare tag"], limit=10)`
- `music_kb_resolve_title_artist(title="...", artist="...")`
- `music_kb_get_canonical_analysis(recording_id="...")`
- `music_kb_tag_facets(namespace="production", prefix="granular")`

Search and canonical-analysis results expose `listen_url`/`source_links` when
available; use the runtime URL exactly and never invent a missing link.
