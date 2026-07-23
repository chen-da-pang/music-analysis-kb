#!/usr/bin/env python3
"""Record and enforce a clean GPU allocation before manual model loading."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence


class ManualGpuGateError(ValueError):
    """Raised when a shared GPU allocation is not safe to use."""


_QUERY = "name,uuid,memory.total,memory.used,memory.free,utilization.gpu"


def _parse_nonnegative_integer(value: str, *, field: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ManualGpuGateError(f"{field} is not an integer: {value!r}") from exc
    if parsed < 0:
        raise ManualGpuGateError(f"{field} must not be negative: {parsed}")
    return parsed


def parse_gpu_query(output: str) -> dict[str, object]:
    """Parse one ``nvidia-smi --format=csv,noheader,nounits`` GPU record."""
    rows = [row for row in csv.reader(line for line in output.splitlines() if line.strip())]
    if len(rows) != 1:
        raise ManualGpuGateError(f"Expected exactly one GPU record, received {len(rows)}")
    row = [column.strip() for column in rows[0]]
    if len(row) != 6:
        raise ManualGpuGateError(f"Expected six GPU fields, received {len(row)}")
    name, uuid, total, used, free, utilization = row
    if not name or not uuid:
        raise ManualGpuGateError("GPU name and UUID must not be empty")
    return {
        "gpu_name": name,
        "gpu_uuid": uuid,
        "memory_total_mib": _parse_nonnegative_integer(total, field="memory.total"),
        "memory_used_mib": _parse_nonnegative_integer(used, field="memory.used"),
        "memory_free_mib": _parse_nonnegative_integer(free, field="memory.free"),
        "utilization_percent": _parse_nonnegative_integer(utilization, field="utilization.gpu"),
    }


def query_gpu() -> dict[str, object]:
    result = subprocess.run(
        ["nvidia-smi", f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ManualGpuGateError(f"nvidia-smi query failed with exit {result.returncode}: {detail}")
    return parse_gpu_query(result.stdout)


def validate_gpu_allocation(
    snapshot: dict[str, object],
    *,
    expected_gpu: str,
    minimum_free_mib: int,
    max_utilization_percent: int,
) -> dict[str, object]:
    """Reject a wrong or externally busy GPU before importing the model."""
    expected = str(expected_gpu or "").strip().lower()
    if not expected:
        raise ManualGpuGateError("expected_gpu must not be empty")
    if minimum_free_mib < 1:
        raise ManualGpuGateError("minimum_free_mib must be positive")
    if not 0 <= max_utilization_percent <= 100:
        raise ManualGpuGateError("max_utilization_percent must be between 0 and 100")
    name = str(snapshot.get("gpu_name") or "")
    if expected not in name.lower():
        raise ManualGpuGateError(f"GPU model mismatch: expected {expected_gpu!r}, received {name!r}")
    free_mib = snapshot.get("memory_free_mib")
    utilization = snapshot.get("utilization_percent")
    if not isinstance(free_mib, int) or not isinstance(utilization, int):
        raise ManualGpuGateError("GPU snapshot is missing parsed free memory or utilization")
    if free_mib < minimum_free_mib:
        raise ManualGpuGateError(
            f"GPU free memory {free_mib} MiB is below required {minimum_free_mib} MiB"
        )
    if utilization > max_utilization_percent:
        raise ManualGpuGateError(
            f"GPU utilization {utilization}% exceeds allowed {max_utilization_percent}%"
        )
    return snapshot


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--expected-gpu", required=True)
    parser.add_argument("--minimum-free-mib", type=int, required=True)
    parser.add_argument("--max-utilization-percent", type=int, default=0)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result: dict[str, object] = {
        "schema_version": 1,
        "phase": args.phase,
        "expected_gpu": args.expected_gpu,
        "minimum_free_mib": args.minimum_free_mib,
        "max_utilization_percent": args.max_utilization_percent,
        "checked_at_epoch_seconds": round(time.time(), 3),
    }
    try:
        snapshot = validate_gpu_allocation(
            query_gpu(),
            expected_gpu=args.expected_gpu,
            minimum_free_mib=args.minimum_free_mib,
            max_utilization_percent=args.max_utilization_percent,
        )
        result.update({"status": "pass", **snapshot})
    except (ManualGpuGateError, OSError) as exc:
        result.update({"status": "fail", "error": str(exc)})
    _atomic_write_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
