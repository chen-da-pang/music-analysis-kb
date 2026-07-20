---
name: music-kb-weekly-orchestrator
description: Orchestrate a complete weekly Music KB update from a Kugou chart through Claude Code download, CNB Music Flamingo delivery, local import, linked snapshot publication, colleague SSH fan-out, and post-publication audio cleanup. Use when the publisher needs to run or resume the recurring weekly music-library update.
---

# Music KB Weekly Orchestrator

Run this skill on the publisher machine only. The retrieval skill on colleague
machines remains read-only.

Before every atom, read and validate:

```text
plugins/music-kb/references/validated-operations.json
```

That file is the versioned record of the final effective methods extracted from
the Codex and Claude Code conversations. If it is missing, invalid, or does not
contain the current atom, stop before doing any write or network operation.

Every atom must write a receipt under the run directory:

```text
data/weekly_runs/<run-id>/run-state.json
data/weekly_runs/<run-id>/atoms/<atom>.json
```

The receipt records the operation-record hash, input paths/counts, command,
output paths/counts, timestamps, and either `succeeded` or `failed` plus the
bounded error. A failed atom stops the run; resume only from a recorded safe
boundary.

## Invocation-time execution gate

The word "weekly" describes the business update cadence, not a separate
monitoring schedule. Every invocation of `music-kb weekly-run` must run the
local `preflight` atom and then the read-only `cnb_storage_preflight` atom
before chart capture, download, CNB upload, or Music Flamingo analysis. The result is
recorded in that invocation's atom receipt. A failed gate stops this
invocation immediately; it must not start a download or bind compute budget.

Do not create a daily or weekly CNB polling job for this gate. CNB is queried
only as an external execution prerequisite when the skill is actually run.

When `--delivery` supplies an already completed local Music Flamingo canonical
delivery, validate that file and its expected count before any downstream
write. This is a post-analysis resume: record the CNB execution preflight,
chart, dedupe, download, fallback, and CNB input atoms as explicitly skipped;
do not query CNB capacity or repeat any pre-analysis work. Continue with the
idempotent import, snapshot, peer decision, and cleanup atoms.

When `weekly-run` has no explicit `--rank-id`, the chart-capture atom reads
`references/kugou-chart-profile.json` and captures all configured charts page by
page until an empty or short page. Explicit `--rank-id` values remain a
bounded single-page override for targeted checks; they must not be used for a
normal full-library weekly update.

The intended order for each invocation is:

1. local preflight and publisher lock;
2. invocation-time CNB storage preflight: verify the Music Flamingo runtime image is present and no prior campaign branches or temporary run assets remain. The default LFS transport also requires the object-storage counter to be clean; the explicit bounded Git-object fallback requires the ordinary Git-storage counter to have the policy headroom instead;
3. Kugou chart capture and chart-level dedupe;
4. inventory rebuild and historical dedupe;
5. Claude Code download of the bounded queue;
6. Dynamic CNB input materialization from the newly downloaded queue;
7. CNB campaign submission, wait, and quality verification;
8. canonical import, tag enrichment, and source-link completeness gate;
9. snapshot creation and verification;
10. on a real publish run, atomically install the verified release into the
    publisher's local `~/.music-kb/current.sqlite` (or the explicit local
    snapshot target);
11. peer dry-run, schema/plugin compatibility check, and SSH fan-out;
12. after the local release succeeds and either all enabled peers succeed or
    the publisher explicitly uses `--skip-peers`, purge only local audio whose
    platform track ID is present in the verified knowledge base, preserve any
    downloaded but not-yet-analyzed audio, delete completed CNB
    run-input/result/ledger refs and temporary assets, and verify final CNB
    object bytes.

Use the executable `music-kb weekly-run` entry point. Never bypass the run
state or call the old full-database downloader for a weekly update.

The publisher-local install is separate from peer publication. Every real
publish run switches the publisher's `current.sqlite`, even when `--skip-peers`
is explicitly selected. `--no-install-local` is rejected with `--publish` so a
successful peer/cleanup path cannot leave the publisher stale. A dry-run does
not switch it unless `--install-local` is explicitly supplied. The target
defaults to the directory containing the writable master database and can be
overridden with `--local-snapshot-dir`.

The storage policy is `plugins/music-kb/references/cnb-storage-policy.json`. Every production
`--publish` run must include `--confirm-delete-audio`,
`--confirm-delete-cnb-storage`, and the separate
`--confirm-delete-cnb-repositories`; the invocation gate fails before chart
capture or download if any is missing. The repository flag is intentionally
separate because it authorizes irreversible deletion of an entire allowlisted
CNB repository, not just campaign refs. Actual deletion still occurs only
after the release and either every enabled peer has succeeded or `--skip-peers`
was explicitly selected. CNB is a runtime mirror, so completed result/ledger
refs, temporary run assets, and policy-allowlisted completed disposable
repositories are disposable after local export. Preserve only the code mirror
and required runtime image; never store the master database or immutable local
releases in CNB.

Use LFS only when `cnb_storage_lifecycle.py inspect --transport lfs` proves
the object-storage policy clean. When CNB's authoritative counter still shows
orphan LFS after every visible campaign branch and temporary asset has been
cleaned, do not call it reclaimed: CNB's public API exposes LFS download and
read-only charge counters, not a manual object-delete/GC operation. CNB support
documents a default seven-day server-side GC window for unreferenced objects
(`cnb/feedback#4551`). Record `server_gc_pending=true` and the observed counter
in the atom receipt, then use the explicit `--cnb-transport git-objects`
fallback only when its own Git-storage gate passes. It is limited by the policy
and campaign gate to 5 GB total and 256 MiB per file and must still prove
runtime presence, no stale campaign branches/assets, and at least 10 GB ordinary
Git headroom. Pass the same transport to the final CNB cleanup atom. A visible
ref cleanup is successful even when `server_gc_pending` remains true, but a
weekly LFS preflight must stay blocked until the counter is below policy or the
bounded Git-object route passes.

For final cleanup, `visible_cleanup_complete=true`,
`destructive_repository_cleanup_complete=true` when the policy has any
disposable repositories, and no deletion failures are required. A
`server_gc_pending=true` receipt is acceptable only after those explicit
repository deletions have been verified; it means all visible disposable
refs/assets are gone and only CNB's asynchronous server reclamation remains.
If a weekly invocation is blocked before analysis by quota, run
`cnb_storage_lifecycle.py delete-disposable-repositories` with
`--confirm-delete-cnb-repositories`. This independent atom performs the same
allowlist, source, workspace, 404/charge, organization-decrease, and
protected-runtime checks and returns success only when every disposable target
is actually absent. It does not pretend that visible ref deletion reclaimed
orphan LFS in a protected repository.
