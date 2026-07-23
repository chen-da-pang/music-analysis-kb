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
| `music-kb` CLI | Publisher lifecycle: initialize, import, validate, create/verify/install snapshots, SSH/rsync fan-out, and local search. |
| `music-kb-mcp` | Bounded, read-only local MCP interface for agents. |
| `plugins/music-kb/skills/music-kb` | Retrieval workflow for canonical analyses and granular tags. |
| `plugins/music-kb/skills/music-kb-audio-downloader` | Publisher-only upstream queue, inventory, and Claude Code/musicdl download workflow. |
| Codex plugin | Packaging layer that ships the CLI/MCP/Skill together. It does not contain the database. |

The retrieval Skill accepts ordinary-language requests, so a user can ask for
“一些 R&B、温暖的、关于爱情的歌” without learning canonical tag names or MCP
syntax. Clear requests are searched first and explained briefly; broad subjective
requests can be split into at most three evidence-backed directions for the user
to compare. Large result sets begin with a representative page and can be
expanded, while small sets may be shown in full. The Skill preserves runtime
listening URLs and does not silently invent a default quantity.

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

# For 100k-scale generic updates, use a physical-LF .jsonl/.ndjson file.
# It streams in bounded batches and rebuilds FTS only once at the end.
uv run music-kb import-analysis \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/new-analyses.jsonl \
  --batch-size 500

# The CNB KuGou campaign uses its own strict, hash-verified JSONL adapter.
# Keep the real delivery outside this repository; the full corpus should be 927.
uv run music-kb import-campaign-delivery \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/kugou-canonical-delivery.jsonl \
  --expected-count 927

# Derive versioned, fine-grained Music Flamingo tags from the canonical prose.
# Run a coverage check first; the write command is publisher-only and idempotent.
uv run music-kb enrich-campaign-tags --db "$HOME/.music-kb/music-master.sqlite" --dry-run
uv run music-kb enrich-campaign-tags --db "$HOME/.music-kb/music-master.sqlite" --batch-size 500

uv run music-kb validate --db "$HOME/.music-kb/music-master.sqlite"
uv run music-kb snapshot create \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --output-dir "$HOME/.music-kb/releases" \
  --name music-kb-2026w29

# A real weekly publish atomically switches the publisher-local current.sqlite
# after release verification; use snapshot install for a manual fallback.

# Peer details remain in a private local TOML file, never in this repository.
uv run music-kb --json publish push \
  --release-dir "$HOME/.music-kb/releases/music-kb-2026w29" \
  --peers-file "$HOME/.config/music-kb/peers.toml" \
  --dry-run
```

## Quick start (colleague)

1. Install the public plugin from GitHub (one time):

   ```bash
   codex plugin marketplace add chen-da-pang/music-analysis-kb --ref main
   codex plugin add music-kb@music-analysis-kb
   ```

   For a checked-out local copy during development, use its absolute repository
   path in the first command instead. If its MCP tools do not appear in an
   already-open Codex task, reopen that task so its tool metadata is refreshed.

2. Enable macOS Remote Login and keep the machine reachable on the company
   network/VPN. The publisher's SSH installer uses the configured remote
   Python executable and does not require a global `music-kb` CLI on this Mac.

3. Receive a release folder via publisher-managed SSH/rsync (never a live
   master database). The publisher verifies it remotely and atomically installs
   it as `~/.music-kb/current.sqlite`.
4. For a manual fallback, verify and atomically install it:

   ```bash
   cd plugins/music-kb
   uv run music-kb snapshot verify --manifest /path/to/release/manifest.json
   uv run music-kb snapshot install \
     --release-dir /path/to/release --target-dir "$HOME/.music-kb"
   ```

5. The default MCP path is `~/.music-kb/current.sqlite`; set `MUSIC_KB_DB` to
   a different local read-only snapshot only when necessary.

## Read paths

```bash
cd plugins/music-kb
uv run music-kb --json doctor
uv run music-kb search --tag "granular vocal chop" --limit 10
uv run music-kb get rec_example
uv run music-kb get-lyrics rec_example
```

The current MCP workflow uses read tools for status, search, title/artist
resolution, canonical analysis retrieval, selected-recording full lyrics, and
tag facets. Candidate search stays compact; full lyrics are returned only for
an explicitly selected recording ID.

Campaign analyses receive deterministic, versioned tags for title, artist,
section, genre, tempo/meter, rhythm, instrumentation, production/mix, harmony,
vocal style, mood, structure, and lyric/theme retrieval. The raw model output
is never rewritten. This release creates no model-output-to-generation-prompt
conversion: all of these tags exist to improve local retrieval.

## Documentation

- [Import contract](docs/import-contract.md)
- [Schema and retrieval design](docs/schema.md)
- [Snapshot publishing and SSH operations](docs/operations.md)
- [Weekly audio download through Claude Code](docs/download-via-claude-code.md)
- [Architecture decision](docs/architecture/2026-07-13-music-analysis-knowledge-base.md)
- [Synthetic 100k benchmark](docs/benchmarks/2026-07-13-generic-100k.md)

## Development

```bash
cd plugins/music-kb
uv sync --all-groups
uv run pytest
uv run python scripts/benchmark_100k.py --records 1000  # fast synthetic smoke test
python3 "${CODEX_HOME:-$HOME/.codex}/skills/plugin-creator/scripts/validate_plugin.py" .
```

Issue tracking and pull requests are required for all changes. See
[issue #1](https://github.com/chen-da-pang/music-analysis-kb/issues/1) for the
initial implementation log.
