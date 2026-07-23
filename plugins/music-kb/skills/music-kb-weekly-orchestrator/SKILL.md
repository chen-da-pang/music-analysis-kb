---
name: music-kb-weekly-orchestrator
description: Orchestrate a complete weekly Music KB update from the configured Kugou charts through Claude Code download, a disposable CNB Music Flamingo campaign, local import, publisher snapshot installation, optional colleague SSH fan-out, and guarded audio/CNB cleanup. Use when the publisher needs to run, dry-run, or resume the recurring weekly music-library update.
---

# Music KB Weekly Orchestrator

Run this skill on the publisher machine only. Colleague machines are read-only
retrieval clients and receive immutable snapshots, never the writable master.

## Non-negotiable run contract

Before every atom, read and validate:

```text
plugins/music-kb/references/validated-operations.json
```

Require the current atom's entry and record the operation-record hash in its
receipt. If the file is missing, invalid, or disagrees with the atom, stop
before any write, remote call, or compute allocation.

Every atom writes an auditable receipt under:

```text
data/weekly_runs/<run-id>/run-state.json
data/weekly_runs/<run-id>/atoms/<atom>.json
```

Each atom receipt carries the same `operations_sha256` as `run-state.json`,
so the validated operation record used for that atom can be audited on its own.

A failed atom stops the run. Resume only from a recorded safe boundary and
reuse the same run id, campaign repository, and campaign receipt; never create
a replacement slug to hide a failure.

The weekly cadence is an invocation concern, not a polling job. Do not create a
daily CNB monitor for the preflight gate. CNB is queried only when this skill is
actually run.

## Fresh versus delivery-resume

With `--delivery`, validate the supplied canonical Music Flamingo JSONL and its
expected count first. This is a post-analysis resume: mark chart capture,
download, fallback, CNB input, campaign repository, campaign submit, and CNB
analysis as skipped; do not query CNB capacity or repeat pre-analysis work. If
the same run also has a campaign receipt, bind the supplied delivery to that
receipt and still run receipt-bound cleanup after the local release and peer
gates. An external delivery with no campaign receipt has nothing to clean up.

When a run directory already contains a failed or interrupted campaign receipt,
the next invocation with the same `--run-id` is a campaign resume. It restores
the on-disk state and receipt, verifies the exact run id/repository prefix,
GitHub commit, runtime digest, transport, and manifest hash/count, and reuses
only that repository. A new run still refuses any leftover campaign repository.
When that receipt is already `completed` and its delivery path/hash/count still
match, reuse the verified delivery and skip another campaign submit; only an
incomplete receipt may trigger a shard retry.

Without `--delivery`, a normal run uses the versioned Kugou profile and captures
all configured charts page by page until the profile's empty/short-page rule.
An explicit `--rank-id` is only a bounded single-page check, not a full weekly
update. Historical dedupe uses `song_inventory.json`; downloaded audio may later
be purged, so inventory—not file presence alone—is the dedupe record.

## Atom order

1. **`preflight`** — acquire the publisher lock; validate workspace, paths,
   database/config writability, dependencies, and publish prerequisites.
2. **`cnb_storage_preflight`** — on a fresh path run the disposable-campaign
   preflight. Verify the protected `moss-music-runner` repository/main and the
   pinned image digest, organization quota headroom, target absence, and no
   leftover repositories matching `campaign_repository_prefix`. Do not treat
   historical orphan LFS objects in the protected runtime repository as a fresh
   campaign blocker or attempt to delete that runtime. The legacy
   `--cnb-command` path alone uses the old protected-repository storage gate.
3. **`chart_capture`** — save every raw Kugou page JSON, normalize it, and keep
   absolute ranks.
4. **`chart_dedupe`** — dedupe by `kugou:<mix_song_id>`, falling back to
   normalized title/artist, while preserving each song's `play_link`.
5. **`historical_dedupe`** — rebuild the inventory and queue only missing,
   failed, or `no_results` items. Do not interpret a new download as an
   analysis result.
6. **`claude_download`** — call the fixed Claude Code worker. It must use
   `musicdl`'s `MusicClient` plus `KugouMusicClient`; it must not call
   `kugou-cli` or the legacy full-database downloader. Keep one song-level
   inventory row per platform identity.
7. **`fallback_download`** — ask Claude Code to process only the primary
   worker's recorded `no_results`, in the configured fallback order, with the
   duration/size checks. Preserve failed/no-result states for retry.
8. **`cnb_input_materialization`** — consume only newly downloaded queue rows;
   verify file existence, identity, SHA-256, byte count, and `source_url`; use
   hardlinks into an isolated staging directory and write the LF JSONL manifest.
   Require `source_links == source_tracks` for a non-empty campaign.
9. **`cnb_campaign_repository`** — export a code-only runner from a full commit
   reachable from GitHub `origin/main`, verify the export provenance and required
   campaign entry points, then prepare the exact repository
   `campaign_repository_prefix + run_id`. Dry-run only writes a local plan and
   receipt; it must not create a CNB repository, push Git/LFS objects, or start a
   build. Production failure retains the same receipt and repository for resume.
10. **`cnb_campaign_submit`** — trigger the generic
    `api_trigger_music_flamingo_campaign` once per explicit shard, wait for every
    shard to reach a terminal success, restore the durable ledger from the same
    campaign repository, and run the pinned
    `build_kugou_canonical_delivery.py`. Never use build logs as delivery. Any
    failed or incomplete shard blocks delivery and cleanup; a later invocation
    may retry that exact failed shard index in the same receipt, never under a
    new repository slug.
11. **`cnb_analysis`** — validate the canonical delivery and expected count.
12. **`knowledge_import`** — import idempotently, backfill song links, enrich
    retrieval tags, and verify the source-link completeness gate.
13. **`lyrics_import`** — import the automatic CC lyric receipt (and any
    supplied historical receipt) only through exact `source_name +
    source_track_id` identity binding. Title/artist is never a database binding
    key.
14. **`lyrics_coverage`** — recompute available, instrumental,
    platform-unavailable, pending, and missing lyric states. Any unresolved
    recording blocks every following release/cleanup atom.
15. **`snapshot`** — create and verify an immutable SQLite release, manifest, and
    SHA-256.
16. **`local_snapshot_install`** — on a real `--publish`, atomically switch the
    publisher's `~/.music-kb/current.sqlite` (or `--local-snapshot-dir`) after
    verification. `--no-install-local` is incompatible with real publish.
17. **`peer_publish`** — dry-run or SSH/rsync the same immutable release using
    the private `peers.toml`; a real run may explicitly use `--skip-peers`.
18. **`cnb_campaign_cleanup`** — only after local release verification and either
    successful enabled peers or explicit peer skip, delete the exact receipt-bound
    disposable repository when `--confirm-delete-cnb-repositories` is present.
    Verify no running workspace, repository 404, zero repository object volume,
    organization usage decrease when applicable, and protected runtime/main/tag
    survival. Gate blocks and dry-runs are written to the campaign receipt.
19. **`audio_cleanup`** — only after the same release/peer gate and
    `--confirm-delete-audio`; delete audio whose platform track ID is present in
    the verified KB and mark `purged_after_analysis`. Preserve unmatched or
    downloaded-but-not-analyzed audio.
20. **`cnb_storage_cleanup`** — with `--confirm-delete-cnb-storage`, clean only
    visible legacy refs/assets allowed by policy after the disposable repository
    has been handled. Never delete `main`, the protected runtime, the master DB,
    releases, or canonical local evidence. If protected-repository orphan LFS
    remains, record `server_gc_pending=true`; do not claim asynchronous GC is
    complete.

## CNB campaign transport and recovery

`--cnb-transport` is a real transport selection, not a label: it is passed to
the disposable campaign prepare/preflight path and to final storage cleanup.
`lfs` is the default. `git-objects` is allowed only when the policy's total,
per-file, and ordinary Git headroom gates pass. `--cnb-command` is an explicit
legacy fallback and does not use the disposable repository path.

The campaign policy is
`plugins/music-kb/references/cnb-storage-policy.json`. A campaign repository is
temporary; GitHub is the only source of runner code. A failed build, ledger
recovery error, incomplete receipt, release failure, or peer failure leaves the
same repository and receipt in place. A retry may reuse the receipt-bound work
directory and ledger clone, but must pass the immutable identity checks and keep
the same repository slug. Cleanup is irreversible and requires the separate
repository confirmation flag in addition to audio/CNB-storage confirmations.
The cleanup atom blocks unless the receipt also proves repository
creation/push, every shard's success and index set, complete runtime-export
provenance, and a delivery file whose hash/count match the manifest; blocked
and dry-run outcomes are written back to that receipt.

## Invocation examples

Use the executable entry point; do not bypass run state or call the old full
database downloader:

```bash
uv run music-kb --json weekly-run \
  --workspace /path/to/music-workspace \
  --run-id kugou-2026w30 \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --chart-database /path/to/music_trends.sqlite \
  --peers-file "$HOME/.config/music-kb/peers.toml" \
  --cnb-transport lfs \
  --download-dry-run
```

For a real publish, remove `--download-dry-run`, review the peer plan, add
`--publish`, and supply `--confirm-delete-audio`,
`--confirm-delete-cnb-storage`, and `--confirm-delete-cnb-repositories`.
Use `--cnb-campaign-dry-run` to exercise export/config/manifest checks without
creating or pushing a campaign repository. If the current code is intentionally
unpublished, the adapter's `--allow-unpublished` is permitted only on the
low-level `prepare` dry-run for local validation; it is rejected with
`--execute` and is not a production weekly-run bypass. A real production export
must point at a commit already reachable from `origin/main`.
