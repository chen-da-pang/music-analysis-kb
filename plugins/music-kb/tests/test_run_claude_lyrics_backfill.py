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


def test_backfill_progress_compacts_a_long_unattempted_tail() -> None:
    module = _module()

    result = module.compact_progress_summary(
        {
            "queue": 30,
            "attempted": 2,
            "available": 2,
            "unattempted": [f"track-{index}" for index in range(28)],
        },
        sample_limit=3,
    )

    assert result["unattempted"] == ["track-0", "track-1", "track-2"]
    assert result["unattempted_count"] == 28


def test_backfill_import_summary_keeps_only_a_review_sample() -> None:
    module = _module()

    result = module.compact_import_summary(
        {"count": 4, "imported": 4, "results": [{"row": index} for index in range(4)]},
        sample_limit=2,
    )

    assert result == {
        "count": 4,
        "imported": 4,
        "result_count": 4,
        "result_sample": [{"row": 0}, {"row": 1}],
        "results_truncated": True,
    }


def test_backfill_wrapper_accepts_an_explicit_musicdl_interpreter() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'parser.add_argument(\n        "--worker-python"' in source
    assert 'default=os.environ.get("MUSICDL_PYTHON", "python3")' in source
    assert "args.worker_python," in source


def test_backfill_wrapper_defaults_to_the_direct_exact_source_executor() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'choices=("direct", "claude")' in source
    assert 'default="direct"' in source
    assert '"executor": "direct"' in source
