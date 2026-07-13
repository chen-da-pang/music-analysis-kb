from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "analysis.json"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "music_kb.cli", *arguments], text=True, capture_output=True, check=False
    )


def test_cli_json_lifecycle(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialized = run_cli("--json", "init", "--db", str(database))
    assert initialized.returncode == 0, initialized.stderr
    imported = run_cli("--json", "import-analysis", "--db", str(database), "--input", str(FIXTURE))
    assert imported.returncode == 0, imported.stderr
    searched = run_cli("--json", "search", "--db", str(database), "--tag", "颗粒人声切片")
    assert searched.returncode == 0, searched.stderr
    assert json.loads(searched.stdout)["result"]["count"] == 1
    valid = run_cli("--json", "validate", "--db", str(database))
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["result"]["valid"] is True


def _generic_payload(recording_id: str, *, title: str) -> dict[str, object]:
    return {
        "recording": {"id": recording_id, "title": title},
        "artists": [{"name": "批量导入测试艺人"}],
        "analysis": {
            "raw_text": "批量 JSONL 导入保留 U+2028 分隔符：第一段\u2028第二段。",
            "summary": "streaming CLI regression fixture",
            "quality_state": "passed",
        },
        "tags": [{"namespace": "production", "name": "streaming test tag"}],
    }


def test_cli_streams_jsonl_and_reports_bounded_summary(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialized = run_cli("--json", "init", "--db", str(database))
    assert initialized.returncode == 0, initialized.stderr

    source = tmp_path / "generic.ndjson"
    source.write_text(
        "\n".join(
            json.dumps(_generic_payload(f"rec_stream_{index}", title=f"stream {index}"), ensure_ascii=False)
            for index in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    imported = run_cli(
        "--json",
        "import-analysis",
        "--db",
        str(database),
        "--input",
        str(source),
        "--batch-size",
        "1",
    )
    assert imported.returncode == 0, imported.stderr
    result = json.loads(imported.stdout)["result"]
    assert {key: result[key] for key in result if key not in {"imports", "imports_returned", "imports_truncated"}} == {
        "count": 3,
        "created_count": 3,
        "idempotent_count": 0,
        "canonical_count": 3,
        "batch_size": 1,
        "batch_count": 3,
        "search_projection_rebuilt": True,
    }
    assert len(result["imports"]) == 3
    assert result["imports_returned"] == 3
    assert result["imports_truncated"] is False
    searched = run_cli("--json", "search", "--db", str(database), "--tag", "streaming test tag")
    assert searched.returncode == 0, searched.stderr
    assert json.loads(searched.stdout)["result"]["count"] == 3


def test_cli_rebuild_search_recovers_after_interrupted_jsonl_batch(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    assert run_cli("--json", "init", "--db", str(database)).returncode == 0
    source = tmp_path / "broken.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(_generic_payload("rec_recover", title="recover"), ensure_ascii=False),
                json.dumps({"recording": {"id": "rec_invalid"}, "artists": [{"name": "bad"}]}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    interrupted = run_cli(
        "--json",
        "import-analysis",
        "--db",
        str(database),
        "--input",
        str(source),
        "--batch-size",
        "1",
    )
    assert interrupted.returncode == 2
    assert "recording.title" in json.loads(interrupted.stderr)["error"]["message"]

    invalid = run_cli("--json", "validate", "--db", str(database))
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout)["result"]["valid"] is False
    assert any(
        issue["code"] == "search_projection_dirty"
        for issue in json.loads(invalid.stdout)["result"]["issues"]
    )

    rebuilt = run_cli("--json", "rebuild-search", "--db", str(database))
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert json.loads(rebuilt.stdout)["result"]["recording_count"] == 1
    valid = run_cli("--json", "validate", "--db", str(database))
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["result"]["valid"] is True


def test_doctor_reports_missing_default_database(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("MUSIC_KB_DB", str(missing))
    result = run_cli("--json", "doctor")
    assert result.returncode == 1
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is False
    assert parsed["result"]["ready"] is False
