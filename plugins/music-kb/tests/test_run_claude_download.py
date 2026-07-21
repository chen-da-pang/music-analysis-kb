from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_claude_download.py"


def _module():
    spec = importlib.util.spec_from_file_location("run_claude_download", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(identity: str) -> dict[str, str]:
    return {"identity_key": identity, "title": identity, "artist": "Artist"}


def test_pending_chunks_are_serial_and_skip_terminal_resume_rows() -> None:
    module = _module()
    rows = [_row(f"kugou:{index}") for index in range(1, 8)]
    progress = {
        "results": {
            "kugou:1": {"status": "downloaded"},
            "kugou:2": {"status": "no_results"},
            "kugou:3": {"status": "failed"},
        }
    }

    chunks = module.pending_chunks(rows, progress, chunk_size=2, max_items=None)

    assert [[module.row_identity(row) for row in chunk] for chunk in chunks] == [
        ["kugou:4", "kugou:5"],
        ["kugou:6", "kugou:7"],
    ]


def test_aggregate_progress_requires_every_original_queue_identity() -> None:
    module = _module()
    rows = [_row("kugou:1"), _row("kugou:2"), _row("kugou:3"), _row("kugou:4")]
    progress = {
        "results": {
            "kugou:1": {"status": "downloaded"},
            "kugou:2": {"status": "skipped_existing"},
            "kugou:3": {"status": "no_results"},
        }
    }

    result = module.aggregate_progress(rows, progress)

    assert result == {
        "queue": 4,
        "downloaded": 1,
        "skipped_existing": 1,
        "failed": 0,
        "no_results": 1,
        "unresolved": ["kugou:4"],
    }


def test_max_items_caps_pending_rows_without_changing_chunk_size() -> None:
    module = _module()
    rows = [_row(f"kugou:{index}") for index in range(1, 10)]

    chunks = module.pending_chunks(rows, {"results": {}}, chunk_size=3, max_items=5)

    assert [len(chunk) for chunk in chunks] == [3, 2]


def test_stale_downloaded_progress_is_retried_when_inventory_file_is_missing(tmp_path: Path) -> None:
    module = _module()
    row = _row("kugou:stale")
    progress = {"results": {"kugou:stale": {"status": "downloaded"}}}
    inventory = {"songs": [{"identity_key": "kugou:stale", "download": {"status": "missing", "path": "old.mp3"}}]}

    chunks = module.pending_chunks(
        [row], progress, chunk_size=25, max_items=None, inventory=inventory, audio_root=tmp_path
    )

    assert [[module.row_identity(item) for item in chunk] for chunk in chunks] == [["kugou:stale"]]
