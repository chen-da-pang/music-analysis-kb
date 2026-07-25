#!/usr/bin/env python3
"""Unit tests for the pre-model shared-GPU allocation gate."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class ManualGpuGateTests(unittest.TestCase):
    def test_l40_clean_snapshot_passes(self) -> None:
        from check_manual_gpu_gate import parse_gpu_query, validate_gpu_allocation

        snapshot = parse_gpu_query("NVIDIA L40, GPU-abc, 46068, 512, 45556, 0\n")
        result = validate_gpu_allocation(
            snapshot,
            expected_gpu="L40",
            minimum_free_mib=40_000,
            max_utilization_percent=0,
        )
        self.assertEqual(result["gpu_name"], "NVIDIA L40")
        self.assertEqual(result["memory_free_mib"], 45_556)

    def test_busy_or_wrong_gpu_is_rejected(self) -> None:
        from check_manual_gpu_gate import ManualGpuGateError, parse_gpu_query, validate_gpu_allocation

        busy = parse_gpu_query("NVIDIA L40, GPU-abc, 46068, 31461, 13994, 93\n")
        with self.assertRaisesRegex(ManualGpuGateError, "free memory"):
            validate_gpu_allocation(
                busy,
                expected_gpu="L40",
                minimum_free_mib=40_000,
                max_utilization_percent=0,
            )
        h20 = parse_gpu_query("NVIDIA H20, GPU-def, 97871, 1000, 96871, 0\n")
        with self.assertRaisesRegex(ManualGpuGateError, "model mismatch"):
            validate_gpu_allocation(
                h20,
                expected_gpu="L40",
                minimum_free_mib=40_000,
                max_utilization_percent=0,
            )

    def test_multiple_gpu_records_are_rejected_not_silently_ignored(self) -> None:
        from check_manual_gpu_gate import ManualGpuGateError, parse_gpu_query

        with self.assertRaisesRegex(ManualGpuGateError, "exactly one GPU"):
            parse_gpu_query(
                "NVIDIA L40, GPU-a, 46068, 0, 46068, 0\n"
                "NVIDIA L40, GPU-b, 46068, 0, 46068, 0\n"
            )

    def test_compute_process_query_preserves_pid_name_and_memory_only(self) -> None:
        from check_manual_gpu_gate import parse_compute_process_query

        self.assertEqual(
            parse_compute_process_query("4242, python, 16488\n5151, ffmpeg, 512\n"),
            [
                {"pid": 4242, "process_name": "python", "memory_used_mib": 16488},
                {"pid": 5151, "process_name": "ffmpeg", "memory_used_mib": 512},
            ],
        )
        self.assertEqual(parse_compute_process_query(""), [])
        self.assertEqual(parse_compute_process_query("No running compute processes found\n"), [])

    def test_compute_process_query_records_unavailable_without_opening_gate(self) -> None:
        from check_manual_gpu_gate import query_compute_processes

        with patch(
            "check_manual_gpu_gate.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["nvidia-smi"],
                returncode=13,
                stdout="",
                stderr="permission denied",
            ),
        ):
            evidence = query_compute_processes()

        self.assertEqual(evidence["status"], "unavailable")
        self.assertIn("exit 13", str(evidence["error"]))

    def test_busy_gate_receipt_keeps_snapshot_and_visible_processes(self) -> None:
        from check_manual_gpu_gate import main

        busy_snapshot = {
            "gpu_name": "NVIDIA L40",
            "gpu_uuid": "GPU-abc",
            "memory_total_mib": 46068,
            "memory_used_mib": 16488,
            "memory_free_mib": 29580,
            "utilization_percent": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "gate.json"
            with (
                patch("check_manual_gpu_gate.query_gpu", return_value=busy_snapshot),
                patch(
                    "check_manual_gpu_gate.query_compute_processes",
                    return_value={
                        "status": "available",
                        "processes": [
                            {
                                "pid": 4242,
                                "process_name": "python",
                                "memory_used_mib": 16488,
                            }
                        ],
                    },
                ),
            ):
                exit_code = main(
                    [
                        "--phase",
                        "before_hydrate",
                        "--expected-gpu",
                        "L40",
                        "--minimum-free-mib",
                        "40000",
                        "--receipt",
                        str(receipt),
                    ]
                )
            recorded = json.loads(receipt.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 2)
        self.assertEqual(recorded["status"], "fail")
        self.assertEqual(recorded["memory_free_mib"], 29580)
        self.assertEqual(recorded["compute_processes"]["status"], "available")
        self.assertEqual(recorded["compute_processes"]["processes"][0]["pid"], 4242)


if __name__ == "__main__":
    unittest.main()
