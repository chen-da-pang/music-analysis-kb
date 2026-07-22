from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


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
        "Make follow-up actions learnable in the answer",
        "你可以这样继续",
        "保持这个方向",
        "保留已展示的歌",
        "替换当前展示",
        "之前的结果仍留在对话记录里",
        "When the current direction has too few valid results",
        "all remaining unshown results",
        "one minimal, neutral question",
        "Offer selected complete descriptions after candidates",
        "visible sequence number",
        "description dimension is optional",
        "Deliver complete descriptions in readable batches",
        "at most **four songs per batch**",
        "Do not prefetch canonical analyses",
        "Keep canonical descriptions faithful to the user's language",
        "complete and faithful Chinese rendering",
        "raw_text_truncated",
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
    assert payload["version"] == "0.2.0"
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
    assert {check["id"] for check in result["checks"]} == {
        "music-kb-ux-branching",
        "music-kb-ux-recovery",
        "music-kb-ux-progressive-results",
        "music-kb-ux-followup-direction",
        "music-kb-ux-followup-guidance",
        "music-kb-ux-insufficient-results",
        "music-kb-ux-detail-selection",
        "music-kb-ux-detail-batching",
        "music-kb-ux-detail-language",
        "music-kb-ux-evidence",
        "music-kb-ux-deferred-decisions",
        "music-kb-ux-onboarding",
    }
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


def test_conversation_ux_metric_pack_catches_missing_followup_guidance(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills" / "music-kb").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"interface": {"defaultPrompt": []}}), encoding="utf-8"
    )
    (plugin / "skills" / "music-kb" / "SKILL.md").write_text(
        "Follow-up requests keep the selected direction: 再来一些 换一批 "
        "current selected direction currently displayed batch Neither phrase creates a new interpretation branch\n",
        encoding="utf-8",
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
    guidance = next(check for check in result["checks"] if check["id"] == "music-kb-ux-followup-guidance")
    assert guidance["status"] == "fail"


@pytest.mark.parametrize(
    ("removed_phrase", "check_id"),
    [
        ("all remaining unshown results", "music-kb-ux-insufficient-results"),
        ("description dimension is optional", "music-kb-ux-detail-selection"),
        ("at most **four songs per batch**", "music-kb-ux-detail-batching"),
        ("complete and faithful Chinese rendering", "music-kb-ux-detail-language"),
    ],
)
def test_conversation_ux_metric_pack_catches_missing_new_contract(
    tmp_path: Path, removed_phrase: str, check_id: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    plugin = tmp_path / "plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills" / "music-kb").mkdir(parents=True)
    manifest = (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    skill = (root / "skills" / "music-kb" / "SKILL.md").read_text(encoding="utf-8")
    assert removed_phrase in skill
    (plugin / ".codex-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    (plugin / "skills" / "music-kb" / "SKILL.md").write_text(
        skill.replace(removed_phrase, "removed contract phrase", 1),
        encoding="utf-8",
    )
    emitter = root / "evals" / "conversation-ux" / "emit-conversation-ux.py"
    completed = subprocess.run(
        ["python3", str(emitter), str(plugin), "plugin"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    check = next(item for item in result["checks"] if item["id"] == check_id)
    assert check["status"] == "fail"


def test_plugin_version_is_kept_in_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (root / "uv.lock").read_text(encoding="utf-8")
    version = manifest["version"]
    assert version == "0.7.4"
    assert f'version = "{version}"' in pyproject
    assert f'name = "music-kb"\nversion = "{version}"' in lockfile
