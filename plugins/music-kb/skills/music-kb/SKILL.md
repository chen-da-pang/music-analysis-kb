---
name: music-kb
description: Search the local, read-only Music Flamingo analysis knowledge base. Use when a user asks for music-reference retrieval, granular style/tag search, title/artist resolution, or canonical analysis lookup based on the private music KB.
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
3. Use a detail tool only after the user has selected a recording from the
   result: `music_kb_get_canonical_analysis` for the music analysis and
   `music_kb_get_lyrics` for lyrics.

## Selected-song lyrics contract

- First-turn search, title/artist resolution, and tag facets stay compact: do
  not fetch or show full lyrics while presenting candidates.
- If the user asks only for “歌词”, “完整歌词”, or equivalent after selecting a
  recording, call **only** `music_kb_get_lyrics(recording_id=...)`. Return the
  complete stored text when status is `available`; do not replace it with a
  summary, tag, translation, generated text, or `.lrc` timestamps.
- If the user asks for “完整内容”, call both
  `music_kb_get_canonical_analysis(recording_id=...)` and
  `music_kb_get_lyrics(recording_id=...)`, then present the complete music
  analysis and the complete lyrics as separate sections for that exact
  recording.
- `instrumental` and `platform_unavailable` are honest terminal outcomes. Say
  which applies and do not invent lyrics. A `pending` response means the
  published snapshot is incomplete or older; report that boundary rather than
  guessing from a title or another version.
- Never use the lyric tool as a corpus-wide text search. It is a selected,
  recording-ID detail read only.

## Retrieval order

1. **Exact tag / tag alias** for a known micro-genre, production technique,
   vocal treatment, arrangement detail, lyric/theme, or title/artist alias.
2. **Title + artist resolution** when the user names a reference track.
3. **Full-text search** for descriptive language when no controlled tag is
   known.

## Useful MCP calls

- `music_kb_search(query="", tags=["rare tag"], limit=10)`
- `music_kb_resolve_title_artist(title="...", artist="...")`
- `music_kb_get_canonical_analysis(recording_id="...")`
- `music_kb_get_lyrics(recording_id="...")`
- `music_kb_tag_facets(namespace="production", prefix="granular")`

Search and canonical-analysis results include `listen_url` and `source_links`
when the source platform has a listening URL. When presenting a match to a
user, render a Markdown link whose target is the runtime value returned in
`listen_url` (for example, a full `https://www.kugou.com/...` URL); do not
silently drop the link.

If the local snapshot is missing or stale, tell the user to run the documented
snapshot update flow. Do not create or edit a database on their behalf.
