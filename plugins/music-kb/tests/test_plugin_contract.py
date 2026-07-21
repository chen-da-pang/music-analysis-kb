from __future__ import annotations

import json
import subprocess
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


def test_conversation_ux_onboarding_and_contract_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skill = (root / "skills" / "music-kb" / "SKILL.md").read_text(encoding="utf-8")

    prompts = manifest["interface"]["defaultPrompt"]
    assert any("R&B" in prompt and "温暖" in prompt for prompt in prompts)
    assert any("氛围感" in prompt and "方向" in prompt for prompt in prompts)
    assert any("试听链接" in prompt for prompt in prompts)

    for phrase in (
        "## Conversation UX contract",
        "Broad subjective requests use real-result branches",
        "tag co-occurrence",
        "at most **three**",
        "most likely interpretation",
        "numeric confidence",
        "Keep a song in every branch",
        "Progressive result volume (方案 1+)",
        "Follow-up requests keep the selected direction",
        "不是这个",
        "再来一些",
        "换一批",
        "currently displayed batch",
        "Neither phrase creates a new interpretation branch",
        "listen_url",
        "search_projection_state",
        "ordered for retrieval",
        "small shortlist",
    ):
        assert phrase in skill

    # The quantity is deliberately still a calibration parameter; the
    # withdrawn contract's permanent 10-result default must not return.
    assert "一些/几首 = N" in skill
    assert "一些/几首 means up to 10" not in skill


def test_conversation_ux_metric_pack_is_shipped() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "evals" / "conversation-ux" / "manifest.json"
    emitter = root / "evals" / "conversation-ux" / "emit-conversation-ux.py"
    assert manifest.is_file()
    assert emitter.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["supportedTargetKinds"] == ["skill", "plugin"]
    assert payload["command"] == ["python3", "./emit-conversation-ux.py"]


def test_conversation_ux_metric_pack_passes_the_plugin() -> None:
    root = Path(__file__).resolve().parents[1]
    emitter = root / "evals" / "conversation-ux" / "emit-conversation-ux.py"
    completed = subprocess.run(
        ["python3", str(emitter), str(root), "plugin"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert all(check["status"] == "pass" for check in result["checks"])
    assert result["metrics"][0]["id"] == "music-kb-conversation-ux-coverage"
    assert result["metrics"][0]["value"] == 100.0


def test_conversation_ux_metric_pack_catches_withdrawn_fixed_quantity(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills" / "music-kb").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"interface": {"defaultPrompt": []}}), encoding="utf-8"
    )
    (plugin / "skills" / "music-kb" / "SKILL.md").write_text(
        "一些/几首默认 10\n", encoding="utf-8"
    )
    root = Path(__file__).resolve().parents[1]
    emitter = root / "evals" / "conversation-ux" / "emit-conversation-ux.py"
    completed = subprocess.run(
        ["python3", str(emitter), str(plugin), "plugin"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    deferred = next(check for check in result["checks"] if check["id"] == "music-kb-ux-deferred-decisions")
    assert deferred["status"] == "fail"


def test_plugin_version_is_kept_in_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (root / "uv.lock").read_text(encoding="utf-8")
    version = manifest["version"]
    assert version == "0.7.2"
    assert f'version = "{version}"' in pyproject
    assert f'name = "music-kb"\nversion = "{version}"' in lockfile
