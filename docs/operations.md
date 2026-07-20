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
  --chart-size 100 \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --delivery /secure/path/canonical-delivery.jsonl \
  --chart-database /path/to/music-workspace/data/music_trends.sqlite \
  --peers-file "$HOME/.config/music-kb/peers.toml" \
  --output-dir "$HOME/.music-kb/releases" \
  --release-name music-kb-2026w30 \
  --download-dry-run
```

Remove `--download-dry-run` only for the approved Claude Code download. Add
`--publish` after the peer dry-run has been reviewed. Every production publish
must also include `--confirm-delete-audio --confirm-delete-cnb-storage`; the
preflight refuses to download when either cleanup confirmation is absent.
On a real publish, the verified release is atomically installed into the
publisher's `~/.music-kb/current.sqlite` before peer publication. Use
`--local-snapshot-dir` to override the target. `--no-install-local` is rejected
with `--publish`; this invariant cannot be bypassed by a normal production
run. Dry-runs do not switch the local snapshot unless `--install-local` is
supplied.
Deletion waits until the local release and every enabled peer have succeeded,
unless `--skip-peers` was explicitly selected. CNB cleanup is not considered
object-reclaimed merely because a branch was deleted: `cnb charge
get-repos-volume` must show the repository below the versioned clean threshold.
If all visible refs/assets are gone but the counter remains high, the receipt
must say `server_gc_pending=true`; CNB support documents a default seven-day
server-side GC window in `cnb/feedback#4551`, and there is no manual LFS-GC API
in the public surface. The orchestrator must wait/recheck rather than delete
the runtime image or repository. It may use the explicitly bounded
Git-object route only when that route's own storage gate passes. The
orchestrator rejects a missing or incomplete source-link set and never treats a
dry-run or skipped CNB analysis stage as a completed analysis.

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
