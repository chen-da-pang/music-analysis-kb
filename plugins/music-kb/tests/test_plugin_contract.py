from __future__ import annotations

import json
from pathlib import Path


def test_plugin_manifest_and_mcp_config_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "music-kb"
    assert "suno" not in json.dumps(manifest, ensure_ascii=False).casefold()
    assert manifest["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["music-kb"]["command"] == "uv"
    assert (root / "skills" / "music-kb" / "SKILL.md").is_file()
