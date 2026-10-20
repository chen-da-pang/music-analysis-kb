#!/usr/bin/env python3
"""Capture the versioned Kugou audience charts into one weekly run directory.

Reuses ``scripts/capture_kugou_chart.py`` (kugou-cli) for every page and writes
the normalized ``-songs.json`` / ``-raw.json`` / ``-manifest.json`` triple under
``<output-dir>`` (normally ``run_dir/audience-charts``).  The two popularity
charts (8888/6666) are not part of the audience profile: they stay in the
existing ``chart_capture`` atom.  The overlapping 52144 chart is reused from
``run_dir/charts`` when that capture already exists, otherwise it is captured
here once.

Raw chart snapshots stay on the publisher machine: they are never committed and
never enter the client snapshot.  This script never writes the download queue.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from music_kb.audience_charts import (
    AUDIENCE_CAPTURE_MANIFEST,
    DEFAULT_AUDIENCE_CHART_PROFILE,
    chart_capture_files,
    load_audience_chart_profile,
)
from music_kb.operation_context import atomic_write_json, load_validated_operations
from capture_kugou_chart import capture_chart


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def capture_audience_charts(
    *,
    run_id: str,
    output_dir: str | Path,
    profile_path: str | Path | None = None,
    reuse_directory: str | Path | None = None,
    rank_ids: Sequence[str] | None = None,
    page_size: int | None = None,
    max_pages: int | None = None,
    snapshot_date: str | None = None,
    kugou_bin: str = "kugou-cli",
    timeout_seconds: int = 120,
    proxy: str | None = None,
    dry_run: bool = False,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Capture every audience chart page and persist a machine-readable manifest."""

    profile = load_audience_chart_profile(profile_path)
    directory = Path(output_dir).expanduser().resolve()
    reuse_dir = Path(reuse_directory).expanduser().resolve() if reuse_directory else None
    selected = {str(rank_id) for rank_id in rank_ids} if rank_ids else None
    effective_size = int(page_size or profile["page_size"])
    effective_max_pages = int(max_pages or profile["max_pages"])
    if effective_size < 1 or effective_size > 500:
        raise ValueError("page_size must be between 1 and 500")
    if effective_max_pages < 1:
        raise ValueError("max_pages must be positive")
    resolved_snapshot_date = (
        str(snapshot_date).strip()
        if snapshot_date
        else datetime.now().astimezone().date().isoformat()
    )
    charts: list[dict[str, Any]] = []
    total_source_records = 0
    for chart in profile["charts"]:
        rank_id = str(chart["rank_id"])
        if selected is not None and rank_id not in selected:
            continue
        reused_from = None
        pages: list[dict[str, Any]] = []
        source_records = 0
        if chart["capture"] == "reuse" and reuse_dir is not None and not dry_run:
            existing = chart_capture_files(reuse_dir, rank_id)
            if existing:
                reused_from = str(reuse_dir)
                for path in existing:
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            f"existing reused chart capture is unreadable: {path}"
                        ) from exc
                    summary = payload.get("summary") if isinstance(payload, Mapping) else None
                    summary = summary if isinstance(summary, Mapping) else {}
                    pages.append(
                        {
                            "page": summary.get("page", 0),
                            "songs": str(path),
                            "source_records": int(summary.get("source_records", 0)),
                            "captured_at": summary.get("captured_at"),
                        }
                    )
                    source_records += int(summary.get("source_records", 0))
        if reused_from is None:
            page = 1
            while True:
                result = capture_chart(
                    run_id=run_id,
                    rank_id=rank_id,
                    page=page,
                    size=effective_size,
                    output_dir=directory,
                    kugou_bin=kugou_bin,
                    timeout_seconds=timeout_seconds,
                    proxy=proxy,
                    dry_run=dry_run,
                    runner=runner,
                )
                pages.append(
                    {
                        "page": page,
                        "songs": str(result["songs"]),
                        "source_records": int(result.get("source_records", 0)),
                        "captured_at": result.get("captured_at"),
                    }
                )
                source_records += int(result.get("source_records", 0))
                if dry_run:
                    break
                if int(result.get("source_records", 0)) <= 0:
                    break
                if int(result.get("source_records", 0)) < effective_size:
                    break
                page += 1
                if page > effective_max_pages:
                    raise RuntimeError(
                        "audience chart pagination exceeded max_pages="
                        f"{effective_max_pages}: rank_id={rank_id}"
                    )
        total_source_records += source_records
        charts.append(
            {
                "rank_id": rank_id,
                "chart_name": chart["chart_name"],
                "family": chart["family"],
                "capture": chart["capture"],
                "status": "reused" if reused_from else ("dry_run" if dry_run else "captured"),
                "reused_from": reused_from,
                "pages": [page["songs"] for page in pages],
                "page_count": len(pages),
                "source_records": source_records,
            }
        )
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "profile_id": profile.get("profile_id"),
        "profile": str(Path(profile_path).expanduser().resolve())
        if profile_path is not None
        else str(DEFAULT_AUDIENCE_CHART_PROFILE),
        "snapshot_date": resolved_snapshot_date,
        "dry_run": dry_run,
        "chart_count": len(charts),
        "captured_charts": sum(1 for chart in charts if chart["status"] == "captured"),
        "reused_charts": [chart["rank_id"] for chart in charts if chart["status"] == "reused"],
        "total_source_records": total_source_records,
        "charts": charts,
    }
    manifest_path = directory / AUDIENCE_CAPTURE_MANIFEST
    atomic_write_json(manifest_path, summary)
    return {**summary, "manifest": str(manifest_path)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_AUDIENCE_CHART_PROFILE)
    parser.add_argument(
        "--reuse-directory",
        type=Path,
        default=None,
        help="Existing run_dir/charts whose chart-<rank_id>-* captures may be reused (52144)",
    )
    parser.add_argument("--rank-id", action="append", default=[], help="Bounded single-chart check")
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--snapshot-date", default=None, help="YYYY-MM-DD; defaults to today")
    parser.add_argument("--kugou-bin", default="kugou-cli")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--proxy")
    parser.add_argument("--operations-file", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    load_validated_operations(args.operations_file, required_atom="audience_chart_capture")
    result = capture_audience_charts(
        run_id=args.run_id,
        output_dir=args.output_dir,
        profile_path=args.profile,
        reuse_directory=args.reuse_directory,
        rank_ids=args.rank_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
        snapshot_date=args.snapshot_date,
        kugou_bin=args.kugou_bin,
        timeout_seconds=args.timeout_seconds,
        proxy=args.proxy,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
