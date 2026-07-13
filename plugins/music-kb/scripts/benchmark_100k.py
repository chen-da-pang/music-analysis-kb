#!/usr/bin/env python3
"""Repeatable synthetic 100k generic import and retrieval benchmark.

This script deliberately creates its data in a temporary directory by default.
It never reads production analyses and should not be pointed at a publisher
database.  Run it from ``plugins/music-kb`` with:

    uv run python scripts/benchmark_100k.py

Use ``--records`` for a smaller local smoke test.  Supplying ``--work-dir``
keeps the generated JSONL and SQLite database for inspection; that directory is
operational data and must remain outside Git.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import tempfile
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Callable

from music_kb.repository import MusicKBRepository, iter_import_file
from music_kb.schema import initialize_database


DEFAULT_RECORDS = 100_000
DEFAULT_BATCH_SIZE = 500
DEFAULT_REPETITIONS = 20


def synthetic_payload(index: int) -> dict[str, Any]:
    """Return one representative, deterministic generic import record."""

    tags = [
        {"namespace": "genre", "name": "electronic pop", "status": "approved"},
        {"namespace": "production", "name": "sidechain", "status": "approved"},
        {"namespace": "instrument", "name": f"synth texture {index % 40}", "status": "candidate"},
        {"namespace": "mood", "name": f"night drive mood {index % 25}", "status": "candidate"},
    ]
    if index % 997 == 0:
        tags.append(
            {"namespace": "production", "name": "rare reverse granular swell", "status": "candidate"}
        )
    return {
        "recording": {
            "id": f"benchmark-{index:06d}",
            "title": f"Benchmark Night Drive {index:06d}",
            "version_label": "synthetic",
        },
        "artists": [{"name": f"Benchmark Artist {index % 500}"}],
        "analysis": {
            "raw_text": (
                "Synthetic Music Flamingo analysis for local scale validation: "
                "electronic pop, sidechain pulse, granular vocal texture, and a night-drive arrangement. "
                f"Record marker {index:06d}."
            ),
            "summary": "Synthetic only; not a production analysis.",
            "quality_state": "passed",
        },
        "tags": tags,
        "numeric_features": [{"name": "bpm", "value": 96 + (index % 40), "unit": "bpm"}],
    }


def write_jsonl(path: Path, records: int) -> float:
    started = perf_counter()
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for index in range(records):
            handle.write(json.dumps(synthetic_payload(index), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    return perf_counter() - started


def timing_summary(operation: Callable[[], Any], *, repetitions: int) -> dict[str, float]:
    # One warm-up avoids reporting cache population as a steady-state query.
    operation()
    samples: list[float] = []
    for _ in range(repetitions):
        started = perf_counter()
        operation()
        samples.append((perf_counter() - started) * 1_000)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * 0.95)))
    return {
        "median_ms": round(median(samples), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def run_benchmark(work_dir: Path, *, records: int, batch_size: int, repetitions: int) -> dict[str, Any]:
    if records < 1:
        raise ValueError("--records must be positive")
    if repetitions < 1:
        raise ValueError("--repetitions must be positive")

    work_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = work_dir / "synthetic-analyses.jsonl"
    database_path = work_dir / "music-master.sqlite"
    if any(work_dir.iterdir()):
        raise ValueError(f"Benchmark work directory must be empty: {work_dir}")

    generation_seconds = write_jsonl(jsonl_path, records)
    initialize_database(database_path)
    with MusicKBRepository(database_path) as repository:
        started = perf_counter()
        imported = repository.import_analyses(iter_import_file(jsonl_path), batch_size=batch_size)
        import_seconds = perf_counter() - started
        validation = repository.validate()
        if not validation["valid"]:
            raise RuntimeError(f"Synthetic benchmark database failed validation: {validation}")

        probes = {
            "common_exact_tag": lambda: repository.search(tags=["sidechain"], limit=10),
            "rare_exact_tag": lambda: repository.search(tags=["rare reverse granular swell"], limit=10),
            "full_text": lambda: repository.search(query="granular vocal texture", limit=10),
            "title": lambda: repository.search(title=f"Benchmark Night Drive {records - 1:06d}", limit=10),
            "artist": lambda: repository.search(artist="Benchmark Artist 42", limit=10),
        }
        query_timings = {name: timing_summary(operation, repetitions=repetitions) for name, operation in probes.items()}

    # The last writer connection has closed, so SQLite has checkpointed the
    # WAL and this is the portable main database size that snapshot/rsync
    # operations will actually distribute.
    database_bytes = database_path.stat().st_size

    return {
        "dataset": {
            "records": records,
            "jsonl_bytes": jsonl_path.stat().st_size,
            "batch_size": batch_size,
            "query_repetitions": repetitions,
            "physical_lf_jsonl": True,
        },
        "timings": {
            "jsonl_generation_seconds": round(generation_seconds, 3),
            "import_and_single_fts_rebuild_seconds": round(import_seconds, 3),
            "queries": query_timings,
        },
        "database": {
            "bytes": database_bytes,
            "canonical_records": imported["canonical_count"],
            "search_projection_rebuilt": imported["search_projection_rebuilt"],
        },
        "runtime": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark music-kb generic import and read paths with synthetic data")
    parser.add_argument("--records", type=int, default=DEFAULT_RECORDS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Empty directory to retain generated benchmark artifacts; default uses an automatically removed temp directory",
    )
    args = parser.parse_args(argv)

    if args.work_dir is not None:
        result = run_benchmark(
            args.work_dir.resolve(),
            records=args.records,
            batch_size=args.batch_size,
            repetitions=args.repetitions,
        )
        result["artifacts_retained_at"] = str(args.work_dir.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="music-kb-benchmark-") as temporary:
            result = run_benchmark(
                Path(temporary),
                records=args.records,
                batch_size=args.batch_size,
                repetitions=args.repetitions,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
