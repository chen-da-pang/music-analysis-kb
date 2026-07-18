#!/usr/bin/env python3
import html
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]

from music_flamingo_run_context import read_status, resolve_run_context


def run_dir_from_environment() -> Path:
    return resolve_run_context(
        default_work_dir=ROOT / "data/output/music_flamingo_pipeline",
        default_output_name="devgpu_batch",
    ).run_dir


RUN_DIR = run_dir_from_environment()


def read_text(path: Path, limit: int = 120_000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace")


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": repr(exc)}


def nvidia_smi() -> str:
    try:
        return subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        ).stdout.strip()
    except Exception as exc:
        return repr(exc)


def _pid_is_current(pid: object, command_fragment: object) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    proc_dir = Path(f"/proc/{pid_int}")
    if not proc_dir.exists():
        return False
    fragment = str(command_fragment or "").strip()
    if not fragment:
        return True
    try:
        cmdline = (proc_dir / "cmdline").read_text(encoding="utf-8", errors="replace").replace("\x00", " ")
    except OSError:
        return False
    return fragment in cmdline


def job_status(run_dir: Path | None = None) -> dict:
    target_dir = RUN_DIR if run_dir is None else run_dir
    stored = read_status(target_dir)
    if stored is None:
        return {
            "run_dir": str(target_dir),
            "state": "not_started",
            "running": False,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    result = {"run_dir": str(target_dir), **stored}
    if stored.get("state") == "running":
        result["running"] = _pid_is_current(stored.get("pid"), stored.get("command_fragment"))
        # An old exit_code.txt must never be combined with a new running status.
        result.pop("exit_code", None)
        if not result["running"]:
            result["state"] = "stale"
            result["reason"] = "recorded running PID is no longer current"
    else:
        result["running"] = False
    result["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result

def snapshot() -> dict:
    return {
        "status": job_status(),
        "gpu": nvidia_smi(),
        "batch_report": read_json(RUN_DIR / "batch_report.json"),
        "progress_tail": read_text(RUN_DIR / "progress.jsonl"),
        "run_log_tail": read_text(RUN_DIR / "run.log"),
        "stderr_tail": read_text(RUN_DIR / "stderr.txt"),
    }


def page() -> bytes:
    data = snapshot()
    body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>Music Flamingo Dev GPU Logs</title>
  <style>
    body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 24px; background: #111; color: #eee; }}
    h1, h2 {{ font-family: system-ui, sans-serif; }}
    pre {{ white-space: pre-wrap; background: #1d1d1d; border: 1px solid #333; padding: 12px; overflow: auto; }}
    .ok {{ color: #8fe388; }}
    .bad {{ color: #ff8b8b; }}
  </style>
</head>
<body>
  <h1>Music Flamingo Dev GPU Logs</h1>
  <p>Refreshes every 10 seconds. Run directory: <code>{html.escape(data["status"]["run_dir"])}</code></p>
  <h2>Status</h2>
  <pre>{html.escape(json.dumps(data["status"], ensure_ascii=False, indent=2))}</pre>
  <h2>GPU</h2>
  <pre>{html.escape(data["gpu"])}</pre>
  <h2>Batch Report</h2>
  <pre>{html.escape(json.dumps(data["batch_report"], ensure_ascii=False, indent=2))}</pre>
  <h2>Progress JSONL Tail</h2>
  <pre>{html.escape(data["progress_tail"])}</pre>
  <h2>Run Log Tail</h2>
  <pre>{html.escape(data["run_log_tail"])}</pre>
  <h2>stderr Tail</h2>
  <pre>{html.escape(data["stderr_tail"])}</pre>
</body>
</html>
"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            payload = json.dumps(snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8686"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Music Flamingo log server listening on 0.0.0.0:{port}", flush=True)
    print(f"Run directory: {RUN_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
