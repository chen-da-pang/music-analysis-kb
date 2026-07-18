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

When `weekly-run` has no explicit `--rank-id`, the chart-capture atom reads
`references/kugou-chart-profile.json` and captures all configured charts page by
page until an empty or short page. Explicit `--rank-id` values remain a
bounded single-page override for targeted checks; they must not be used for a
normal full-library weekly update.

The intended order is:

1. preflight and publisher lock;
2. CNB storage preflight: verify the Music Flamingo runtime image is present and no prior campaign branches or temporary run assets remain. The default LFS transport also requires the object-storage counter to be clean; the explicit bounded Git-object fallback requires the ordinary Git-storage counter to have the policy headroom instead;
3. Kugou chart capture and chart-level dedupe;
4. inventory rebuild and historical dedupe;
5. Claude Code download of the bounded queue;
6. Dynamic CNB input materialization from the newly downloaded queue;
7. CNB campaign submission, wait, and quality verification;
8. canonical import, tag enrichment, and source-link completeness gate;
9. snapshot creation and verification;
10. peer dry-run, schema/plugin compatibility check, and SSH fan-out;
11. only after all enabled peers succeed, local audio purge, campaign branch/temporary asset cleanup, and final CNB object-byte verification.

Use the executable `music-kb weekly-run` entry point. Never bypass the run
state or call the old full-database downloader for a weekly update.

The storage policy is `plugins/music-kb/references/cnb-storage-policy.json`. Every production
`--publish` run must include `--confirm-delete-audio` and
`--confirm-delete-cnb-storage`; preflight fails before download if either is
missing. Actual deletion still occurs only after the release and every enabled
peer have succeeded. The CNB cleanup atom preserves the
runtime image, result branches, canonical delivery, ledger, master database,
and immutable releases. It deletes only campaign input branches and temporary
run assets, then checks both CNB storage counters.

Use LFS only when `cnb_storage_lifecycle.py inspect --transport lfs` proves
the object-storage policy clean. When CNB's authoritative counter still shows
orphan LFS after every visible campaign branch and temporary asset has been
cleaned, do not call it reclaimed: CNB's documented API exposes LFS download
only, not object deletion or garbage collection. Record that fact in the atom
receipt, then use the explicit `--cnb-transport git-objects` fallback. It is
limited by the policy and campaign gate to 5 GB total and 256 MiB per file and
must still prove runtime presence, no stale campaign branches/assets, and at
least 10 GB ordinary Git headroom. Pass the same transport to the final CNB
cleanup atom; do not let a successful Git-object weekly run be falsely failed
because orphan LFS did not disappear.
