# Publishing, sync, and rollback

## Publisher flow

1. Import only passed Music Flamingo results into the **local master**.
2. Run `music-kb validate`.
3. Build a release with `music-kb snapshot create`.
4. Send the generated release directory over SSH with a restricted sync user:

   ```bash
   rsync -a --partial --checksum \
     /srv/music-kb/releases/music-kb-2026w29/ \
     colleague@machine:/incoming/music-kb-2026w29/
   ```

5. The colleague verifies the manifest and runs `music-kb snapshot install`.

Never rsync `music-master.sqlite` while it is writable. Never grant client
agents write access to a snapshot.

## Client update

The supplied `plugins/music-kb/scripts/pull-release.sh` stages an rsync release
in a temporary directory, verifies SHA-256 via the CLI, and atomically changes
`~/.music-kb/current.sqlite` only after verification succeeds.

`music-kb-mcp` opens the database read-only for each request, so its next
query automatically uses the switched snapshot.

## Rollback

Client installations retain release files in `~/.music-kb/releases/`. To
rollback, install a previously verified release folder again. The operation
changes the `current.sqlite` symlink atomically; it never mutates either
release database.

## File permissions

- publisher master: owner-only (`0600`) where practical;
- release database and manifest: read-only (`0444`) after creation;
- client directory: owned by the colleague, but agents use SQLite URI
  `mode=ro` and `PRAGMA query_only=ON` regardless of filesystem permissions;
- SSH key: restricted sync account, no shell access when infrastructure permits.
