---
name: music-kb-weekly-orchestrator
description: Orchestrate a complete weekly Music KB update from the configured Kugou charts through a fixed direct download worker, a disposable CNB Music Flamingo campaign, local import, publisher snapshot installation, optional colleague SSH fan-out, and guarded audio/CNB cleanup. Use when the publisher needs to run, dry-run, or resume the recurring weekly music-library update.
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

## CNB history gate

Before responding to a CNB failure or allocating another CNB workspace, first
read the current run's receipt and attempts, the linked GitHub Issue, and the
relevant prior rollout/thread evidence. Classify the failure before choosing an
operation. Do not treat a familiar error as a new code defect, and do not use
an immediate retry as a substitute for that review.

For executable `cnb_campaign_devgpu_recovery`, write a receipt-bound history
review JSON and pass it through `--history-review`. It must bind the current
source-receipt SHA-256 and operation-record SHA-256, name the failure
fingerprint, cite at least one prior evidence source and the operation being
reused, and record the chosen action/rationale. The recovery receipt stores its
path and hash before a workspace can be allocated.

Use the previously verified operation for a recognized fingerprint:

- A large image pull in `Prepare` is preparation time, not a model failure.
- `initial_dirty_l40_allocation` fails before hydrate under the existing
  40,000 MiB / 0% gate. Confirm the stopped workspace is absent, preserve the
  same receipt, and inspect that failed gate receipt's measured GPU snapshot
  plus visible compute-process evidence. An empty or unavailable process table
  does not prove another tenant: record it as an attribution limit. Only then
  decide whether a deliberately reviewed same-receipt retry is justified; do
  not change the batch, model, threshold, slug, or create a probe.
- A prior shard still holding VRAM is the distinct post-shard release case:
  use the bounded same-threshold release gate before the next shard.
- A recovery runner refresh must come from a full commit already on GitHub
  `origin/main`, and may update only the explicitly allowed operational gate
  files. Its commit and file hashes are recorded on the same recovery receipt;
  never copy a local or CNB working tree into the campaign branch.
- An outer CNB stage marked successful without supervisor/report/ledger/canonical
  evidence is the false-success case: fail closed and use the foreground
  supervisor plus durable evidence contract.

If the history review does not yield a known safe operation, stop before
allocating compute and record the unresolved decision in the owning Issue.

For a nonzero-pending Dev GPU recovery, immediately before `start-workspace`,
read the official CNB organization `get-quota` / `get-volume` fields and record
`available_dev_gpu_seconds = dev_gpu_in_sec.total - dev_gpu_in_sec -
freeze_dev_gpu_in_sec`. The versioned policy supplies a per-pending-item
estimate and fixed headroom; both must fit in that available capacity. Missing,
non-integer, or negative total/used values, a present malformed/negative frozen
value, or negative/insufficient available capacity blocks allocation. CNB omits
`freeze_dev_gpu_in_sec` when zero in its current response: normalize only that
absent field to zero and record the normalization. This capacity proof does not
replace the existing history, receipt, clean-GPU or supervisor gates; zero
pending still bypasses workspace allocation entirely.

## Fresh versus delivery-resume

With `--delivery`, validate the supplied canonical Music Flamingo JSONL and its
expected count first. This is a post-analysis resume: mark chart capture,
download, fallback, CNB input, campaign repository, campaign submit, and CNB
analysis as skipped; do not query CNB capacity or repeat pre-analysis work. If
the same run also has a campaign receipt, bind the supplied delivery to that
receipt and still run receipt-bound cleanup after the local release and peer
gates. An external delivery normally has no disposable repository to clean up.
The narrow legacy exception is a local source-run receipt that is still
`failed` or `interrupted`, has no delivery field, and maps exactly to the
delivery's campaign ID. After the new release and peer gate, cleanup must create
a separate external-delivery reconciliation receipt—without editing the failed
receipt—and compare every source-manifest and delivery ID, index, path, hash,
byte count, and listening URL against the verified release provenance. Only
that receipt may authorize the dedicated destructive cleanup.

When a run directory already contains a failed or interrupted campaign receipt,
the next invocation with the same `--run-id` is a campaign resume. It restores
the on-disk state and receipt, verifies the exact run id/repository prefix,
GitHub commit, runtime digest, transport, and manifest hash/count, and reuses
only that repository. A new run still refuses any leftover campaign repository.
For a different fresh run, an old campaign repository remains a blocker by
default. The sole exception is an explicit `--retained-campaign-receipt`: it
must prove one exact prefix-mapped repository is `failed` or `interrupted`, was
created and pushed, has no delivery, and names a workspace that CNB reports as
not running. The preflight records the supplied receipt path/hash and still
requires all other prefix repositories to be absent. It passes that same proof
to repository preparation, where quota, target-absence, transport and the new
manifest's actual byte estimate are checked again. Never infer this exception
from repository discovery or use it for a completed/mismatched/active receipt.
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
   leftover repositories matching `campaign_repository_prefix`; only the
   explicit receipt-bound retained exception above may coexist. Do not treat
   historical orphan LFS objects in the protected runtime repository as a fresh
   campaign blocker or attempt to delete that runtime. The legacy
   `--cnb-command` path alone uses the old protected-repository storage gate.
3. **`chart_capture`** — save every raw Kugou page JSON, normalize it, and keep
   absolute ranks.
4. **`chart_dedupe`** — dedupe by `kugou:<mix_song_id>`, falling back to
   normalized title/artist, while preserving each song's `play_link`.
5. **`historical_dedupe`** — rebuild the inventory and queue only missing,
   failed, or `no_results` items. `abandoned` is a durable terminal download
   state and requires an explicit `--retry-abandoned` recovery. Do not
   interpret a new download as an analysis result.
6. **`claude_download`** — retain this historical atom name but default to one
   fixed direct worker. It must use `musicdl`'s `MusicClient` plus
   `KugouMusicClient`; it first resolves the queue's exact mix-song page and
   verified audio hash, then falls back to title/artist search only when needed.
   It must not call `kugou-cli` or the legacy full-database downloader. Keep one
   song-level inventory row per platform identity and do not run concurrent
   workers against shared state. `--executor claude` is only a bounded
   compatibility retry.
7. **`fallback_download`** — directly process only the primary worker's
   recorded `no_results` or `failed` states in the configured fallback order,
   with duration/size checks. `run_claude_fallback.py` validates a Python that
   imports `musicdl` and starts a short detached supervisor. Its default two
   isolated staging shards never touch real inventory, progress, or audio; one
   serial merger is the sole formal-state writer. `--executor claude` is a
   compatibility launcher only. Preserve `retry_from_status` and fallback
   attempt history; after the second unsuccessful fallback round, write
   `abandoned` and do not requeue it automatically.
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
10b. **`cnb_campaign_devgpu_recovery`** — only after the same receipt has
    failed at a CNB build-GPU platform gate, keep that source receipt immutable
    and write a separate recovery receipt. Reuse its exact repository, main
    commit, manifest, runtime and durable ledger; choose an explicit configured
    Dev GPU profile (default L40; H20 and non-default L40-35G-VALIDATION only
    through their versioned profiles), then
    run every configured shard serially after two profile-specific
    clean-allocation gates and a pre-model gate for each shard. This is a full
    resume, never a probe. L40-35G-VALIDATION remains bound to a real L40
    runner/execution profile and requires the receipt-bound history review's
    exact `user_authorized_l40_35g_full_serial_recovery` action; it cannot
    lower the default L40 gate. Stop the workspace before recovering an external
    canonical delivery, then continue through `--delivery` and external-delivery
    reconciliation.
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
    A legacy failed/interrupted receipt with no delivery can use only its
    separately revalidated external-delivery reconciliation receipt; never
    rewrite the old receipt to pretend it completed. Verify no running workspace,
    repository 404, zero repository object volume, organization usage decrease
    when applicable, and protected runtime/main/tag survival. Gate blocks and
    dry-runs are written to the corresponding receipt.
19. **`audio_cleanup`** — only after the same release/peer gate and
    `--confirm-delete-audio`; delete audio whose platform track ID is present in
    the verified KB and mark `purged_after_analysis`. For a supplied canonical
    delivery, also remove only receipt-bound local CNB input/check-out audio
    whose manifest exactly matches every delivery ID, source hash/bytes, and
    source URL; preserve the manifests, canonical evidence, and every unmatched
    or downloaded-but-not-analyzed audio file.
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

CNB CLI process exit code alone is not success evidence: every JSON response
must have a 2xx top-level API status before an atom records progress. An
optional-resource 404 may become an audited absence only in the explicit probe
path; a 403 (including a missing `repo-delete:rw` scope) or any other non-2xx
status stops the atom before it can claim a repository was created, deleted, or
cleaned.

The campaign policy is
`plugins/music-kb/references/cnb-storage-policy.json`. A campaign repository is
temporary; GitHub is the only source of runner code. A failed build, ledger
recovery error, incomplete receipt, release failure, or peer failure leaves the
same repository and receipt in place. A retry may reuse the receipt-bound work
directory and ledger clone, but must pass the immutable identity checks and keep
the same repository slug. Cleanup is irreversible and requires the separate
repository confirmation flag in addition to audio/CNB-storage confirmations.
Build-GPU quota failure may use only the explicit Dev GPU recovery atom. It
must not edit the failed source receipt or claim failed build records succeeded;
the recovery delivery remains external until local release provenance has been
reconciled.
The cleanup atom blocks unless the receipt also proves repository
creation/push, every shard's success and index set, complete runtime-export
provenance, and a delivery file whose hash/count match the manifest. A legacy
receipt without that delivery remains blocked unless a separately stored
external-delivery reconciliation proves the untouched receipt's manifest,
external delivery, and verified release provenance are exactly the same.
Blocked and dry-run outcomes are written back to the corresponding receipt.

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
  --proxy http://127.0.0.1:7890 \
  --cnb-transport lfs \
  --download-dry-run
```

When a receipt has the recorded build-GPU platform failure and the explicit
Dev GPU recovery atom is selected, keep the source receipt untouched:

```bash
uv run python scripts/cnb_campaign_repository.py recover-devgpu \
  --policy references/cnb-storage-policy.json \
  --operations-file references/validated-operations.json \
  --repository-root /path/to/music-analysis-kb \
  --github-commit "$(git -C /path/to/music-analysis-kb rev-parse origin/main)" \
  --receipt /path/to/run/cnb/campaign-receipt.json \
  --recovery-receipt /path/to/run/cnb/devgpu-recovery/receipt.json \
  --history-review /path/to/run/cnb/devgpu-recovery/history-review.json \
  --devgpu-profile H20 \
  --run-dir /path/to/run \
  --transport lfs \
  --execute --wait
```

On the publisher, prefer an existing repository credential only as
`MUSIC_KB_CNB_GIT_TOKEN`. If it is not configured but `cnb status` confirms
the administrator OAuth login, the adapter confines the official
`cnb git-credential` helper to Git/LFS subprocesses after clearing inherited
helpers; it does not extract, persist, or export an OAuth token. The adapter
always strips both Git-token variables from CNB CLI subprocesses so workspace
start/stop keeps using the logged-in administrator OAuth. Do not export a
repository credential as `CNB_TOKEN` for the whole recovery process.

After the recovery receipt proves a complete canonical delivery, invoke the
weekly publisher with that delivery path. The existing post-analysis resume
and external-delivery reconciliation own import, release and cleanup; never
copy recovery success fields into the failed campaign receipt.

On the publisher Mac this is the fastest measured download profile: the proxy
is propagated to chart capture, the serial Kugou worker, and the two-way
fallback wrapper. Confirm that the local listener is healthy first. The default
route is a system TUN rather than a bare direct connection; if the listener is
unavailable, omit `--proxy` and use that route instead.

For a real publish, remove `--download-dry-run`, review the peer plan, add
`--publish`, and supply `--confirm-delete-audio`,
`--confirm-delete-cnb-storage`, and `--confirm-delete-cnb-repositories`.
Use `--cnb-campaign-dry-run` to exercise export/config/manifest checks without
creating or pushing a campaign repository. If the current code is intentionally
unpublished, the adapter's `--allow-unpublished` is permitted only on the
low-level `prepare` dry-run for local validation; it is rejected with
`--execute` and is not a production weekly-run bypass. A real production export
must point at a commit already reachable from `origin/main`.
