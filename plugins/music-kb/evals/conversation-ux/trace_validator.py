"""Validate normalized runtime traces for multi-direction Music KB retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


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
        "category": "conversation-behavior",
        "severity": "info" if ok else "error",
        "status": "pass" if ok else "fail",
        "message": message,
        "evidence": evidence,
        "remediation": remediation,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _result_count(item: Mapping[str, Any]) -> int | None:
    value = item.get("result_count")
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def validate_trace(trace: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return plugin-eval checks and metrics for one normalized behavior trace."""

    checks: list[dict[str, Any]] = []
    base = trace.get("base_search") if isinstance(trace.get("base_search"), Mapping) else {}
    base_result = base.get("result") if isinstance(base.get("result"), Mapping) else {}
    facet_counts = base_result.get("facet_counts") if isinstance(base_result.get("facet_counts"), list) else []
    facet_scope = base_result.get("facet_scope") if isinstance(base_result.get("facet_scope"), Mapping) else {}
    schema_ok = trace.get("schema_version") == 1 and bool(str(trace.get("scenario_id") or "").strip())
    checks.append(
        _check(
            "music-kb-behavior-trace-shape",
            schema_ok,
            "The normalized runtime trace identifies a versioned benchmark scenario.",
            evidence=[f"schema_version={trace.get('schema_version')}", f"scenario_id={trace.get('scenario_id')}"],
            remediation=["Normalize the runtime run with trace schema version 1 and a stable scenario_id."],
        )
    )

    base_ok = (
        base.get("tool") == "music_kb_search"
        and bool(facet_counts)
        and facet_scope.get("kind") == "returned_results"
        and isinstance(facet_scope.get("recording_count"), int)
    )
    checks.append(
        _check(
            "music-kb-behavior-base-facets",
            base_ok,
            "The base search exposes tag facets scoped to its bounded returned rows.",
            evidence=[
                f"tool={base.get('tool')}",
                f"facet_count={len(facet_counts)}",
                f"scope={facet_scope.get('kind')}",
            ],
            remediation=["Capture the literal base music_kb_search result, including facet_counts and facet_scope."],
        )
    )

    supported_raw = trace.get("supported_directions")
    supported = supported_raw if isinstance(supported_raw, list) else []
    important_ids = [
        str(item.get("id")).strip()
        for item in supported
        if isinstance(item, Mapping)
        and item.get("important", True)
        and str(item.get("id") or "").strip()
    ]
    selected_ids = _string_list(trace.get("selected_direction_ids"))
    important_set = set(important_ids)
    selected_set = set(selected_ids)
    expected_count = min(len(important_set), 3)
    selection_ok = (
        len(important_set) >= 2
        and len(selected_ids) == len(selected_set) == expected_count
        and selected_set.issubset(important_set)
        and (len(important_set) > 3 or selected_set == important_set)
    )
    checks.append(
        _check(
            "music-kb-behavior-direction-completeness",
            selection_ok,
            "Two or three evidence-backed important directions are selected without silent loss.",
            evidence=[f"important={important_ids}", f"selected={selected_ids}"],
            remediation=[
                "Select two or three important directions; when two or three are supported, include every one."
            ],
        )
    )

    branch_raw = trace.get("branch_searches")
    branch_searches = branch_raw if isinstance(branch_raw, list) else []
    searches_by_direction: dict[str, list[Mapping[str, Any]]] = {}
    for item in branch_searches:
        if not isinstance(item, Mapping):
            continue
        direction_id = str(item.get("direction_id") or "").strip()
        if direction_id:
            searches_by_direction.setdefault(direction_id, []).append(item)
    selected_searches = [
        searches_by_direction[direction_id][0]
        for direction_id in selected_ids
        if len(searches_by_direction.get(direction_id, [])) == 1
    ]
    call_ids = [str(item.get("call_id") or "").strip() for item in selected_searches]
    argument_keys = [
        json.dumps(item.get("arguments"), ensure_ascii=False, sort_keys=True)
        for item in selected_searches
        if isinstance(item.get("arguments"), Mapping)
    ]
    searches_ok = (
        len(selected_searches) == len(selected_ids)
        and all(item.get("tool") == "music_kb_search" for item in selected_searches)
        and all(call_ids)
        and len(call_ids) == len(set(call_ids))
        and len(argument_keys) == len(set(argument_keys)) == len(selected_ids)
        and all(_result_count(item) is not None for item in selected_searches)
    )
    checks.append(
        _check(
            "music-kb-behavior-independent-searches",
            searches_ok,
            "Every selected direction has one distinct bounded music_kb_search call.",
            evidence=[f"selected={len(selected_ids)}", f"distinct_calls={len(set(call_ids))}", f"distinct_args={len(set(argument_keys))}"],
            remediation=["Run one distinct music_kb_search call for each selected direction."],
        )
    )

    valid_ids = [
        direction_id
        for direction_id in selected_ids
        if len(searches_by_direction.get(direction_id, [])) == 1
        and (_result_count(searches_by_direction[direction_id][0]) or 0) > 0
        and searches_by_direction[direction_id][0].get("credible_results", True)
    ]
    weak_ids = [direction_id for direction_id in selected_ids if direction_id not in valid_ids]
    final = trace.get("final_response") if isinstance(trace.get("final_response"), Mapping) else {}
    grouped_ids = _string_list(final.get("grouped_direction_ids"))
    reported_ids = _string_list(final.get("reported_separately_direction_ids"))
    grouping_ok = (
        final.get("layout") == "grouped"
        and grouped_ids == valid_ids
        and reported_ids == weak_ids
        and not _string_list(final.get("ungrouped_recording_ids"))
    )
    checks.append(
        _check(
            "music-kb-behavior-grouped-rendering",
            grouping_ok,
            "The final answer keeps every valid searched direction in its own group and reports weak directions separately.",
            evidence=[
                f"layout={final.get('layout')}",
                f"expected_groups={valid_ids}",
                f"rendered_groups={grouped_ids}",
                f"separate_reports={reported_ids}",
            ],
            remediation=["Render one answer group per valid branch and never flatten searched branches into one list."],
        )
    )

    passed = sum(item["status"] == "pass" for item in checks)
    total = len(checks)
    score = round((passed / total) * 100, 2) if total else 0.0
    metrics = [
        {
            "id": "music-kb-runtime-behavior-coverage",
            "category": "conversation-behavior",
            "value": score,
            "unit": "percent",
            "band": "good" if score == 100 else "needs-work",
        },
        {
            "id": "music-kb-runtime-behavior-failed-checks",
            "category": "conversation-behavior",
            "value": total - passed,
            "unit": "checks",
            "band": "good" if passed == total else "needs-work",
        },
    ]
    return checks, metrics


def validate_trace_file(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            [
                _check(
                    "music-kb-behavior-trace-shape",
                    False,
                    "The runtime trace could not be read as JSON.",
                    evidence=[str(exc)],
                    remediation=["Provide a readable JSON trace matching trace-schema.json."],
                )
            ],
            [],
        )
    if not isinstance(payload, Mapping):
        return validate_trace({})
    return validate_trace(payload)
