# music-kb Codex plugin

This directory is the installable Codex plugin and Python package for the
local-first Music Flamingo analysis knowledge base.

It intentionally contains **code, fixtures, and documentation only**. Do not
put production SQLite files, lyrics, audio, SSH keys, or analysis exports in
this directory or in Git.

> **运行时状态（2026-09，ADR-0001）**：CNB 云端 Music Flamingo 分析运行时已退役——CNB 平台开始收费，不再作为默认或回退链路；本节及 weekly-run 中所有 `cnb_*` 原子仅作历史参考保留。默认分析路径正迁移至本机 MOSS-Music（MLX 8-bit，见 ADR-0001 与 issue #111）。fresh 周更若要触达 CNB 计费入口，现在必须显式传 `--confirm-cnb-analysis`，否则快速失败。现状分析续跑（`--delivery`）不受影响。

## Audio vectors (publisher, snapshots, CLI and MCP, schema v8)

Natural requests such as “找像《X》的歌”, “找节奏像的” and “找音色像的”
resolve title/artist first; ambiguous versions require user selection. Channels
are `overall` (default), `rhythm` and `timbre`, never a blended score.
The [publisher runner](../../runners/audio-embed/README.md) documents
`embed_audio.py` and `evaluate_suno.py`: inference stays in an external
publisher environment; Suno JSON reports stay outside git and never write the KB.
MuQ-MuLan/MERT weights are CC-BY-NC-4.0; MERIT heads are MIT. Internal
research only; commercial use requires authorization or replacement.
Only embedded recordings participate (initially 434/2005); coverage is not
similarity quality. Produce, verify and install a v8 snapshot before client
upgrade; local installation does not distribute it to peers.

Upgrade a publisher v7 master explicitly with `music-kb init --db /path/to/master.sqlite`.
Read-only queries never migrate a client database; v7 clients must receive a
compatible v8 snapshot before upgrading the plugin. Opening a v7 snapshot fails
with `Unsupported schema version 7; expected 8.` No client query migrates it.
These commands do not compute vectors or download weights.

```bash
music-kb --json import-embeddings --db /path/to/master.sqlite \
  --input /path/to/vectors.jsonl --model-id muq-mulan-v3 \
  --model-name MuQ-MuLan --recipe v3 --dim 512 --metric cosine \
  --license CC-BY-NC-4.0
music-kb --json similar --db /path/to/master.sqlite \
  --recording-id rec_example --channel overall --limit 10
```

Input is UTF-8 physical-LF JSONL, one object per line, with a final LF and no
blank lines or CRLF. Each object contains `recording_id`, `audio_sha256`, and
`vector_b64` (base64 of little-endian float32 bytes). Optional `norm` is checked
against the actual Euclidean norm (`rtol=1e-5`, `atol=1e-6`); otherwise it is
computed. Vectors must be finite, nonzero, exactly `dim * 4` bytes, and match
the recording's existing audio hash. Model registration and all rows commit
together or roll back together. Re-import upserts the same recording/model/factor.
Model metadata is immutable; a changed recipe needs a new id.

For MERIT use the same registered model id/recipe with `--dim 128` and import
the heads separately with `--factor rhythm` or `--factor timbre`. Query with
`--channel rhythm` or `--channel timbre`; no melody or fused score exists.
The first overall audio import sets `meta.default_embedding_model`; later
imports preserve it. `--model-id` overrides query selection explicitly. A
rhythm/timbre channel with multiple models requires that override.

`similar` returns compact `results` with recording ID, title, version, artists,
numeric `distance`, listening URL (nullable), and `more_versions` (other embedded
versions of the normalized title, excluding the seed). Results sort by full
cosine distance then recording ID before title collapse and limit (1–50).
Distances are 0–2, smaller is closer, rounded to three decimals in JSON and
printed with three decimal places in human output; they are not percentages
or threshold decisions. Missing vectors and invalid channels return the usual
structured error envelope. Both new commands accept `--json` before or after
the command name.

`doctor` / repository `status()` include `embedding_models`,
`recordings_with_embeddings`, and `embedding_coverage` with a 0–1 coverage
ratio (not a similarity score). `validate` audits vector lengths, norms, audio
ownership and foreign keys. Existing tag retrieval contracts are unchanged.
`music_kb_status` exposes the same counts and coverage. The read-only MCP tool
`music_kb_similar(recording_id, channel="overall", limit=10, model_id=None)` returns the same
compact result as CLI `similar`; it loads no model and requires no write access.
For ambiguous rhythm/timbre recipes, select explicitly with MCP `model_id` or
CLI `--model-id`; omitting the model fails clearly rather than choosing a recipe.

`snapshot create` carries model metadata and vectors in its SQLite backup;
canonical-only pruning cascades deletion of excluded recordings' vectors.
The manifest counts include `embedding_models` and `recordings_with_embeddings`.
`snapshot verify` audits embedding integrity through `validate`, and installation
verifies before switching `current.sqlite`. Installed snapshots remain read-only,
including for embedding imports, even if their file permissions are changed.

MuQ-MuLan/MERT weights are CC-BY-NC-4.0; MERIT heads are MIT. Use internally
for research only until commercial rights are resolved (ADR-0003).

## Natural-language retrieval

The retrieval Skill is designed for a first request in ordinary language. A
user can say, for example, “我需要一些 R&B、温暖的、关于爱情的歌” or “找一些
有氛围感的歌”，without learning canonical English tags or MCP syntax.

For a clear request, the Skill retrieves first and briefly states how it
understood the request. Broad subjective requests begin with
`music_kb_discover`: SQLite counts every canonical match and returns
`facet_counts` without serializing song records. Each supported direction then
uses its own `music_kb_recommend` call. The backend hard-filters the requested
conditions, orders matches by group representativeness, and adds secondary-tag
diversity only among near-relevance rows. The model receives a compact page
containing titles, artists, matching evidence, selection evidence, and listening
links rather than full tag dumps and source metadata. Internal selection-basis
codes are translated into short plain-language reasons rather than shown to the
user.

Two or three supported directions stay in separate answer groups; three
important directions cannot be silently reduced to two or flattened into one
song list. The most likely direction is shown first, and overlapping songs
remain in each matching direction. A selected direction becomes the
conversation context without silently triggering another retrieval. Large
result sets start with a representative, expandable page; small sets can be
shown in full. Stable `next_offset` continuation prevents “再来一些” and
“换一批” from repeating the first page.

The exact first-page quantity and the semantics of “再来一些” versus “换一批”
now keep the selected direction: “再来一些” appends new results to the current
list, while “换一批” replaces the displayed batch with new results from that
same direction. Whenever a response offers an expandable representative set,
it also tells the user in plain language:

> 你可以这样继续：
> - “再来一些”：保持这个方向，保留已展示的歌，再补充一批之前没展示过的歌。
> - “换一批”：保持这个方向，换一批之前没展示过的新歌，替换当前展示；之前的结果仍留在对话记录里。

The Skill must not invent a permanent default quantity or silently broaden a
request. Every listening link shown to a user comes from the runtime
`listen_url` returned by the read-only MCP path.

If the current direction runs short, the Skill first delivers every remaining
valid, not-yet-shown result. It then presents only evidence-backed choices to
relax a less-central condition or try a meaningfully different adjacent
direction. Nothing is broadened or searched on the user's behalf until they
choose; the chosen path then becomes the direction used by later follow-ups.

Candidate results stay light and visibly numbered. After a non-empty result,
the Skill asks which songs the user wants complete descriptions for; the user
can answer with sequence numbers, song titles, “前几首”, or “全部”, without
having to choose an analysis field. These detail selections are separate from
“再来一些” and “换一批”: the former expands already displayed songs, while the
latter two retrieve more candidates in the current direction.

Complete descriptions are loaded only for the selected songs. One to four can
be delivered together; selections of five or more are automatically split into
batches of at most four, and later batches are not fetched in advance. The
selected order and current direction stay intact between batches. “完整描述”、
“完整结果” and “完整 Music Flamingo 输出” return the unmodified canonical
`analysis.raw_text` by default, even in a Chinese conversation. “中文翻译” and
“摘要” are explicit, separately labelled modes: a translation keeps the source
paragraph order and content, while a summary is never presented as the complete
Music Flamingo output. The Skill does not add unsupported musical judgments or
convert an analysis into a generation prompt.

From this directory:

```bash
uv sync --all-groups
uv run music-kb --help
uv run music-kb --json doctor
uv run music-kb-mcp
```

The bundled conversation metric pack reports static contract coverage by
default. It deliberately marks runtime behavior as unmeasured unless
`MUSIC_KB_CONVERSATION_TRACE` points to a normalized trace matching
`evals/conversation-ux/trace-schema.json`; only then does it report separate
direction discovery, compact ranked retrieval, branch execution, and grouped
rendering behavior metrics.

The publisher-only `music-kb publish push` command fans out an immutable
release over SSH/rsync using a private peer TOML file; see
[`../../docs/operations.md`](../../docs/operations.md). It never carries peer
configuration or production data in this plugin.

Publisher-only KuGou campaign imports use `music-kb import-campaign-delivery`
with a strict LF JSONL manifest; see [`../../docs/import-contract.md`](../../docs/import-contract.md).

Schema v7 stores one identity-bound lyric outcome for every canonical
recording. `available` retains normalized ordinary text; `instrumental` and
`platform_unavailable` retain exact-source evidence. Missing/pending lyrics
block snapshots, peer publication, and audio cleanup. New songs receive their
receipt from the fixed direct Kugou download worker. For new audio, that worker
uses the queue's exact Kugou mix-song page and its verified audio hash before
falling back to title/artist search, so it does not expand several text-search
candidates just to discard the wrong MixSongIDs. The one-time historical
backfill defaults to a local exact-source path: it verifies the public Kugou
mix-song page's MixSongID, then uses that page's hash and duration to request
lyrics without downloading audio or invoking a title/artist search. The older
Claude Code executor remains available as `--executor claude` for a bounded
compatibility retry:

```bash
python3 scripts/run_claude_lyrics_backfill.py \
  --workspace /path/to/music-workspace \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --chart-db /path/to/music-workspace/data/music_trends.sqlite \
  --run-id kugou-lyrics-backfill-2026w30 \
  --dry-run
```

Review the generated exact-identity queue first, then remove `--dry-run`.
For legacy source rows, the chart database is used only as an exact public
play-link to MixSongID bridge, never as a title/artist match. The real command
writes receipts under `data/weekly_runs/<run-id>/lyrics-backfill/` and imports
them into the supplied publisher master; it never re-downloads or scans
existing audio/LRC files. When an old exact page no longer exposes a usable
hash, the materialized queue may carry one archived `musicdl` hash only from a
same-`kugou:<MixSongID>`, successfully downloaded inventory record; conflicting
or unproven inventory values are ignored. That no-duration request accepts only
one usable lyric candidate, otherwise it remains pending. If `musicdl` returns
an empty or invalid lyric during a future audio download, the worker retries
the exact page and then only the already MixSongID-validated search-result hash
before recording a pending lyric result.

The complete publisher-side weekly run is `music-kb weekly-run`. It records a
run state and one receipt per atom under `data/weekly_runs/<run-id>/`, reads the
versioned final operating decisions from `references/validated-operations.json`,
and stops at the first failed gate. Each atom receipt includes the validated
operations-file hash. With no `--rank-id`, it uses the versioned
`references/kugou-chart-profile.json` to capture all six configured charts and
their pages; an explicit `--rank-id` is a bounded single-page override. A fresh
run materializes one disposable CNB campaign repository per run id after the
fixed direct download worker. The worker retains one Kugou HTTP session and
resolves the exact mix-song page before the legacy title/artist fallback. It
writes comparable inventory/queue/lookup/download/lyrics/commit timings while
preserving one serial owner of the inventory, progress, and lyric receipt. The
no-results cross-platform fallback is different: its direct wrapper uses two
state-isolated staging shards by default, then merges only after both terminal
receipts succeed and the real inventory has not changed. This is the measured
fast path; it never lets concurrent workers write the durable files directly.
An item receives at most two fallback rounds across runs; a second unsuccessful
round becomes an auditable `abandoned` state and no longer re-enters automatic
download queues. `--retry-abandoned` is the explicit manual recovery path.
`--executor claude` is an explicit
compatibility retry, not the normal weekly dependency. Pass `--delivery <canonical.jsonl>` only to resume from
an already completed CNB delivery; when the same run has a campaign receipt,
the delivery is bound to that receipt and its cleanup still runs after the
release/peer gates. A delivery without a campaign receipt has no campaign
cleanup. A failed run can be resumed with the same `--run-id`: the receipt's
exact repository, GitHub commit, runtime digest, and manifest hash/count are
verified before any campaign retry. If the receipt is already completed and
its delivery hash/count/path still match, the delivery is reused without a
second campaign submit. Pass `--cnb-command <command>` only for the
explicit legacy fallback that writes `MUSIC_KB_CNB_OUTPUT`. Use
`--download-dry-run` or `--cnb-campaign-dry-run` while reviewing a new workflow;
neither creates, pushes, or starts a CNB campaign. Every production
`--publish` run must include `--confirm-delete-audio`,
`--confirm-delete-cnb-storage`, and the separate
`--confirm-delete-cnb-repositories`. The former preserves dedupe metadata while
removing local audio; the latter two remove visible legacy assets and the exact
completed disposable repository, with post-delete 404, charge,
organization-volume, and protected-runtime verification before the run may
succeed. `--cnb-transport` is passed to the disposable campaign and final
storage-cleanup atoms; `git-objects` is accepted only under its policy gates.

After `lyrics_coverage` and before `snapshot`, `audio_embedding` runs the existing
publisher runner sequentially for `overall` and `merit`, then imports each
model/factor into the master using the `import-embeddings` repository path.
Use `--embed-python` or `MUSIC_KB_EMBED_PYTHON` to select the external model
environment; the default is the verified publisher `venv-muq/bin/python` under
`/Users/wycm/Documents/克莱儿/.scratch/mir-song-description/bakeoff/`.
Runner JSONL/receipts live in `data/weekly_runs/<run-id>/audio-embedding/`;
the atom receipt records commands, imported rows, failures and per-channel
coverage. Every canonical recording in this delivery with local SHA256-matched
audio must have all three current vectors. Historical recordings without local
audio only lower reported coverage, not the publication gate. A failure stops
before snapshot and audio cleanup; retain the audio and retry the same run ID
(`--resume` is passed to the runner). Local-audio coverage and the runner use the
same eligibility rule: nonempty `.flac`, `.mp3`, `.ogg`, or `.m4a` files, with no
minimum byte size beyond zero. Scan/hash errors fail rather than disappear.
Download/CNB dry-runs and runs without a delivery skip embedding automatically;
real runs have no embedding bypass. No eligible local audio from this delivery
means no inference is needed.

On a real publish run, the verified release is also installed atomically as the
publisher's local `~/.music-kb/current.sqlite` (or `--local-snapshot-dir`),
independently of whether peer SSH is explicitly skipped. Dry-runs leave the
local current snapshot unchanged unless `--install-local` is supplied;
`--no-install-local` is rejected for real publishes.

Read the repository-level [README](../../README.md) for the publisher/client
workflow and deployment rules.
