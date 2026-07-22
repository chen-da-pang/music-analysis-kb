from __future__ import annotations

import asyncio

from music_kb.mcp_server import ReadOnlyMusicKB, create_server


def test_read_only_mcp_facade_searches_canonical_data(master_database) -> None:
    api = ReadOnlyMusicKB(master_database)
    result = api.search(tags=["granular vocal chop"])
    assert result["count"] == 1
    assert result["results"][0]["recording_id"] == "rec_neon_night_studio"
    assert result["facet_counts"] == [
        {"namespace": "drum", "name": "syncopated rimshot", "count": 1},
        {"namespace": "genre", "name": "electronic pop", "count": 1},
        {"namespace": "production", "name": "granular vocal chop", "count": 1},
    ]
    assert result["facet_scope"] == {
        "kind": "returned_results",
        "recording_count": 1,
        "max_per_namespace": 5,
    }
    assert all(item["namespace"] not in {"title", "artist"} for item in result["facet_counts"])
    assert not hasattr(api, "compile_suno_style")
    canonical = api.get_canonical_analysis(recording_id="rec_neon_night_studio")
    assert all("suno_safe" not in tag for tag in canonical["tags"])
    facets = api.tag_facets(prefix="granular")
    assert all("suno_safe" not in tag for tag in facets["tags"])
    empty = api.search(tags=["definitely absent fixture tag"])
    assert empty["count"] == 0
    assert empty["facet_counts"] == []
    assert empty["facet_scope"]["recording_count"] == 0


def test_mcp_server_exposes_only_retrieval_tools(master_database) -> None:
    server = create_server(master_database)
    assert [tool.name for tool in asyncio.run(server.list_tools())] == [
        "music_kb_status",
        "music_kb_search",
        "music_kb_resolve_title_artist",
        "music_kb_get_canonical_analysis",
        "music_kb_tag_facets",
    ]
