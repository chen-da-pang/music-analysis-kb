from __future__ import annotations

from music_kb.repository import MusicKBRepository
from music_kb.retrieval import (
    DIVERSITY_NAMESPACES,
    CandidateEvidence,
    EvidenceTag,
    select_representative_candidates,
)
from music_kb.schema import initialize_database


def _evidence(namespace: str, name: str, frequency: int) -> EvidenceTag:
    return EvidenceTag(namespace=namespace, name=name, frequency=frequency)


def test_selector_promotes_only_close_relevance_diversity() -> None:
    shared = (
        _evidence("genre", "ballad", 3),
        _evidence("mood", "intimate", 3),
    )
    candidates = [
        CandidateEvidence(
            "rec_a",
            100,
            (*shared, _evidence("production", "reverb", 2)),
        ),
        CandidateEvidence(
            "rec_b",
            99,
            (*shared, _evidence("production", "delay", 2)),
        ),
        CandidateEvidence(
            "rec_c",
            95,
            (
                *shared,
                _evidence("genre", "soul", 1),
                _evidence("mood", "dreamy", 1),
            ),
        ),
        CandidateEvidence(
            "rec_outlier",
            50,
            (
                _evidence("genre", "experimental", 1),
                _evidence("vocal", "spoken word", 1),
            ),
        ),
    ]

    selected = select_representative_candidates(candidates)

    assert [item.recording_id for item in selected[:3]] == ["rec_a", "rec_c", "rec_b"]
    assert selected[1].selection_basis == "secondary_tag_coverage"
    assert selected[2].recording_id != "rec_outlier"
    assert len({tag.namespace for tag in selected[0].representative_tags}) == len(
        selected[0].representative_tags
    )


def test_selector_does_not_promote_a_sparse_subset_without_new_tags() -> None:
    common = (
        _evidence("genre", "ballad", 3),
        _evidence("mood", "intimate", 3),
        _evidence("production", "reverb", 3),
    )
    candidates = [
        CandidateEvidence("rec_a", 100, common),
        CandidateEvidence("rec_b", 99, common),
        CandidateEvidence("rec_sparse", 95, common[:1]),
    ]

    selected = select_representative_candidates(candidates)

    assert [item.recording_id for item in selected] == [
        "rec_a",
        "rec_b",
        "rec_sparse",
    ]
    assert all(item.selection_basis == "representative_core" for item in selected)


def _payload(recording_id: str, secondary_tags: list[tuple[str, str]]) -> dict[str, object]:
    return {
        "recording": {"id": recording_id, "title": recording_id},
        "artists": [{"name": f"artist {recording_id}"}],
        "analysis": {
            "raw_text": f"canonical analysis for {recording_id}",
            "quality_state": "passed",
        },
        "tags": [
            {"namespace": "genre", "name": "r&b", "confidence": 1.0},
            {"namespace": "mood", "name": "warm", "confidence": 1.0},
            {"namespace": "lyric_theme", "name": "love", "confidence": 1.0},
            *(
                {"namespace": namespace, "name": name, "confidence": 1.0}
                for namespace, name in secondary_tags
            ),
        ],
        "source_tracks": [
            {
                "source": "fixture",
                "source_track_id": recording_id,
                "source_url": f"https://music.example/{recording_id}",
            }
        ],
    }


def test_discover_and_recommend_separate_coverage_from_compact_results(tmp_path) -> None:
    database = tmp_path / "ranked.sqlite"
    initialize_database(database)
    payloads = [
        _payload(
            "rec_a",
            [("genre", "ballad"), ("mood", "intimate"), ("production", "reverb")],
        ),
        _payload(
            "rec_b",
            [("genre", "ballad"), ("mood", "intimate"), ("production", "reverb")],
        ),
        _payload(
            "rec_c",
            [
                ("genre", "ballad"),
                ("mood", "intimate"),
                ("genre", "soul"),
                ("mood", "dreamy"),
            ],
        ),
        _payload(
            "rec_outlier",
            [("genre", "experimental"), ("vocal", "spoken word")],
        ),
    ]
    with MusicKBRepository(database) as repository:
        repository.import_analyses(payloads)
        repository.connection.execute(
            "UPDATE recording SET updated_at = '2099-01-01' WHERE id = 'rec_outlier'"
        )

        discovered = repository.discover(tags=["r&b", "warm", "love"])
        recommended = repository.recommend(tags=["r&b", "warm", "love"], limit=3)
        continued = repository.recommend(tags=["r&b", "warm", "love"], limit=3, offset=3)

    assert discovered["match_count"] == 4
    assert discovered["facet_scope"]["kind"] == "all_matches"
    assert discovered["facet_scope"]["recording_count"] == 4
    assert discovered["facet_scope"]["facet_count"] == len(discovered["facet_counts"])
    assert discovered["facet_scope"]["namespaces"] == list(DIVERSITY_NAMESPACES)
    assert discovered["facet_scope"]["per_namespace_target"] == 20
    assert discovered["facet_scope"]["cutoff_ties_included"] is True
    assert discovered["facet_scope"]["truncated_namespaces"] == []
    assert "results" not in discovered
    assert {item["name"] for item in discovered["facet_counts"]}.issuperset(
        {"r&b", "warm", "love", "ballad", "intimate"}
    )

    assert [item["recording_id"] for item in recommended["results"]] == [
        "rec_a",
        "rec_c",
        "rec_b",
    ]
    assert recommended["results"][1]["selection_basis"] == "secondary_tag_coverage"
    assert recommended["results"][0]["listen_url"] == "https://music.example/rec_a"
    assert recommended["match_count"] == 4
    assert recommended["next_offset"] == 3
    assert recommended["has_more"] is True
    assert all(
        not {"tags", "summary", "source_links", "canonical_created_at"}.intersection(item)
        for item in recommended["results"]
    )
    assert [item["recording_id"] for item in continued["results"]] == ["rec_outlier"]
    assert continued["next_offset"] is None
    assert continued["has_more"] is False


def test_discover_keeps_all_tags_tied_at_namespace_cutoff(tmp_path) -> None:
    database = tmp_path / "facet-ties.sqlite"
    initialize_database(database)
    payloads = [
        _payload(
            "rec_a",
            [("genre", "ballad"), ("genre", "soul")],
        ),
        _payload(
            "rec_b",
            [("genre", "ballad"), ("genre", "soul")],
        ),
        _payload(
            "rec_c",
            [("genre", "ballad"), ("genre", "soul"), ("genre", "experimental")],
        ),
    ]
    with MusicKBRepository(database) as repository:
        repository.import_analyses(payloads)
        discovered = repository.discover(
            tags=["r&b", "warm", "love"],
            per_namespace_limit=1,
        )

    genre_names = {
        item["name"]
        for item in discovered["facet_counts"]
        if item["namespace"] == "genre"
    }
    assert genre_names == {"r&b", "ballad", "soul"}
    genre_scope = next(
        item
        for item in discovered["facet_scope"]["truncated_namespaces"]
        if item["namespace"] == "genre"
    )
    assert genre_scope == {"namespace": "genre", "returned": 3, "available": 4}
