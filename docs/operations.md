# Publishing, sync, and rollback

## Publisher push (recommended)

The publisher pushes a **previously created immutable release** to each peer.
The peer's Agent and MCP server still read only its local
`~/.music-kb/current.sqlite`; they never query the publisher, a NAS, or cloud
storage at runtime.

### One-time peer prerequisites

For every colleague computer:

1. Install the Codex plugin yourself. The SSH publisher does not install or
   modify the plugin and does not require a globally registered `music-kb`
   executable on the peer:

   ```bash
   codex plugin marketplace add chen-da-pang/music-analysis-kb --ref main
   codex plugin add music-kb@music-analysis-kb
   ```

2. Enable macOS **Remote Login**, keep the machine reachable on the company
   network/VPN, and add the publisher's public key to the colleague account.
3. Verify the SSH host key before automated publishing. The publisher command
   deliberately uses `BatchMode=yes` and `StrictHostKeyChecking=yes`; it will
   fail rather than accept an unknown or changed host key.

On the publisher, copy the tracked example into a private location and replace
the placeholders. The real inventory, hostnames, usernames, and key paths must
not be committed:

```bash
mkdir -p "$HOME/.config/music-kb"
cp config/peers.example.toml "$HOME/.config/music-kb/peers.toml"
chmod 600 "$HOME/.config/music-kb/peers.toml"
```

The peer file has a `[[peers]]` entry per colleague. It controls only a peer's
SSH identity, target directory, and remote Python executable; it never contains
a database or raw model output. Set `enabled = false` to keep a peer in the
inventory without including it in an all-peer publish. An explicit
`--peer <name>` retry still targets that named peer, even when it is disabled.
The default `python_path` is `python3`; set an absolute or home-relative path
when a colleague uses a nonstandard Python installation.

### Weekly release flow

1. Import only passed Music Flamingo results into the **local master**.
2. Run `music-kb validate`.
3. Build a release with `music-kb snapshot create`.
4. Inspect the fan-out plan without connecting to anyone:

   ```bash
   cd plugins/music-kb
   uv run music-kb --json publish push \
     --release-dir "$HOME/.music-kb/releases/music-kb-2026w29" \
     --peers-file "$HOME/.config/music-kb/peers.toml" \
     --dry-run
   ```

5. Publish the exact same release:

   ```bash
   uv run music-kb --json publish push \
     --release-dir "$HOME/.music-kb/releases/music-kb-2026w29" \
     --peers-file "$HOME/.config/music-kb/peers.toml"
   ```

### Complete weekly-run orchestration

For a recurring update, use the publisher-only orchestrator so the capture,
dedupe, Claude Code download, CNB handoff, import, link gate, snapshot,
publisher-local current-snapshot install, peer plan, and cleanup receipts share
one run id:

```bash
uv run music-kb --json weekly-run \
  --workspace /path/to/music-workspace \
  --run-id kugou-2026w30 \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --chart-database /path/to/music-workspace/data/music_trends.sqlite \
  --peers-file "$HOME/.config/music-kb/peers.toml" \
  --output-dir "$HOME/.music-kb/releases" \
  --release-name music-kb-2026w30 \
  --cnb-transport lfs \
  --download-dry-run
```

This fresh path captures the full configured chart profile, dedupes against the
inventory, asks Claude Code to download only the queue, materializes a new
campaign staging directory, and uses one disposable repository named
`<campaign_repository_prefix><run-id>`. Remove `--download-dry-run` only for the
approved Claude Code download. `--cnb-campaign-dry-run` exercises the pinned
GitHub runner export, manifest checks, and generated CNB config without creating
or pushing a CNB repository or starting a build. For local validation of an
unpublished branch, the low-level adapter accepts `--allow-unpublished` only
with a non-executing `prepare` dry-run; it cannot be combined with `--execute`
and is never a production bypass. `--cnb-command` is an explicit
legacy fallback and is the only path that uses the protected-repository storage
gate.

Add `--publish` after the peer dry-run has been reviewed. Every production
publish must also include `--confirm-delete-audio`,
`--confirm-delete-cnb-storage`, and `--confirm-delete-cnb-repositories`; the
preflight refuses to download when any cleanup confirmation is absent. The
repository flag is a separate irreversible authorization for the exact
receipt-bound disposable campaign repository.
On a real publish, the verified release is atomically installed into the
publisher's `~/.music-kb/current.sqlite` before peer publication. Use
`--local-snapshot-dir` to override the target. `--no-install-local` is rejected
with `--publish`; this invariant cannot be bypassed by a normal production
run. Dry-runs do not switch the local snapshot unless `--install-local` is
supplied.
Deletion waits until the canonical delivery has been imported, the local
release is verified, and every enabled peer has succeeded, unless `--skip-peers`
was explicitly selected. The disposable campaign cleanup then deletes only the
receipt-bound campaign repository, after checking for running workspaces and
verifying repository 404/zero volume, organization usage decrease when bytes
existed, and protected runtime/main preservation. Any failed shard, release
gate, peer gate, receipt check, or deletion verification leaves the same
repository and receipt for recovery. A dry-run or blocked cleanup is recorded in
that receipt and does not delete anything.

The final storage atom handles only visible legacy refs/assets. It is not a
manual orphan-LFS GC: CNB documents a default seven-day server-side window in
`cnb/feedback#4551`, and the public surface has no manual LFS-GC API. If the
protected repository's counter remains high after visible cleanup, record
`server_gc_pending=true`; never delete the protected runtime image or `main`.
The explicitly bounded `git-objects` route is allowed only when its own total,
per-file, and Git-headroom gates pass. The orchestrator rejects incomplete
source links and never treats a dry-run or skipped analysis as a completed
analysis.

When quota blocks a weekly invocation before analysis, the independent
destructive cleanup atom can remove completed allowlisted repositories without
waiting for the post-publication stage:

```bash
python plugins/music-kb/scripts/cnb_storage_lifecycle.py \
  delete-disposable-repositories \
  --policy plugins/music-kb/references/cnb-storage-policy.json \
  --confirm-delete-cnb-repositories
```

Its exit status is zero only after every target is 404/zero-volume and the
protected Music Flamingo repository/main/runtime has been reverified.

When `--rank-id` is omitted, `weekly-run` reads the versioned
`plugins/music-kb/references/kugou-chart-profile.json` and captures all six
configured Kugou charts page by page until an empty or short page. The default
profile is the validated six-chart contract (1136 historical raw rows and 927
unique songs; live counts may drift). Passing `--rank-id` intentionally selects
the bounded single-page mode for a targeted chart check.

For each peer, the command performs six bounded stages:

1. preflights the configured Python executable over non-interactive SSH;
2. checks the remote music-kb plugin cache for a compatible plugin version and
   schema before transferring any database;
3. creates `~/.music-kb/incoming/<release-name>/`;
4. runs `rsync -a --partial --checksum` into that incoming directory;
5. remotely runs an embedded `hashlib`/`sqlite3` verification against
   `manifest.json`;
6. remotely copies the verified release into the peer's release directory and
   atomically changes only `~/.music-kb/current.sqlite`.

It never syncs `music-master.sqlite`, never uses `rsync --inplace`, and does
not stop the other peers if one peer is offline. Retry one failed colleague
after it is reachable with:

```bash
uv run music-kb --json publish push \
  --release-dir "$HOME/.music-kb/releases/music-kb-2026w29" \
  --peers-file "$HOME/.config/music-kb/peers.toml" \
  --peer first-colleague
```

The JSON result returns `succeeded_count`, `failed_count`, a per-peer stage
list, and bounded command output excerpts. A partial peer failure exits with
status `1`, so weekly automation can detect it without hiding the successful
peer installs.

Never grant client agents write access to a snapshot.

## Schema v6 publisher upgrade

Before using the 100k generic-import path against a pre-existing schema-v4
master, run the one-time local migration on the publisher machine:

```bash
music-kb init --db "$HOME/.music-kb/music-master.sqlite"
music-kb validate --db "$HOME/.music-kb/music-master.sqlite"
```

This installs the v5 canonical-switch/exact-tag indexes and the v6
`source_track.source_url` field, then records the FTS projection state. It
never rewrites Music Flamingo raw text or uploads data.
Create and distribute a fresh snapshot after the migration; do not try to
initialize a client snapshot directly.

## Manual client update (fallback)

The supplied `plugins/music-kb/scripts/pull-release.sh` stages an rsync release
in a temporary directory, verifies SHA-256 via the CLI, and atomically changes
`~/.music-kb/current.sqlite` only after verification succeeds.

`music-kb-mcp` opens the database read-only for each request, so its next
query automatically uses the switched snapshot.

## Rollback

The publisher push keeps the verified incoming release folder at
`~/.music-kb/incoming/<release-name>/`. To roll back a client, run the same
`music-kb snapshot install` operation against an earlier verified incoming
folder. The operation changes the `current.sqlite` symlink atomically; it
never mutates either release database.

## File permissions

- publisher master: owner-only (`0600`) where practical;
- release database and manifest: read-only (`0444`) after creation;
- client directory: owned by the colleague, but agents use SQLite URI
  `mode=ro` and `PRAGMA query_only=ON` regardless of filesystem permissions;
- SSH key: restricted sync account, no shell access when infrastructure permits;
  real peer configuration stays in `~/.config/music-kb/peers.toml` on the
  publisher, never in Git.
