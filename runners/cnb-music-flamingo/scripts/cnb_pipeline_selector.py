#!/usr/bin/env python3
"""Select a CNB pipeline deterministically and reject ambiguous builds."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Mapping


class PipelineSelectionError(ValueError):
    """Raised when a pipeline selection is missing or ambiguous."""


def select_pipeline_id(pipelines: Mapping[str, object], requested_pipeline_id: str | None = None) -> str:
    ids = sorted(str(key) for key in pipelines)
    if requested_pipeline_id:
        if requested_pipeline_id not in pipelines:
            raise PipelineSelectionError(
                f"Requested pipeline_id={requested_pipeline_id!r} is not present. Available: {', '.join(ids) or '(none)'}"
            )
        return requested_pipeline_id
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise PipelineSelectionError("No pipeline id found in CNB build status.")
    raise PipelineSelectionError(
        "Build contains multiple pipelines; pass PIPELINE_ID explicitly. Available: " + ", ".join(ids)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-id")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        pipelines = payload.get("data", {}).get("pipelinesStatus", {})
        if not isinstance(pipelines, dict):
            raise PipelineSelectionError("CNB build status did not contain an object at data.pipelinesStatus.")
        print(select_pipeline_id(pipelines, args.pipeline_id))
        return 0
    except (json.JSONDecodeError, PipelineSelectionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
