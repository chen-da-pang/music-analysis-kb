#!/usr/bin/env python3
"""Regression tests for the final 12-song quality-rerun audit."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def ledger_record(item_id: str, output_text: str, *, generated_token_count: int = 900) -> dict[str, object]:
    return {
        "attempt_id": "attempt-1",
        "id": item_id,
        "status": "success",
        "max_new_tokens": 1400,
        "generated_token_count": generated_token_count,
        "generation_controls": {"repetition_penalty": 1.08, "no_repeat_ngram_size": 4},
        "output_text": output_text,
        "output_text_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
    }


class QualityRerunAuditTests(unittest.TestCase):
    def write_inputs(self, root: Path) -> tuple[Path, Path]:
        manifest = root / "manifest.jsonl"
        rows = [
            {"id": "song-a", "title": "A", "artist": "Artist A"},
            {"id": "song-b", "title": "B", "artist": "Artist B"},
            {"id": "song-c", "title": "C", "artist": "Artist C"},
        ]
        manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        selection = root / "selection.txt"
        selection.write_text("1\n3\n", encoding="utf-8")
        return manifest, selection

    def test_audit_accepts_complete_non_cap_attempt(self) -> None:
        from audit_kugou_quality_rerun import audit_quality_rerun

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, selection = self.write_inputs(root)
            ledger = root / "ledger.jsonl"
            output_a = ("Detailed audible analysis of arrangement, vocal placement, production depth, rhythm, and emotional arc. " * 6).strip() + ".\n"
            output_c = ("Independent analysis of tempo, timbre, structure, tonal color, mix balance, and scene use case. " * 6).strip() + ".\n"
            ledger.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in (ledger_record("song-a", output_a), ledger_record("song-c", output_c))
                )
                + "\n",
                encoding="utf-8",
            )

            report = audit_quality_rerun(
                source_manifest=manifest,
                selection_file=selection,
                ledger_path=ledger,
                attempt_id="attempt-1",
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["observed_item_count"], 2)
            self.assertEqual(report["failures"], [])

    def test_audit_rejects_token_cap_and_repeating_tail(self) -> None:
        from audit_kugou_quality_rerun import audit_quality_rerun

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, selection = self.write_inputs(root)
            ledger = root / "ledger.jsonl"
            good = ("Detailed audible analysis of arrangement, vocal placement, production depth, rhythm, and emotional arc. " * 6).strip() + ".\n"
            repeated = ("ordinary introduction. " * 20) + ("repeating terminal description phrase " * 3)
            ledger.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in (
                        ledger_record("song-a", good, generated_token_count=1400),
                        ledger_record("song-c", repeated),
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = audit_quality_rerun(
                source_manifest=manifest,
                selection_file=selection,
                ledger_path=ledger,
                attempt_id="attempt-1",
            )
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("token cap reached" in failure for failure in report["failures"]))
            self.assertTrue(any("repeated terminal tail" in failure for failure in report["failures"]))
