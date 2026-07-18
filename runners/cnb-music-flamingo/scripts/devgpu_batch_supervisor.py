#!/usr/bin/env python3
"""Signal-safe supervisor for a Music Flamingo Dev GPU batch."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from music_flamingo_run_context import resolve_run_context, write_status


class BatchSupervisor:
    def __init__(self) -> None:
        self.context = resolve_run_context(default_output_name="devgpu_batch")
        self.process: subprocess.Popen[str] | None = None
        self.interrupted_signal: signal.Signals | None = None

    def _handle_signal(self, signum: int, _frame) -> None:
        self.interrupted_signal = signal.Signals(signum)
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _command(self) -> list[str]:
        return [
            "bash",
            "-lc",
            """set -eu
printf '%s\\n' '== Music Flamingo Dev GPU batch =='
date
printf 'run_dir=%s\\n' "$MUSIC_FLAMINGO_RUN_DIR"
printf 'run_id=%s\\n' "$MUSIC_FLAMINGO_RUN_ID"
printf 'audio_root=%s\\n' "$AUDIO_ROOT"
printf 'batch_limit=%s\\n' "$MUSIC_FLAMINGO_BATCH_LIMIT"
printf 'max_new_tokens=%s\\n' "$MUSIC_FLAMINGO_MAX_NEW_TOKENS"
printf 'clip_seconds=%s\\n' "$MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS"
printf 'runtime_image=%s\\n' "$CNB_RUNTIME_IMAGE"
printf '\\n'
bash scripts/check_remote_env.sh
printf '\\n'
python scripts/run_music_flamingo_batch.py
""",
        ]

    def run(self) -> int:
        run_dir = self.context.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        for legacy_name in ("job.pid", "exit_code.txt"):
            (run_dir / legacy_name).unlink(missing_ok=True)
        write_status(
            run_dir,
            state="running",
            run_id=self.context.run_id,
            pid=os.getpid(),
            command_fragment="devgpu_batch_supervisor.py",
        )
        (run_dir / "run_status_start.json").write_text(
            (run_dir / "run_status.json").read_text(encoding="utf-8"), encoding="utf-8"
        )

        env = os.environ.copy()
        env["MUSIC_FLAMINGO_RUN_DIR"] = str(run_dir)
        exit_code = 1
        reason: str | None = None
        try:
            with (run_dir / "run.log").open("w", encoding="utf-8") as log:
                self.process = subprocess.Popen(
                    self._command(),
                    cwd=Path(__file__).resolve().parents[1],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log.write(line)
                    log.flush()
                exit_code = self.process.wait()
            if self.interrupted_signal is not None:
                reason = f"interrupted:{self.interrupted_signal.name.removeprefix('SIG')}"
                exit_code = 128 + int(self.interrupted_signal)
        except Exception as exc:
            reason = f"supervisor_error:{exc!r}"
            exit_code = 1
        finally:
            state = "success" if exit_code == 0 and reason is None else "failed"
            payload = write_status(
                run_dir,
                state=state,
                run_id=self.context.run_id,
                pid=os.getpid(),
                exit_code=exit_code,
                **({"reason": reason} if reason else {}),
            )
            (run_dir / "run_status_final.json").write_text(
                __import__("json").dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return exit_code


def main() -> int:
    supervisor = BatchSupervisor()
    signal.signal(signal.SIGINT, supervisor._handle_signal)
    signal.signal(signal.SIGTERM, supervisor._handle_signal)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
