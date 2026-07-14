from __future__ import annotations

from music_kb.mcp_server import ReadOnlyMusicKB, create_server


def test_read_only_mcp_facade_searches_canonical_data(master_database) -> None:
    api = ReadOnlyMusicKB(master_database)
    result = api.search(tags=["granular vocal chop"])
    assert result["count"] == 1
    assert result["results"][0]["recording_id"] == "rec_neon_night_studio"
    assert not hasattr(api, "compile_suno_style")
    canonical = api.get_canonical_analysis(recording_id="rec_neon_night_studio")
    assert all("suno_safe" not in tag for tag in canonical["tags"])
    facets = api.tag_facets(prefix="granular")
    assert all("suno_safe" not in tag for tag in facets["tags"])


def test_mcp_server_can_be_constructed(master_database) -> None:
    server = create_server(master_database)
    assert server is not None
