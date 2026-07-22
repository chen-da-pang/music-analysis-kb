from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_claude_lyrics_backfill.py"


def _module():
    spec = importlib.util.spec_from_file_location("run_claude_lyrics_backfill", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(source_track_id: str) -> dict[str, str]:
    return {
        "recording_id": f"recording-{source_track_id}",
        "source_track_id": source_track_id,
        "source_name": "kugou",
        "platform_track_key": source_track_id.removeprefix("kugou-"),
        "title": "Fixture title",
        "artist": "Fixture artist",
    }


def test_backfill_chunks_retry_pending_but_skip_terminal_receipts() -> None:
    module = _module()
    rows = [_row("kugou-1"), _row("kugou-2"), _row("kugou-3")]
    progress = {
        "results": {
            "kugou-1": {"query_status": "completed", "lyric_status": "available"},
            "kugou-2": {"query_status": "completed", "lyric_status": "pending"},
        }
    }

    chunks = module.pending_chunks(rows, progress, chunk_size=2, max_items=None)

    assert [[module.row_identity(row) for row in chunk] for chunk in chunks] == [["kugou-2", "kugou-3"]]


def test_backfill_progress_marks_pending_as_attempted_and_prompt_forbids_audio() -> None:
    module = _module()
    rows = [_row("kugou-1"), _row("kugou-2")]
    summary = module.progress_summary(
        rows,
        {
            "results": {
                "kugou-1": {"query_status": "completed", "lyric_status": "pending"},
                "kugou-2": {"query_status": "completed", "lyric_status": "instrumental"},
            }
        },
    )
    prompt = module.render_prompt("python3 worker.py --lyrics-only", chunk_index=1, chunk_total=2)

    assert summary == {
        "queue": 2,
        "attempted": 2,
        "available": 0,
        "instrumental": 1,
        "platform_unavailable": 0,
        "pending": 1,
        "unattempted": [],
    }
    assert "不能下载音频" in prompt
    assert "不得扫描、移动或手工补写 .lrc" in prompt
    assert "--lyrics-only" in prompt
