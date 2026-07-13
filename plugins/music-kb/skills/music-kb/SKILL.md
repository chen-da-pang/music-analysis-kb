---
name: music-kb
description: Search the local, read-only Music Flamingo analysis knowledge base and turn approved audible tags into safe Suno style directions. Use when a user asks for music-reference retrieval, granular style/tag search, canonical analysis lookup, or a Suno-safe style prompt based on the private music KB.
---

# Music Knowledge Base

## Safety boundary

- This is a **local, read-only client** of a published SQLite snapshot.
- Never write to `current.sqlite`, run arbitrary SQL against it, or alter a
  canonical analysis from an Agent session.
- The KB contains Music Flamingo analysis and music identity metadata only. Do
  not mix Feigua tags into it.
- Do not use artist names, song titles, lyrics, or a recoverable original
  melody as a Suno prompt. Use only returned `suno_safe` tags and audible
  production descriptions.

## Start here

1. Call `music_kb_status` to confirm that a local snapshot is available.
2. Use `music_kb_search` with exact tags first when the request contains a
   niche term; aliases are searched too.
3. Use `music_kb_get_canonical_analysis` only for selected recordings.
4. Use `music_kb_compile_suno_style` to create a safe style direction. Keep
   the returned exclusions intact.

## Retrieval order

1. **Exact tag / tag alias** for a known micro-genre, production technique,
   vocal treatment, arrangement detail, or title/artist alias.
2. **Title + artist resolution** when the user names a reference track.
3. **Full-text search** for descriptive language when no controlled tag is
   known.
4. **Suno compiler** only after selecting records/tags; it intentionally omits
   artist, title, lyric, and melody information.

## Useful MCP calls

- `music_kb_search(query="", tags=["rare tag"], limit=10)`
- `music_kb_resolve_title_artist(title="...", artist="...")`
- `music_kb_get_canonical_analysis(recording_id="...")`
- `music_kb_tag_facets(namespace="production", prefix="granular")`
- `music_kb_compile_suno_style(recording_ids=["..."], selected_tags=[])`

If the local snapshot is missing or stale, tell the user to run the documented
snapshot update flow. Do not create or edit a database on their behalf.
