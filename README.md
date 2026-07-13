# Music Analysis Knowledge Base

Private, local-first knowledge base for **Music Flamingo** analysis results.
It is designed for a publisher machine to maintain a canonical SQLite library,
then distribute verified, read-only snapshots to colleagues over SSH/rsync.
Their agents query the local snapshot through MCP; no cloud database or object
storage is required.

> **Data boundary:** this repository contains software and synthetic fixtures
> only. Do not commit production music analyses, lyrics, audio, SQLite files,
> SSH keys, or Feigua data.

## Architecture

```text
publisher: music-master.sqlite (only writable copy)
        │
        │ music-kb snapshot create
        ▼
music-kb-YYYYwNN.sqlite + manifest.json + SHA-256
        │
        │ rsync / SSH, then verify + atomic local switch
        ▼
colleague: ~/.music-kb/current.sqlite (read-only)
        │
        ▼
  music-kb-mcp → Codex / other MCP-capable agents
```

The database holds one public canonical analysis per **recording**, not merely
per title. Historical/replaced revisions remain in the publisher master for
audit but are removed from client snapshots and never appear in MCP search.

## Components

| Component | Role |
| --- | --- |
| `music-kb` CLI | Publisher lifecycle: initialize, import, validate, create/verify/install snapshots, and local search. |
| `music-kb-mcp` | Bounded, read-only local MCP interface for agents. |
| `plugins/music-kb/skills/music-kb` | Retrieval and Suno-safe prompting workflow. |
| Codex plugin | Packaging layer that ships the CLI/MCP/Skill together. It does not contain the database. |

## Quick start (publisher)

```bash
cd plugins/music-kb
uv sync --all-groups

# Create the only writable master database.
uv run music-kb init --db "$HOME/.music-kb/music-master.sqlite"

# Import approved Music Flamingo output using the documented JSON/JSONL contract.
uv run music-kb import-analysis \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input tests/fixtures/analysis.json

uv run music-kb validate --db "$HOME/.music-kb/music-master.sqlite"
uv run music-kb snapshot create \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --output-dir "$HOME/.music-kb/releases" \
  --name music-kb-2026w29
```

## Quick start (colleague)

1. Install the plugin/runtime from this private repository.
2. Receive a release folder via `rsync` (never a live master database).
3. Verify and atomically install it:

   ```bash
   cd plugins/music-kb
   uv run music-kb snapshot verify --manifest /path/to/release/manifest.json
   uv run music-kb snapshot install \
     --release-dir /path/to/release --target-dir "$HOME/.music-kb"
   ```

4. The default MCP path is `~/.music-kb/current.sqlite`; set `MUSIC_KB_DB` to
   a different local read-only snapshot only when necessary.

## Read paths

```bash
cd plugins/music-kb
uv run music-kb --json doctor
uv run music-kb search --tag "granular vocal chop" --limit 10
uv run music-kb get rec_example
```

MCP exposes only read tools: status, search, resolve title/artist, canonical
analysis retrieval, tag facets, and a Suno-safe style compiler.

## Documentation

- [Import contract](docs/import-contract.md)
- [Schema and retrieval design](docs/schema.md)
- [Snapshot publishing and SSH operations](docs/operations.md)
- [Architecture decision](docs/architecture/2026-07-13-music-analysis-knowledge-base.md)

## Development

```bash
cd plugins/music-kb
uv sync --all-groups
uv run pytest
python3 /Users/wycm/.codex/skills/plugin-creator/scripts/validate_plugin.py .
```

Issue tracking and pull requests are required for all changes. See
[issue #1](https://github.com/chen-da-pang/music-analysis-kb/issues/1) for the
initial implementation log.
