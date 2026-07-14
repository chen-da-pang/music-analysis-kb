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

Read the repository-level [README](../../README.md) for the publisher/client
workflow and deployment rules.
