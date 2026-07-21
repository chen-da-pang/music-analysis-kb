#!/usr/bin/env python3
"""Emit deterministic checks for the approved Music KB conversation contract."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _check(
    check_id: str,
    ok: bool,
    message: str,
    *,
    evidence: list[str],
    remediation: list[str],
) -> dict[str, Any]:
    return {
        "id": check_id,
        "category": "conversation-ux",
        "severity": "info" if ok else "error",
        "status": "pass" if ok else "fail",
        "message": message,
        "evidence": evidence,
        "remediation": remediation,
    }


def _locate(target: Path, target_kind: str) -> tuple[Path | None, Path | None]:
    """Return (skill_file, plugin_manifest) for plugin-eval's target shapes."""

    target = target.resolve()
    if target.is_file():
        if target.name == "SKILL.md":
            return target, None
        if target.name == "plugin.json" and target.parent.name == ".codex-plugin":
            root = target.parent.parent
            return root / "skills" / "music-kb" / "SKILL.md", target

    if target.is_dir():
        if (target / ".codex-plugin" / "plugin.json").is_file():
            return target / "skills" / "music-kb" / "SKILL.md", target / ".codex-plugin" / "plugin.json"
        if (target / "SKILL.md").is_file():
            return target / "SKILL.md", None

    return None, None


def _read(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _manifest(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PLUGIN_EVAL_TARGET", "."))
    target_kind = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("PLUGIN_EVAL_TARGET_KIND", "unknown")
    skill_path, manifest_path = _locate(target, target_kind)
    skill = _read(skill_path)
    normalized_skill = re.sub(r"\s+", " ", skill)
    manifest = _manifest(manifest_path)
    checks: list[dict[str, Any]] = []

    branching_phrases = (
        "## Conversation UX contract",
        "Broad subjective requests use real-result branches",
        "tag co-occurrence",
        "at most **three**",
        "most likely interpretation",
        "one short reason",
        "Keep a song in every branch",
    )
    branching_ok = bool(skill) and all(phrase in normalized_skill for phrase in branching_phrases)
    checks.append(
        _check(
            "music-kb-ux-branching",
            branching_ok,
            "Natural-language requests have bounded, transparent branch retrieval.",
            evidence=[phrase for phrase in branching_phrases if phrase in normalized_skill],
            remediation=["Restore the approved branch, ordering, and cross-branch disclosure rules."],
        )
    )

    recovery_phrases = (
        "When the user selects a branch",
        "Do **not** silently run another",
        "不是这个",
        "one minimal question",
        "small question",
    )
    recovery_ok = bool(skill) and all(phrase in normalized_skill for phrase in recovery_phrases)
    checks.append(
        _check(
            "music-kb-ux-recovery",
            recovery_ok,
            "Branch selection and correction preserve user control without hidden retrieval.",
            evidence=[phrase for phrase in recovery_phrases if phrase in normalized_skill],
            remediation=["Keep branch selection as context and use one minimal repair question."],
        )
    )

    progressive_phrases = (
        "Progressive result volume (方案 1+)",
        "small result set",
        "representative candidates",
        "grouped, batched presentation",
        "exact first-page number is intentionally still a calibration parameter",
    )
    progressive_ok = bool(skill) and all(phrase in normalized_skill for phrase in progressive_phrases)
    checks.append(
        _check(
            "music-kb-ux-progressive-results",
            progressive_ok,
            "Small and large result sets use the approved progressive presentation policy.",
            evidence=[phrase for phrase in progressive_phrases if phrase in normalized_skill],
            remediation=["Restore the small-set, representative-page, and batched-result rules."],
        )
    )

    evidence_phrases = (
        "ordered for retrieval",
        "small shortlist",
        "listen_url",
        "search_projection_state",
        "Do not infer mood",
    )
    evidence_ok = bool(skill) and all(phrase in normalized_skill for phrase in evidence_phrases)
    checks.append(
        _check(
            "music-kb-ux-evidence",
            evidence_ok,
            "Candidate claims are evidence-backed and preserve the read-only link contract.",
            evidence=[phrase for phrase in evidence_phrases if phrase in normalized_skill],
            remediation=["Keep bounded canonical verification, status gating, and runtime listen URLs."],
        )
    )

    deferred_phrases = (
        "append/replace meanings",
        "再来一些",
        "换一批",
        "deliberately deferred product decisions",
        "universal intersection or union rule",
    )
    fixed_default = bool(
        re.search(
            r"(?:一些|几首)[^。.!?\n]{0,40}(?:默认|means|等于|=)\s*(?:3|5|10)(?!\d)",
            normalized_skill,
            flags=re.IGNORECASE,
        )
        or re.search(r"(?:up to|default)\s+10\s+results?", normalized_skill, flags=re.IGNORECASE)
    )
    deferred_ok = bool(skill) and all(phrase in normalized_skill for phrase in deferred_phrases) and not fixed_default
    checks.append(
        _check(
            "music-kb-ux-deferred-decisions",
            deferred_ok,
            "Undecided follow-up semantics remain explicitly deferred; no withdrawn fixed quantity returned.",
            evidence=[phrase for phrase in deferred_phrases if phrase in normalized_skill]
            + (["fixed default detected"] if fixed_default else ["no fixed quantity pattern detected"]),
            remediation=["Do not encode append/replace or multi-tag defaults before a later product decision."],
        )
    )

    prompts = manifest.get("interface", {}).get("defaultPrompt", []) if manifest else []
    prompts_text = " ".join(str(item) for item in prompts)
    onboarding_ok = (
        target_kind == "skill"
        or (
            bool(manifest)
            and "R&B" in prompts_text
            and "温暖" in prompts_text
            and "氛围感" in prompts_text
            and "试听链接" in prompts_text
        )
    )
    checks.append(
        _check(
            "music-kb-ux-onboarding",
            onboarding_ok,
            "The plugin card demonstrates ordinary-language discovery requests.",
            evidence=["natural-language default prompts present"] if onboarding_ok else ["onboarding prompts missing"],
            remediation=["Use ordinary-language examples that expose the conversation flow."],
        )
    )

    passed = sum(1 for check in checks if check["status"] == "pass")
    total = len(checks)
    score = round((passed / total) * 100, 2) if total else 0
    band = "good" if score == 100 else "needs-work" if score >= 80 else "poor"
    payload = {
        "checks": checks,
        "metrics": [
            {
                "id": "music-kb-conversation-ux-coverage",
                "category": "conversation-ux",
                "value": score,
                "unit": "percent",
                "band": band,
            },
            {
                "id": "music-kb-conversation-ux-failed-checks",
                "category": "conversation-ux",
                "value": total - passed,
                "unit": "checks",
                "band": "good" if passed == total else "needs-work",
            },
        ],
        "artifacts": [],
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
