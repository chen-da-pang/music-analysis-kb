# music-kb Codex plugin

This directory is the installable Codex plugin and Python package for the
local-first Music Flamingo analysis knowledge base.

It intentionally contains **code, fixtures, and documentation only**. Do not
put production SQLite files, lyrics, audio, SSH keys, or analysis exports in
this directory or in Git.

From this directory:

```bash
uv sync --all-groups
uv run music-kb --help
uv run music-kb --json doctor
uv run music-kb-mcp
```

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
receipt from the existing CC/Kugou download worker. The one-time historical
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
existing audio/LRC files. If `musicdl` returns an empty or invalid lyric during
a future audio download, the download worker retries that same exact-source
path before recording a pending lyric result.

The complete publisher-side weekly run is `music-kb weekly-run`. It records a
run state and one receipt per atom under `data/weekly_runs/<run-id>/`, reads the
versioned final operating decisions from `references/validated-operations.json`,
and stops at the first failed gate. Each atom receipt includes the validated
operations-file hash. With no `--rank-id`, it uses the versioned
`references/kugou-chart-profile.json` to capture all six configured charts and
their pages; an explicit `--rank-id` is a bounded single-page override. A fresh
run materializes one disposable CNB campaign repository per run id after the
Claude Code download. Pass `--delivery <canonical.jsonl>` only to resume from
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

On a real publish run, the verified release is also installed atomically as the
publisher's local `~/.music-kb/current.sqlite` (or `--local-snapshot-dir`),
independently of whether peer SSH is explicitly skipped. Dry-runs leave the
local current snapshot unchanged unless `--install-local` is supplied;
`--no-install-local` is rejected for real publishes.

Read the repository-level [README](../../README.md) for the publisher/client
workflow and deployment rules.
