# music-kb Codex plugin

This directory is the installable Codex plugin and Python package for the
local-first Music Flamingo analysis knowledge base.

It intentionally contains **code, fixtures, and documentation only**. Do not
put production SQLite files, lyrics, audio, SSH keys, or analysis exports in
this directory or in Git.

## Natural-language retrieval

The retrieval Skill is designed for a first request in ordinary language. A
user can say, for example, “我需要一些 R&B、温暖的、关于爱情的歌” or “找一些
有氛围感的歌”，without learning canonical English tags or MCP syntax.

For a clear request, the Skill searches first and briefly states how it
understood the request. For a broad subjective request, it may search up to
three evidence-backed directions in the first answer so the user can compare
them. The most likely direction is shown first; overlapping songs remain in
each matching direction. A selected direction becomes the conversation
context without silently triggering another search. Large result sets start
with a representative, expandable page; small sets can be shown in full.

The exact first-page quantity and the semantics of “再来一些” versus “换一批”
remain deliberate calibration decisions. The Skill must not invent a permanent
default quantity or silently broaden a request. Every listening link shown to a
user comes from the runtime `listen_url` returned by the read-only MCP path.

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
