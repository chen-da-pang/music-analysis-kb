#!/usr/bin/env python3
"""Single source of truth for persistent, run-scoped Music Flamingo outputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RunContextError(ValueError):
    """Raised when a run identity cannot safely be used as a filesystem component."""


@dataclass(frozen=True)
class RunContext:
    work_dir: Path
    output_name: str
    run_id: str
    run_dir: Path


def _clean_component(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RunContextError(f"{field} must not be empty")
    if text in {".", ".."} or not _COMPONENT_RE.fullmatch(text):
        raise RunContextError(
            f"{field} must contain only letters, digits, dot, underscore, or hyphen and must not contain a path"
        )
    return text


def resolve_run_context(
    env: Mapping[str, str] | None = None,
    *,
    default_work_dir: Path | None = None,
    default_output_name: str = "one_smoke",
) -> RunContext:
    values = os.environ if env is None else env
    if default_work_dir is None:
        default_work_dir = Path(__file__).resolve().parents[1] / "data/output/music_flamingo_pipeline"
    work_dir_text = str(values.get("WORK_DIR") or default_work_dir).strip()
    if not work_dir_text:
        raise RunContextError("WORK_DIR must not be empty")
    work_dir = Path(work_dir_text).expanduser()
    output_name = _clean_component(values.get("MUSIC_FLAMINGO_OUTPUT_NAME") or default_output_name, "MUSIC_FLAMINGO_OUTPUT_NAME")
    run_id = _clean_component(
        values.get("MUSIC_FLAMINGO_RUN_ID")
        or values.get("CNB_BUILD_ID")
        or values.get("CNB_PIPELINE_ID")
        or "local",
        "MUSIC_FLAMINGO_RUN_ID",
    )
    return RunContext(work_dir=work_dir, output_name=output_name, run_id=run_id, run_dir=work_dir / output_name / run_id)


def status_path(run_dir: Path) -> Path:
    return run_dir / "run_status.json"


def write_status(run_dir: Path, *, state: str, run_id: str, **fields: object) -> dict:
    if state not in {"running", "success", "failed"}:
        raise RunContextError(f"Unsupported run state: {state}")
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": _clean_component(run_id, "run_id"),
        "state": state,
        "updated_at_epoch_seconds": round(time.time(), 3),
        **fields,
    }
    target = status_path(run_dir)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=run_dir,
        prefix=".status-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    temporary_path.replace(target)
    return payload


def read_status(run_dir: Path) -> dict | None:
    target = status_path(run_dir)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"state": "invalid", "error": repr(exc), "status_path": str(target)}
    if not isinstance(payload, dict):
        return {"state": "invalid", "error": "run_status.json must contain an object", "status_path": str(target)}
    return payload


def _command_print_dir() -> int:
    context = resolve_run_context()
    print(context.run_dir)
    return 0


def _command_write_status(args: argparse.Namespace) -> int:
    context = resolve_run_context()
    payload: dict[str, object] = {}
    if args.pid is not None:
        payload["pid"] = args.pid
    if args.command_fragment:
        payload["command_fragment"] = args.command_fragment
    if args.exit_code is not None:
        payload["exit_code"] = args.exit_code
    if args.reason:
        payload["reason"] = args.reason
    result = write_status(context.run_dir, state=args.state, run_id=context.run_id, **payload)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("print-dir", help="print the canonical run directory")
    status_parser = subparsers.add_parser("write-status", help="atomically update run_status.json")
    status_parser.add_argument("--state", choices=("running", "success", "failed"), required=True)
    status_parser.add_argument("--pid", type=int)
    status_parser.add_argument("--command-fragment")
    status_parser.add_argument("--exit-code", type=int)
    status_parser.add_argument("--reason")
    args = parser.parse_args()
    if args.command == "print-dir":
        return _command_print_dir()
    return _command_write_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
