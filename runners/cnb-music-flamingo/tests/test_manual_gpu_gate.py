#!/usr/bin/env python3
"""Unit tests for the pre-model shared-GPU allocation gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
