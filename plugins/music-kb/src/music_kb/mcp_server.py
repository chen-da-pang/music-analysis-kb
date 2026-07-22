from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .errors import MusicKBError
from .repository import MusicKBRepository


def configured_database() -> Path:
    return Path(os.environ.get("MUSIC_KB_DB", "~/.music-kb/current.sqlite")).expanduser()


class ReadOnlyMusicKB:
    """Read facade shared by the MCP tools and direct unit tests."""

    def __init__(self, database: str | Path | None = None) -> None:
        self.database = Path(database or configured_database()).expanduser()

    def _call(self, operation: Callable[[MusicKBRepository], dict[str, Any] | list[dict[str, Any]]]) -> Any:
        with MusicKBRepository(self.database, read_only=True) as repository:
            return operation(repository)

    def status(self) -> dict[str, Any]:
        return self._call(lambda repository: repository.status())

    def search(
        self,
        *,
        query: str = "",
        tags: list[str] | None = None,
        title: str = "",
        artist: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        results = self._call(
            lambda repository: repository.search(
                query=query, tags=tags or [], title=title, artist=artist, limit=limit
            )
        )
        assert isinstance(results, list)
        return {"results": results, "count": len(results), "limit_applied": min(max(int(limit), 1), 50)}

    def resolve_title_artist(self, *, title: str, artist: str = "", limit: int = 10) -> dict[str, Any]:
        return self.search(title=title, artist=artist, limit=limit)

    def get_canonical_analysis(self, *, recording_id: str, max_chars: int = 24_000) -> dict[str, Any]:
        result = self._call(
            lambda repository: repository.get_canonical_analysis(recording_id, max_chars=max_chars)
        )
        assert isinstance(result, dict)
        return result

    def get_lyrics(self, *, recording_id: str) -> dict[str, Any]:
        """Return the selected recording's full stored lyric text unchanged."""

        result = self._call(lambda repository: repository.get_lyrics(recording_id))
        assert isinstance(result, dict)
        return result

    def tag_facets(self, *, namespace: str = "", prefix: str = "", limit: int = 30) -> dict[str, Any]:
        result = self._call(
            lambda repository: repository.tag_facets(namespace=namespace, prefix=prefix, limit=limit)
        )
        assert isinstance(result, list)
        return {"tags": result, "count": len(result)}

def create_server(database: str | Path | None = None) -> Any:
    """Create a stdio server with no mutation-capable tools."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised during setup errors
        raise RuntimeError("The MCP runtime is missing. Run `uv sync` in the music-kb plugin.") from exc

    api = ReadOnlyMusicKB(database)
    server = FastMCP("Music Knowledge Base")

    @server.tool(name="music_kb_status", description="Show local snapshot path, version, and counts without writing.")
    def music_kb_status() -> dict[str, Any]:
        return api.status()

    @server.tool(
        name="music_kb_search",
        description="Search canonical Music Flamingo analyses by text, exact tags/aliases, title, and artist. Results are bounded and read-only.",
    )
    def music_kb_search(
        query: str = "",
        tags: list[str] | None = None,
        title: str = "",
        artist: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        return api.search(query=query, tags=tags, title=title, artist=artist, limit=limit)

    @server.tool(
        name="music_kb_resolve_title_artist",
        description="Resolve a supplied title and optional artist through title/artist aliases in the local canonical library.",
    )
    def music_kb_resolve_title_artist(title: str, artist: str = "", limit: int = 10) -> dict[str, Any]:
        return api.resolve_title_artist(title=title, artist=artist, limit=limit)

    @server.tool(
        name="music_kb_get_canonical_analysis",
        description="Fetch one canonical analysis by recording ID. Historical revisions are intentionally unavailable.",
    )
    def music_kb_get_canonical_analysis(recording_id: str, max_chars: int = 24_000) -> dict[str, Any]:
        return api.get_canonical_analysis(recording_id=recording_id, max_chars=max_chars)

    @server.tool(
        name="music_kb_get_lyrics",
        description="Fetch the full stored lyrics for one selected canonical recording by recording ID. This does not search lyrics or modify the local snapshot.",
    )
    def music_kb_get_lyrics(recording_id: str) -> dict[str, Any]:
        return api.get_lyrics(recording_id=recording_id)

    @server.tool(
        name="music_kb_tag_facets",
        description="Find controlled tags and aliases by namespace/prefix; useful for rare exact terms.",
    )
    def music_kb_tag_facets(namespace: str = "", prefix: str = "", limit: int = 30) -> dict[str, Any]:
        return api.tag_facets(namespace=namespace, prefix=prefix, limit=limit)

    return server


def main() -> None:
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
