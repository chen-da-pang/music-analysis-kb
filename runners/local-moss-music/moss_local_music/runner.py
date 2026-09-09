"""Single-song end-to-end runner: analyze one track and write audit artifacts.

Pure functions (prompt building, think/answer splitting) run under any
python3; the default generator is lazy (see generate_bridge).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .generate_bridge import (
    DEFAULT_MAX_NEW_TOKENS,
    DEFAULT_TEMP,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
    resolve_model_path,
)
from .prompt import build_prompt

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

SIDECAR_SCHEMA_VERSION = 1

GenerateFn = Callable[..., tuple[str, int, float]]


def split_answer(raw_text: str) -> tuple[str, str]:
    """Return (answer, thinking); a missing or unterminated think block
    never drops text."""

    text = raw_text.strip()
    if THINK_OPEN in text and THINK_CLOSE in text:
        _, rest = text.split(THINK_OPEN, 1)
        think, _, answer = rest.partition(THINK_CLOSE)
        return answer.strip(), think.strip()
    return text, ""


def run_single(
    audio_path: str,
    out_dir: str | Path,
    *,
    model_path: str | None = None,
    run_id: str,
    generate_fn: Optional[GenerateFn] = None,
    temp: float = DEFAULT_TEMP,
    top_p: float = DEFAULT_TOP_P,
    top_k: int = DEFAULT_TOP_K,
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
) -> dict[str, Any]:
    """Analyze one song end to end and write three artifacts:
    ``<item>.answer.txt`` (think-stripped analysis), ``<item>.raw.md`` (full
    text including thinking), ``<item>.sidecar.json`` (stats, answer text and
    audit fields). Returns the sidecar dict."""

    prompt = build_prompt()
    gen = generate_fn or _default_generate
    resolved_model = resolve_model_path(model_path)
    raw_text, token_count, elapsed = gen(
        audio_path,
        resolved_model,
        prompt,
        temp=temp,
        top_p=top_p,
        top_k=top_k,
        max_new_tokens=max_new_tokens,
    )
    answer, thinking = split_answer(raw_text)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    item_id = Path(audio_path).stem
    answer_path = out / f"{item_id}.answer.txt"
    raw_path = out / f"{item_id}.raw.md"
    sidecar_path = out / f"{item_id}.sidecar.json"
    answer_path.write_text(answer + "\n", encoding="utf-8")
    raw_path.write_text(
        raw_text if raw_text.endswith("\n") else raw_text + "\n", encoding="utf-8"
    )

    sidecar: dict[str, Any] = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "run_id": run_id,
        "item_id": item_id,
        "audio_path": str(Path(audio_path).resolve()),
        "model_path": resolved_model,
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "generation_controls": {
            "temp": temp,
            "top_p": top_p,
            "top_k": top_k,
            "max_new_tokens": max_new_tokens,
        },
        "generated_token_count": token_count,
        "elapsed_seconds": round(elapsed, 1),
        "generation_rate_tok_s": round(token_count / max(elapsed, 1e-9), 1),
        "think_present": bool(thinking),
        "thinking": thinking,
        "answer": answer,
        "answer_path": str(answer_path),
        "raw_path": str(raw_path),
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    sidecar_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return sidecar


def _default_generate(audio_path: str, model_path: str, prompt: str, **kwargs):
    from .generate_bridge import default_generate_fn

    return default_generate_fn(audio_path, model_path, prompt, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default=None, help="converted MLX model dir (defaults to the HF cache snapshot)")
    parser.add_argument("--temp", type=float, default=DEFAULT_TEMP)
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    args = parser.parse_args()

    sidecar = run_single(
        args.audio,
        args.out,
        model_path=args.model,
        run_id=args.run_id,
        temp=args.temp,
        top_p=args.top_p,
        top_k=args.top_k,
        max_new_tokens=args.max_new_tokens,
    )
    print(
        json.dumps(
            {
                "item_id": sidecar["item_id"],
                "generated_token_count": sidecar["generated_token_count"],
                "elapsed_seconds": sidecar["elapsed_seconds"],
                "generation_rate_tok_s": sidecar["generation_rate_tok_s"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
