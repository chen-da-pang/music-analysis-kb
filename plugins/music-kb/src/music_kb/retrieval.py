from __future__ import annotations

from dataclasses import dataclass


DIVERSITY_NAMESPACES = (
    "genre",
    "mood",
    "lyric_theme",
    "vocal",
    "production",
    "tempo",
    "instrument",
    "rhythm",
    "drum",
)
MAX_DIVERSITY_LOOKAHEAD = 12
REPRESENTATIVE_SCORE_FLOOR_RATIO = 0.90
MAX_VISIBLE_EVIDENCE_TAGS = 4


@dataclass(frozen=True)
class EvidenceTag:
    namespace: str
    name: str
    frequency: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.namespace, self.name)


@dataclass(frozen=True)
class CandidateEvidence:
    recording_id: str
    representative_score: float
    tags: tuple[EvidenceTag, ...]


@dataclass(frozen=True)
class CandidateSelection:
    recording_id: str
    selection_basis: str
    representative_tags: tuple[EvidenceTag, ...]


def _jaccard(left: frozenset[tuple[str, str]], right: frozenset[tuple[str, str]]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _visible_tags(
    candidate: CandidateEvidence,
    *,
    excluded: frozenset[tuple[str, str]] | None = None,
) -> tuple[EvidenceTag, ...]:
    namespace_order = {namespace: index for index, namespace in enumerate(DIVERSITY_NAMESPACES)}
    tags = [
        tag
        for tag in candidate.tags
        if excluded is None or tag.key not in excluded
    ]
    tags.sort(
        key=lambda tag: (
            namespace_order.get(tag.namespace, len(namespace_order)),
            -tag.frequency,
            tag.name,
        )
    )
    visible: list[EvidenceTag] = []
    used_namespaces: set[str] = set()
    for tag in tags:
        if tag.namespace in used_namespaces:
            continue
        visible.append(tag)
        used_namespaces.add(tag.namespace)
        if len(visible) >= MAX_VISIBLE_EVIDENCE_TAGS:
            return tuple(visible)
    for tag in tags:
        if tag in visible:
            continue
        visible.append(tag)
        if len(visible) >= MAX_VISIBLE_EVIDENCE_TAGS:
            break
    return tuple(visible)


def select_representative_candidates(
    candidates: list[CandidateEvidence],
) -> list[CandidateSelection]:
    """Keep the representative order primary and add bounded tag diversity.

    The database has already hard-filtered every required condition.  This
    selector may promote only a candidate whose representative score is close
    to the best remaining candidate, and it only looks ahead a small fixed
    window.  Diversity therefore cannot pull an obvious outlier into the page.
    """

    remaining = sorted(
        candidates,
        key=lambda candidate: (-candidate.representative_score, candidate.recording_id),
    )
    selections: list[CandidateSelection] = []
    selected_tag_sets: list[frozenset[tuple[str, str]]] = []
    seen_tags: set[tuple[str, str]] = set()

    while remaining:
        best_remaining = remaining[0]
        chosen_index = 0
        selection_basis = "representative_core"

        if selections:
            floor = best_remaining.representative_score * REPRESENTATIVE_SCORE_FLOOR_RATIO
            window = remaining[:MAX_DIVERSITY_LOOKAHEAD]
            eligible = [
                (index, candidate)
                for index, candidate in enumerate(window)
                if candidate.representative_score >= floor
            ]

            novel_candidates = [
                (index, candidate)
                for index, candidate in eligible
                if any(tag.key not in seen_tags for tag in candidate.tags)
            ]

            def novelty(item: tuple[int, CandidateEvidence]) -> tuple[float, int, float, int]:
                index, candidate = item
                tag_set = frozenset(tag.key for tag in candidate.tags)
                maximum_similarity = max(
                    (_jaccard(tag_set, selected) for selected in selected_tag_sets),
                    default=0.0,
                )
                new_tag_count = len(tag_set - seen_tags)
                return (
                    1.0 - maximum_similarity,
                    new_tag_count,
                    candidate.representative_score,
                    -index,
                )

            if novel_candidates:
                chosen_index, _ = max(novel_candidates, key=novelty)
                if chosen_index != 0:
                    selection_basis = "secondary_tag_coverage"

        chosen = remaining.pop(chosen_index)
        chosen_tag_set = frozenset(tag.key for tag in chosen.tags)
        visible_tags = _visible_tags(
            chosen,
            excluded=frozenset(seen_tags) if selection_basis == "secondary_tag_coverage" else None,
        )
        selections.append(
            CandidateSelection(
                recording_id=chosen.recording_id,
                selection_basis=selection_basis,
                representative_tags=visible_tags,
            )
        )
        selected_tag_sets.append(chosen_tag_set)
        seen_tags.update(chosen_tag_set)

    return selections
