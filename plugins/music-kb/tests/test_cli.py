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


def test_doctor_reports_missing_default_database(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("MUSIC_KB_DB", str(missing))
    result = run_cli("--json", "doctor")
    assert result.returncode == 1
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is False
    assert parsed["result"]["ready"] is False
