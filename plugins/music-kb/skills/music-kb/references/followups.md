# Follow-up retrieval and description rules

Read this file only when the current result set is insufficient, the user asks
for more or a replacement batch, selects complete descriptions, chooses a
direction, or corrects the result. The first-turn candidate flow stays in the
parent Skill.

## When the current direction has too few valid results

- There is no universal count for “insufficient”; compare the requested quantity
  with credible unshown matches, never a fixed threshold.
- If valid results remain, deliver all remaining unshown results and say the
  direction is running short. Never withhold, repeat, or pad with partially
  matching songs.
- Put supported next paths side by side: one justifiable relaxation and credible,
  meaningfully different adjacent directions. Ground them in existing returned
  evidence; do not retrieve or switch before the user chooses.
- Include every important, distinct, scannable path; omit repetitive, weak, or
  unreadable options and never manufacture a fixed count. Separate adjacent
  directions only when evidence and user-visible value both differ.
- Relax the least central condition, not the biggest count booster. Use the full
  conversation: preserve the current selected direction and corrections, then
  emphasized or repeated conditions, then original wording.
- If the full conversation cannot distinguish two plausible relaxations, ask
  one minimal, neutral question. In one line each, say what changes, what stays,
  and the audible difference; retrieve neither before the answer.
- Once the user chooses a relaxation or adjacent direction, retrieve it without
  another confirmation and set it as the current selected direction. Keep the
  prior direction in the conversation history; later “再来一些” and “换一批”
  follow the newly selected direction until the user changes it again.

## Follow-up requests keep the selected direction

- “再来一些” means the user wants more songs that fit the **current selected
  direction**. Keep the results already shown and append new, not-yet-shown
  recordings from that same direction by continuing its `next_offset`.
- “换一批” means the user wants a different batch from the **same current
  direction**. Replace the currently displayed batch with new, not-yet-shown
  recordings from its `next_offset`, while keeping the direction and the
  conversation context.
- Neither phrase creates a new interpretation branch, switches to another
  direction, or silently broadens the request. Do not make the user repeat the
  original conditions.

## Deliver complete descriptions in readable batches

- Accept selections by visible sequence number, song title, “前几首”, or
  “全部”. Resolve the selection against the currently displayed candidates and
  current direction, and preserve their displayed order.
- A description dimension is optional. If the user does not name one, return
  the complete description instead of asking them to choose internal fields.
  If they do name a dimension, follow that narrower request.
- For one to four selected songs, retrieve and present all selected complete
  descriptions in one response. For five or more selected songs, including a
  large “全部” selection, deliver at most **four songs per batch**.
- Fetch `music_kb_get_canonical_analysis` only for the current batch. Do not
  prefetch canonical analyses for later batches or retrieve every candidate's
  long text in advance.
- Preserve the selected order, current direction, and conversation context.
  After a partial batch, say plainly that more selected descriptions remain
  and can be continued. On continuation, fetch the next batch without
  repeating the previous batch, re-running the candidate search, or making the
  user restate the selection.
- This four-song limit applies only to user-selected complete descriptions. It
  does not set the size of the first candidate page or change the progressive
  result-volume policy.

## Keep canonical descriptions faithful to the user's language

- Present a selected description in the user's current language. In a Chinese
  conversation, provide a complete and faithful Chinese rendering of the
  canonical analysis.
- Preserve all substantive content in the source, including rhythm/groove,
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

## Keep the conversation recoverable

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
