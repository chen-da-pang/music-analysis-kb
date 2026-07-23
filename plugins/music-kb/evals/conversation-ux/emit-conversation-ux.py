#!/usr/bin/env python3
"""Emit deterministic checks for the approved Music KB conversation contract."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from trace_validator import validate_trace_file


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


def _info(check_id: str, message: str, *, evidence: list[str]) -> dict[str, Any]:
    return {
        "id": check_id,
        "category": "conversation-behavior",
        "severity": "info",
        "status": "info",
        "message": message,
        "evidence": evidence,
        "remediation": [],
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
    followups = _read(
        skill_path.parent / "references" / "followups.md" if skill_path is not None else None
    )
    normalized_skill = re.sub(r"\s+", " ", f"{skill}\n{followups}")
    manifest = _manifest(manifest_path)
    checks: list[dict[str, Any]] = []

    routing_heading = "## Runtime routing — do this first"
    routing_heading_index = skill.find(routing_heading)
    safety_heading_index = skill.find("## Safety and data boundary")
    routing_phrases = (
        "If `music_kb_status` appears in the provided tool list",
        "immediately run the PATH command `music-kb --json doctor`",
        "music-kb --json discover --tag",
        "Do not call `list_mcp_resources`",
        "Do not scan plugin directories",
        "README text",
        "`.venv/bin/music-kb`",
        "Do not inspect `--help` unless",
        "Do not reread this Skill",
        "Do not repeat a successful discovery or recommendation with identical",
        "Run each branch recommendation as its own call",
        "A complete first read",
        "do not use sed, cat, or another file",
    )
    routing_ok = (
        bool(skill)
        and 0 <= routing_heading_index < safety_heading_index
        and all(phrase in skill for phrase in routing_phrases)
    )
    checks.append(
        _check(
            "music-kb-ux-runtime-routing-first",
            routing_ok,
            "The Skill exposes the direct MCP-or-PATH route before the conversation contract.",
            evidence=[
                f"routing_heading_index={routing_heading_index}",
                f"safety_heading_index={safety_heading_index}",
            ]
            + [phrase for phrase in routing_phrases if phrase in skill],
            remediation=[
                "Put the one-read MCP-or-PATH route first and explicitly forbid resource probes, implementation scans, private binaries, help probes, and duplicate retrieval."
            ],
        )
    )

    branching_phrases = (
        "## Conversation UX contract",
        "Broad subjective requests use real-result branches",
        "music_kb_discover",
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

    branch_execution_phrases = (
        "facet_scope.kind=all_matches",
        "two or more user-relevant interpretations",
        "at least two and at most **three**",
        "exactly three important directions",
        "do not silently reduce them to one or two",
        "A smaller match count alone does not make it unimportant",
        "direction ledger",
        "do not start branch calls from a partial list",
        "non-zero `hopeful`, `melancholic`, and `soul` facets",
        "separate `music_kb_recommend`",
        "A label without its own recommendation call",
        "Finish all selected calls before answering",
        "never flatten or recombine recommended branches",
        "the final answer must contain one separate group per recommendation",
    )
    branch_execution_ok = bool(skill) and all(
        phrase in normalized_skill for phrase in branch_execution_phrases
    )
    checks.append(
        _check(
            "music-kb-contract-branch-execution",
            branch_execution_ok,
            "The written contract requires evidence-backed branch completeness, separate searches, and grouped rendering.",
            evidence=[
                phrase for phrase in branch_execution_phrases if phrase in normalized_skill
            ],
            remediation=[
                "Require two or three supported directions, one recommendation per direction, and one final group per valid branch."
            ],
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
        "omit the recommendation `limit` argument",
        "show every row returned on that page",
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

    followup_phrases = (
        "Follow-up requests keep the selected direction",
        "再来一些",
        "换一批",
        "current selected direction",
        "currently displayed batch",
        "Neither phrase creates a new interpretation branch",
    )
    followup_ok = bool(skill) and all(phrase in normalized_skill for phrase in followup_phrases)
    checks.append(
        _check(
            "music-kb-ux-followup-direction",
            followup_ok,
            "Follow-up requests add or replace results without changing the selected direction.",
            evidence=[phrase for phrase in followup_phrases if phrase in normalized_skill],
            remediation=["Keep both follow-up actions in the current branch and distinguish append from display replacement."],
        )
    )

    guidance_phrases = (
        "Make the first answer learnable",
        "你可以这样继续",
        "“再来一些”",
        "保持这个方向",
        "保留已展示的歌",
        "“换一批”",
        "替换当前展示",
        "之前的结果仍留在对话记录里",
        "Keep these two affordances distinct",
    )
    guidance_ok = bool(skill) and all(phrase in normalized_skill for phrase in guidance_phrases)
    checks.append(
        _check(
            "music-kb-ux-followup-guidance",
            guidance_ok,
            "Expandable results teach the user the plain-language follow-up actions in the same answer.",
            evidence=[phrase for phrase in guidance_phrases if phrase in normalized_skill],
            remediation=[
                "Add a compact in-answer guide that distinguishes append from replacement while keeping the current direction."
            ],
        )
    )

    insufficient_phrases = (
        "When the current direction has too few valid results",
        "no universal count",
        "all remaining unshown results",
        "partially matching songs",
        "side by side",
        "existing returned evidence",
        "full conversation",
        "one minimal, neutral question",
        "set it as the current selected direction",
    )
    insufficient_ok = bool(skill) and all(
        phrase in normalized_skill for phrase in insufficient_phrases
    )
    checks.append(
        _check(
            "music-kb-ux-insufficient-results",
            insufficient_ok,
            "Insufficient directions deliver remaining valid matches before offering user-controlled fallbacks.",
            evidence=[phrase for phrase in insufficient_phrases if phrase in normalized_skill],
            remediation=[
                "Restore remaining-result delivery, evidence-backed fallback choices, full-context constraint priority, and direction inheritance."
            ],
        )
    )

    detail_selection_phrases = (
        "After any non-empty candidate list",
        "visible sequence number",
        "序号",
        "歌名",
        "前几首",
        "全部",
        "description dimension is optional",
        "Fetch canonical analysis only after a user selection",
    )
    detail_selection_ok = bool(skill) and all(
        phrase in normalized_skill for phrase in detail_selection_phrases
    )
    checks.append(
        _check(
            "music-kb-ux-detail-selection",
            detail_selection_ok,
            "Candidate replies expose a simple, optional path to selected complete descriptions.",
            evidence=[phrase for phrase in detail_selection_phrases if phrase in normalized_skill],
            remediation=[
                "Ask which displayed songs to expand and accept numbers, titles, prefix selections, or all without requiring a field choice."
            ],
        )
    )

    detail_batching_phrases = (
        "Deliver complete descriptions in readable batches",
        "one to four selected songs",
        "five or more selected songs",
        "at most **four songs per batch**",
        "only for the current batch",
        "Do not prefetch canonical analyses",
        "Preserve the selected order",
        "does not set the size of the first candidate page",
    )
    detail_batching_ok = bool(skill) and all(
        phrase in normalized_skill for phrase in detail_batching_phrases
    )
    checks.append(
        _check(
            "music-kb-ux-detail-batching",
            detail_batching_ok,
            "Selected complete descriptions are fetched lazily in batches of at most four.",
            evidence=[phrase for phrase in detail_batching_phrases if phrase in normalized_skill],
            remediation=[
                "Cap detail batches at four, fetch only the current batch, and preserve selection order and candidate-page independence."
            ],
        )
    )

    detail_language_phrases = (
        "Keep canonical descriptions faithful to the user's language",
        "user's current language",
        "complete and faithful Chinese rendering",
        "Do not summarize away content",
        "outside the canonical analysis",
        "English original or a bilingual version",
        "user explicitly asks",
        "raw_text_truncated",
        "retrieval-only",
    )
    detail_language_ok = bool(skill) and all(
        phrase in normalized_skill for phrase in detail_language_phrases
    )
    checks.append(
        _check(
            "music-kb-ux-detail-language",
            detail_language_ok,
            "Complete descriptions follow the user's language without summarization or unsupported additions.",
            evidence=[phrase for phrase in detail_language_phrases if phrase in normalized_skill],
            remediation=[
                "Preserve all canonical content in the user's language, disclose truncation, and show English or bilingual text only on request."
            ],
        )
    )

    evidence_phrases = (
        "orders exact matches by group representativeness",
        "small shortlist",
        "facet_counts",
        "facet_scope.kind=all_matches",
        "listen_url",
        "search_projection_state",
        "do not infer mood",
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

    compact_phrases = (
        "without song records",
        "matched_tags",
        "representative_tags",
        "selection_basis",
        "do not call the page a universal",
        "next_offset",
        "legacy `music_kb_search` row order",
        "omit full tag dumps",
        "Never retrieve a larger page and then prune, reorder, or silently de-duplicate it",
        "displayed IDs must exactly equal",
        "Compact recommendations expose `listen_url` but omit",
    )
    compact_ok = bool(skill) and all(phrase in normalized_skill for phrase in compact_phrases)
    checks.append(
        _check(
            "music-kb-ux-ranked-compact-retrieval",
            compact_ok,
            "Direction discovery and ranked recommendation keep intermediate retrieval out of the model context.",
            evidence=[phrase for phrase in compact_phrases if phrase in normalized_skill],
            remediation=[
                "Use all-match facet discovery, backend-ranked compact rows, stable continuation, and lazy canonical details."
            ],
        )
    )

    rendering_phrases = (
        "Markdown listening link",
        "也符合：",
        "cross-group duplicate",
    )
    rendering_ok = bool(skill) and all(phrase in normalized_skill for phrase in rendering_phrases)
    checks.append(
        _check(
            "music-kb-ux-rendering-contract",
            rendering_ok,
            "The written contract makes listening links and cross-group overlap disclosure visible to users.",
            evidence=[phrase for phrase in rendering_phrases if phrase in normalized_skill],
            remediation=[
                "Require Markdown listening links and a short overlap label whenever a recording appears in more than one direction."
            ],
        )
    )

    deferred_phrases = (
        "default intersection/union semantics",
        "ambiguous multi-tag wording",
        "deliberately deferred product decision",
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
            "Ambiguous multi-tag semantics remain deferred; no withdrawn fixed quantity returned.",
            evidence=[phrase for phrase in deferred_phrases if phrase in normalized_skill]
            + (["fixed default detected"] if fixed_default else ["no fixed quantity pattern detected"]),
            remediation=["Do not encode a universal intersection/union default before a later product decision."],
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

    contract_checks = list(checks)
    passed = sum(1 for check in contract_checks if check["status"] == "pass")
    total = len(contract_checks)
    score = round((passed / total) * 100, 2) if total else 0
    band = "good" if score == 100 else "needs-work" if score >= 80 else "poor"
    metrics = [
        {
            "id": "music-kb-conversation-contract-coverage",
            "category": "conversation-contract",
            "value": score,
            "unit": "percent",
            "band": band,
        },
        {
            "id": "music-kb-conversation-contract-failed-checks",
            "category": "conversation-contract",
            "value": total - passed,
            "unit": "checks",
            "band": "good" if passed == total else "needs-work",
        },
    ]
    artifacts: list[dict[str, Any]] = []
    trace_value = os.environ.get("MUSIC_KB_CONVERSATION_TRACE", "").strip()
    if trace_value:
        trace_path = Path(trace_value).expanduser().resolve()
        behavior_checks, behavior_metrics = validate_trace_file(trace_path)
        checks.extend(behavior_checks)
        metrics.extend(behavior_metrics)
        artifacts.append(
            {
                "id": "music-kb-conversation-runtime-trace",
                "type": "runtime-trace",
                "label": "Normalized Music KB conversation trace",
                "description": "Runtime behavior evidence evaluated separately from the static Skill contract.",
                "path": str(trace_path),
            }
        )
    else:
        checks.append(
            _info(
                "music-kb-runtime-behavior-unmeasured",
                "Runtime branch execution and final-answer grouping were not measured because no normalized trace was provided.",
                evidence=[
                    "Set MUSIC_KB_CONVERSATION_TRACE to a trace matching trace-schema.json to measure behavior."
                ],
            )
        )
    payload = {
        "checks": checks,
        "metrics": metrics,
        "artifacts": artifacts,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
