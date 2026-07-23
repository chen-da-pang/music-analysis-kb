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


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


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
    runtime = trace.get("runtime") if isinstance(trace.get("runtime"), Mapping) else {}
    runtime_events = _mapping_list(runtime.get("events"))
    event_ids = [str(item.get("event_id") or "").strip() for item in runtime_events]
    retrieval_events = [item for item in runtime_events if item.get("kind") == "retrieval_call"]
    branch_recommendations = _mapping_list(trace.get("branch_recommendations"))
    base = trace.get("base_discovery") if isinstance(trace.get("base_discovery"), Mapping) else {}
    base_result = base.get("result") if isinstance(base.get("result"), Mapping) else {}
    facet_counts = base_result.get("facet_counts") if isinstance(base_result.get("facet_counts"), list) else []
    facet_scope = base_result.get("facet_scope") if isinstance(base_result.get("facet_scope"), Mapping) else {}
    schema_ok = (
        trace.get("schema_version") == 3
        and bool(str(trace.get("scenario_id") or "").strip())
        and runtime.get("capture_complete") is True
        and bool(runtime_events)
        and all(event_ids)
        and len(event_ids) == len(set(event_ids))
    )
    checks.append(
        _check(
            "music-kb-behavior-trace-shape",
            schema_ok,
            "The normalized runtime trace identifies a complete, versioned benchmark scenario.",
            evidence=[
                f"schema_version={trace.get('schema_version')}",
                f"scenario_id={trace.get('scenario_id')}",
                f"capture_complete={runtime.get('capture_complete')}",
                f"event_count={len(runtime_events)}",
            ],
            remediation=[
                "Normalize the complete runtime event stream with trace schema version 3 and a stable scenario_id."
            ],
        )
    )

    skill_read_count = sum(item.get("kind") == "skill_read" for item in runtime_events)
    checks.append(
        _check(
            "music-kb-behavior-single-skill-read",
            skill_read_count <= 1,
            "The runtime does not reread the Skill before retrieval.",
            evidence=[f"skill_read_count={skill_read_count}"],
            remediation=["Read the Skill at most once; do not reopen sections before calling the selected route."],
        )
    )

    mcp_resource_probe_count = sum(
        item.get("kind") == "mcp_resource_probe" for item in runtime_events
    )
    checks.append(
        _check(
            "music-kb-behavior-no-mcp-resource-probes",
            mcp_resource_probe_count == 0,
            "Named Music KB functions are used directly without MCP resource discovery.",
            evidence=[f"mcp_resource_probe_count={mcp_resource_probe_count}"],
            remediation=["Do not call MCP resource-listing APIs to discover named Music KB functions."],
        )
    )

    implementation_inspection_count = sum(
        item.get("kind") == "implementation_inspection" for item in runtime_events
    )
    checks.append(
        _check(
            "music-kb-behavior-no-implementation-inspection",
            implementation_inspection_count == 0,
            "Retrieval does not inspect plugin source, README text, or installation internals.",
            evidence=[f"implementation_inspection_count={implementation_inspection_count}"],
            remediation=["Use the documented MCP or PATH interface without scanning implementation files."],
        )
    )

    help_probe_count = sum(item.get("kind") == "help_probe" for item in runtime_events)
    checks.append(
        _check(
            "music-kb-behavior-no-help-probes",
            help_probe_count == 0,
            "The known successful scenario does not spend a turn rediscovering CLI syntax.",
            evidence=[f"help_probe_count={help_probe_count}"],
            remediation=["Use the documented CLI forms directly; inspect help only after a real argument error."],
        )
    )

    mcp_entrypoints = {
        "status": "music_kb_status",
        "discover": "music_kb_discover",
        "recommend": "music_kb_recommend",
        "resolve_title_artist": "music_kb_resolve_title_artist",
        "get_canonical_analysis": "music_kb_get_canonical_analysis",
        "tag_facets": "music_kb_tag_facets",
    }
    direct_entrypoints_ok = all(
        (
            item.get("transport") == "cli"
            and item.get("entrypoint") == "music-kb"
        )
        or (
            item.get("transport") == "mcp"
            and item.get("entrypoint") == mcp_entrypoints.get(str(item.get("operation") or ""))
        )
        for item in retrieval_events
    )
    retrieval_call_ids = [str(item.get("call_id") or "").strip() for item in retrieval_events]
    successful_call_ids = {
        str(item.get("call_id") or "").strip()
        for item in retrieval_events
        if item.get("succeeded") is True
    }
    retrieval_by_call_id = {
        str(item.get("call_id") or "").strip(): item for item in retrieval_events
    }
    linked_call_ids = {
        str(base.get("call_id") or "").strip(),
        *[str(item.get("call_id") or "").strip() for item in branch_recommendations],
    }
    linked_call_ids.discard("")
    base_runtime_event = retrieval_by_call_id.get(str(base.get("call_id") or "").strip(), {})
    linked_events_ok = (
        base_runtime_event.get("operation") == "discover"
        and base_runtime_event.get("arguments") == base.get("arguments")
        and all(
            retrieval_by_call_id.get(str(item.get("call_id") or "").strip(), {}).get("operation")
            == "recommend"
            and retrieval_by_call_id.get(
                str(item.get("call_id") or "").strip(), {}
            ).get("arguments")
            == item.get("arguments")
            for item in branch_recommendations
        )
    )
    binary_resolution_probe_count = sum(
        item.get("kind") == "binary_resolution_probe" for item in runtime_events
    )
    direct_route_ok = (
        bool(retrieval_events)
        and retrieval_events[0].get("operation") == "status"
        and direct_entrypoints_ok
        and all(retrieval_call_ids)
        and len(retrieval_call_ids) == len(set(retrieval_call_ids))
        and linked_call_ids.issubset(successful_call_ids)
        and linked_events_ok
        and binary_resolution_probe_count == 0
    )
    checks.append(
        _check(
            "music-kb-behavior-direct-runtime-route",
            direct_route_ok,
            "Runtime retrieval starts with status and uses only the provided MCP functions or PATH music-kb command.",
            evidence=[
                f"first_operation={retrieval_events[0].get('operation') if retrieval_events else None}",
                f"entrypoints={[item.get('entrypoint') for item in retrieval_events]}",
                f"binary_resolution_probe_count={binary_resolution_probe_count}",
                f"linked_calls={sorted(linked_call_ids)}",
            ],
            remediation=[
                "Start with status, invoke named MCP tools or PATH music-kb directly, and never locate or call a private .venv binary."
            ],
        )
    )

    successful_query_events = [
        item
        for item in retrieval_events
        if item.get("succeeded") is True and item.get("operation") in {"discover", "recommend"}
    ]
    successful_query_signatures = [
        json.dumps(
            [item.get("operation"), item.get("arguments")],
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in successful_query_events
    ]
    duplicate_query_count = len(successful_query_signatures) - len(
        set(successful_query_signatures)
    )
    checks.append(
        _check(
            "music-kb-behavior-no-duplicate-retrieval",
            duplicate_query_count == 0,
            "Successful discovery and recommendation results are reused instead of repeated.",
            evidence=[
                f"successful_query_count={len(successful_query_signatures)}",
                f"duplicate_query_count={duplicate_query_count}",
            ],
            remediation=["Do not repeat a successful discovery or recommendation with identical arguments."],
        )
    )

    base_ok = (
        bool(str(base.get("call_id") or "").strip())
        and base.get("tool") == "music_kb_discover"
        and bool(facet_counts)
        and facet_scope.get("kind") == "all_matches"
        and isinstance(facet_scope.get("recording_count"), int)
        and base_result.get("match_count") == facet_scope.get("recording_count")
        and "results" not in base_result
    )
    checks.append(
        _check(
            "music-kb-behavior-base-discovery",
            base_ok,
            "Direction discovery covers all matches without serializing song records.",
            evidence=[
                f"tool={base.get('tool')}",
                f"facet_count={len(facet_counts)}",
                f"scope={facet_scope.get('kind')}",
            ],
            remediation=["Capture music_kb_discover with all-match facets, match_count, and no results array."],
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

    recommendations_by_direction: dict[str, list[Mapping[str, Any]]] = {}
    for item in branch_recommendations:
        if not isinstance(item, Mapping):
            continue
        direction_id = str(item.get("direction_id") or "").strip()
        if direction_id:
            recommendations_by_direction.setdefault(direction_id, []).append(item)
    selected_recommendations = [
        recommendations_by_direction[direction_id][0]
        for direction_id in selected_ids
        if len(recommendations_by_direction.get(direction_id, [])) == 1
    ]
    call_ids = [str(item.get("call_id") or "").strip() for item in selected_recommendations]
    argument_keys = [
        json.dumps(item.get("arguments"), ensure_ascii=False, sort_keys=True)
        for item in selected_recommendations
        if isinstance(item.get("arguments"), Mapping)
    ]
    recommendations_ok = (
        len(selected_ids) >= 2
        and len(selected_recommendations) == len(selected_ids)
        and len(branch_recommendations) == len(selected_ids)
        and set(recommendations_by_direction) == set(selected_ids)
        and all(item.get("tool") == "music_kb_recommend" for item in selected_recommendations)
        and all(call_ids)
        and len(call_ids) == len(set(call_ids))
        and len(argument_keys) == len(set(argument_keys)) == len(selected_ids)
        and all(_result_count(item) is not None for item in selected_recommendations)
    )
    checks.append(
        _check(
            "music-kb-behavior-independent-recommendations",
            recommendations_ok,
            "Every selected direction has one distinct compact recommendation call.",
            evidence=[f"selected={len(selected_ids)}", f"distinct_calls={len(set(call_ids))}", f"distinct_args={len(set(argument_keys))}"],
            remediation=["Run one distinct music_kb_recommend call for each selected direction."],
        )
    )

    required_fields = {
        "recording_id",
        "title",
        "artists",
        "matched_tags",
        "representative_tags",
        "selection_basis",
        "listen_url",
    }
    forbidden_fields = {"tags", "summary", "source_links", "canonical_created_at", "raw_text"}
    compact_ok = bool(selected_recommendations)
    compact_evidence: list[str] = []
    for item in selected_recommendations:
        fields = set(_string_list(item.get("result_fields")))
        payload_bytes = item.get("payload_bytes")
        match_count = item.get("match_count")
        result_count = _result_count(item)
        scope = item.get("selection_scope") if isinstance(item.get("selection_scope"), Mapping) else {}
        try:
            payload_size = int(payload_bytes)
            total_matches = int(match_count)
        except (TypeError, ValueError):
            compact_ok = False
            payload_size = -1
            total_matches = -1
        compact_ok = compact_ok and (
            required_fields.issubset(fields)
            and not forbidden_fields.intersection(fields)
            and 0 <= payload_size <= 12_000
            and result_count is not None
            and total_matches >= (result_count or 0)
            and scope.get("kind") == "ranked_representative_results"
        )
        compact_evidence.append(
            f"{item.get('direction_id')}:bytes={payload_size},fields={sorted(fields)}"
        )
    checks.append(
        _check(
            "music-kb-behavior-compact-ranked-results",
            compact_ok,
            "Branch calls return backend-ranked compact rows within the scenario payload budget.",
            evidence=compact_evidence,
            remediation=[
                "Return only compact recommendation fields, ranked selection scope, and at most 12000 bytes per first-page branch."
            ],
        )
    )

    valid_ids = [
        direction_id
        for direction_id in selected_ids
        if len(recommendations_by_direction.get(direction_id, [])) == 1
        and (_result_count(recommendations_by_direction[direction_id][0]) or 0) > 0
        and recommendations_by_direction[direction_id][0].get("credible_results", True)
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

    grouped_recording_raw = final.get("grouped_recording_ids")
    grouped_recording_ids = (
        grouped_recording_raw if isinstance(grouped_recording_raw, Mapping) else {}
    )
    page_fidelity_ok = bool(valid_ids) and set(grouped_recording_ids) == set(valid_ids)
    page_fidelity_evidence: list[str] = []
    for direction_id in valid_ids:
        recommendation = recommendations_by_direction[direction_id][0]
        returned_ids = _string_list(recommendation.get("result_recording_ids"))
        displayed_ids = _string_list(grouped_recording_ids.get(direction_id))
        result_count = _result_count(recommendation)
        direction_ok = (
            result_count == len(returned_ids)
            and len(returned_ids) == len(set(returned_ids))
            and displayed_ids == returned_ids
        )
        page_fidelity_ok = page_fidelity_ok and direction_ok
        page_fidelity_evidence.append(
            f"{direction_id}:returned={len(returned_ids)},displayed={len(displayed_ids)},same_order={displayed_ids == returned_ids}"
        )
    checks.append(
        _check(
            "music-kb-behavior-page-fidelity",
            page_fidelity_ok,
            "Every compact recommendation row is rendered once in stable branch order.",
            evidence=page_fidelity_evidence,
            remediation=[
                "Request the intended page size, then render every returned row in order without title pruning or cross-branch de-duplication."
            ],
        )
    )

    rendering_contract_ok = (
        final.get("overlap_labels_disclosed") is True
        and final.get("listening_links_markdown") is True
    )
    checks.append(
        _check(
            "music-kb-behavior-rendering-contract",
            rendering_contract_ok,
            "The final answer discloses cross-group overlaps and renders every listening URL as a Markdown link.",
            evidence=[
                f"overlap_labels_disclosed={final.get('overlap_labels_disclosed')}",
                f"listening_links_markdown={final.get('listening_links_markdown')}",
            ],
            remediation=[
                "Keep recordings in every matching group, label cross-group repeats, and render each runtime listen_url as a Markdown link."
            ],
        )
    )

    followup_actions = set(_string_list(final.get("followup_actions")))
    handoff_ok = {"再来一些", "换一批"}.issubset(followup_actions) and (
        final.get("complete_description_offer") is True
    )
    checks.append(
        _check(
            "music-kb-behavior-user-handoff",
            handoff_ok,
            "The answer teaches both continuation actions and offers selected complete descriptions.",
            evidence=[
                f"followup_actions={sorted(followup_actions)}",
                f"complete_description_offer={final.get('complete_description_offer')}",
            ],
            remediation=[
                "Include both 再来一些 and 换一批 guidance plus the optional complete-description selection question."
            ],
        )
    )

    exposed_internal_fields = set(_string_list(final.get("exposed_internal_fields")))
    forbidden_internal_fields = {"recording_id", "selection_basis"}
    boundary_ok = not exposed_internal_fields.intersection(forbidden_internal_fields)
    checks.append(
        _check(
            "music-kb-behavior-internal-boundary",
            boundary_ok,
            "Internal record identifiers and selection enums stay out of the user-facing answer.",
            evidence=[f"exposed_internal_fields={sorted(exposed_internal_fields)}"],
            remediation=[
                "Translate ranking evidence into ordinary language and hide recording_id and selection_basis."
            ],
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
