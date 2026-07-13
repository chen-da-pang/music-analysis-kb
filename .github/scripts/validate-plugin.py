from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "plugins" / "music-kb" / ".codex-plugin" / "plugin.json"
MCP = ROOT / "plugins" / "music-kb" / ".mcp.json"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "music-kb"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (ROOT / "plugins" / "music-kb" / "skills" / "music-kb" / "SKILL.md").is_file()
    mcp = json.loads(MCP.read_text(encoding="utf-8"))
    assert "music-kb" in mcp["mcpServers"]


if __name__ == "__main__":
    main()
