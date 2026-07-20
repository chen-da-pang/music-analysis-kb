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


def test_beginner_facing_retrieval_interaction_contract_is_declared() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skill = (root / "skills" / "music-kb" / "SKILL.md").read_text(encoding="utf-8")

    # Keep the onboarding surface aligned with the behavior contract instead
    # of silently regressing to CLI/MCP jargon in the plugin card.
    prompts = manifest["interface"]["defaultPrompt"]
    assert any("标签" in prompt and "找" in prompt for prompt in prompts)
    assert any("试听" in prompt for prompt in prompts)
    assert "User-facing interaction contract" in skill
    for phrase in (
        "music_kb_status",
        "music_kb_tag_facets",
        "全文命中/近似检索",
        "recording_id",
        "listen_url",
        "search_projection_state",
        "MCP `count` is the number returned",
    ):
        assert phrase in skill
    assert "canonical tag name" in skill


def test_plugin_version_is_kept_in_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (root / "uv.lock").read_text(encoding="utf-8")
    version = manifest["version"]
    assert version == "0.7.1"
    assert f'version = "{version}"' in pyproject
    assert f'name = "music-kb"\nversion = "{version}"' in lockfile
