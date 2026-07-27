# Grok Build plugin packaging

This repository dual-packages the `music-kb` plugin for **Grok Build** and
**Codex**.

## Layout

| Path | Role |
| --- | --- |
| `.grok-plugin/marketplace.json` | Grok marketplace index (lists `music-kb`) |
| `.grok-plugin/plugin-index.json` | Optional catalog for Marketplace UI components |
| `plugins/music-kb/.grok-plugin/plugin.json` | Grok plugin manifest |
| `plugins/music-kb/.codex-plugin/plugin.json` | Codex plugin manifest (kept in sync) |
| `.agents/plugins/marketplace.json` | Codex/agents marketplace index |
| `plugins/music-kb/skills/*` | Shared skills (Grok + Codex) |
| `plugins/music-kb/.mcp.json` | MCP server launch config (`uv run music-kb-mcp`) |

Skills and MCP are shared. Only the packaging manifests differ by harness.

## Install (publisher or colleague machine)

From a local checkout of this branch/repo:

```bash
grok plugin marketplace add /absolute/path/to/music-analysis-kb
grok plugin install music-kb --trust
grok plugin enable music-kb   # if still disabled
```

Or add a durable source in `~/.grok/config.toml`:

```toml
[[marketplace.sources]]
name = "music-analysis-kb"
path = "/absolute/path/to/music-analysis-kb"

[plugins]
enabled = ["music-kb"]
```

Validate packaging before publishing:

```bash
grok plugin validate plugins/music-kb
```

## Skills

| Skill | Audience | Purpose |
| --- | --- | --- |
| `music-kb` | Everyone with a local snapshot | Natural-language retrieval + lyrics |
| `music-kb-audio-downloader` | Publisher only | Inventory, primary download, fallback |
| `music-kb-weekly-orchestrator` | Publisher only | Full weekly pipeline |
| `music-kb-weekly-publisher` | Publisher only | Import, snapshot, SSH fan-out |

Publisher skills default to **direct** executors. Script names such as
`run_claude_download.py` are historical atom entry points and do not require
Claude.

## MCP

After trust + enable, Grok attaches `music-kb` from `.mcp.json`:

```json
{
  "mcpServers": {
    "music-kb": {
      "cwd": ".",
      "command": "uv",
      "args": ["run", "--project", ".", "music-kb-mcp"]
    }
  }
}
```

The MCP process is started with `cwd` at the installed plugin root
(`plugins/music-kb`). Ensure `uv sync` has been run there (or the install
path has a working environment) before relying on MCP tools.

## Version

Plugin version **0.8.6** introduces dual Grok/Codex packaging. Runtime
behavior of CLI/MCP remains the same as 0.8.5 unless otherwise noted in the
changelog.
