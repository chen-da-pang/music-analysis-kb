# Publishing, sync, and rollback

## Publisher push (recommended)

The publisher pushes a **previously created immutable release** to each peer.
The peer's Agent and MCP server still read only its local
`~/.music-kb/current.sqlite`; they never query the publisher, a NAS, or cloud
storage at runtime.

### One-time peer prerequisites

For every colleague computer:

1. Install the private Codex plugin and then make the CLI available on `PATH`:

   ```bash
   cd /path/to/the/installed/music-kb/plugin
   ./scripts/install-local.sh
   music-kb --help
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
SSH identity, target directory, and the absolute/home-relative CLI path; it
never contains a database or raw model output. Set `enabled = false` to keep a
peer in the inventory without including it in an all-peer publish. An explicit
`--peer <name>` retry still targets that named peer, even when it is disabled.
The default `cli_path` is
`~/.local/bin/music-kb`, the normal destination of `install-local.sh`. Set it
explicitly if the colleague uses `UV_TOOL_BIN_DIR` or a wrapper location.

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

For each peer, the command performs five bounded stages:

1. preflights the configured absolute CLI path over non-interactive SSH;
2. creates `~/.music-kb/incoming/<release-name>/`;
3. runs `rsync -a --partial --checksum` into that incoming directory;
4. remotely runs `music-kb snapshot verify` against `manifest.json`;
5. remotely runs `music-kb snapshot install`, which atomically changes only
   `~/.music-kb/current.sqlite` after a successful verification.

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

## Schema v5 publisher upgrade

Before using the 100k generic-import path against a pre-existing schema-v4
master, run the one-time local migration on the publisher machine:

```bash
music-kb init --db "$HOME/.music-kb/music-master.sqlite"
music-kb validate --db "$HOME/.music-kb/music-master.sqlite"
```

This installs the canonical-switch and exact-tag indexes and records the FTS
projection state. It never rewrites Music Flamingo raw text or uploads data.
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
