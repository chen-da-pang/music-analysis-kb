"""Lazy in-process MLX generation bridge.

Importing this module never requires mlx; calling :func:`default_generate_fn`
does, so it must run under the publisher's ``.venv-moss`` interpreter. The
module-level caches keep one model and processor resident per process so a
batch loop pays the load cost once.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ID = "mlx-community/MOSS-Music-8B-Thinking-8bit"

def _default_hf_cache_model() -> Path:
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")), "hub"
    )
    return Path(hub) / ("models--" + DEFAULT_MODEL_ID.replace("/", "--"))

DEFAULT_HF_CACHE_MODEL = _default_hf_cache_model()

MOSS_MLX_DIR = Path(
    os.environ.get(
        "MOSS_MLX_DIR",
        "~/Documents/ChatGPT/拆解 歌曲情绪/third_party/MOSS-Music-moss/mlx",
    )
).expanduser()

DEFAULT_TEMP = 1.0
DEFAULT_TOP_P = 0.8
DEFAULT_TOP_K = 50
DEFAULT_MAX_NEW_TOKENS = 1500

_model_cache: dict[str, Any] = {}
_processor_cache: dict[str, Any] = {}


def resolve_model_path(model_path: str | Path | None = None) -> str:
    """Resolve the converted MLX model directory.

    The HF cache model directory holds exactly one ``snapshots/<rev>/``
    checkout; ``load_pretrained`` needs that concrete directory, not the
    cache root or a repo id.
    """

    if model_path is not None:
        resolved = Path(model_path).expanduser()
        if resolved.name == "snapshots":
            snapshots = sorted(resolved.iterdir())
            if len(snapshots) != 1:
                raise RuntimeError(f"expected exactly one snapshot under {resolved}")
            resolved = snapshots[0]
        return str(resolved)
    snapshots_dir = DEFAULT_HF_CACHE_MODEL / "snapshots"
    snapshots = sorted(snapshots_dir.iterdir()) if snapshots_dir.is_dir() else []
    if len(snapshots) != 1:
        raise RuntimeError(
            "could not resolve the MOSS-Music MLX snapshot; pass an explicit model directory"
        )
    return str(snapshots[0])


def default_generate_fn(
    audio_path: str,
    model_path: str,
    prompt: str,
    *,
    temp: float = DEFAULT_TEMP,
    top_p: float = DEFAULT_TOP_P,
    top_k: int = DEFAULT_TOP_K,
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
) -> tuple[str, int, float]:
    """Generate one analysis; returns (raw text, token count, elapsed seconds)."""

    import mlx.core  # noqa: F401  (fails fast outside .venv-moss)

    if str(MOSS_MLX_DIR) not in sys.path:
        sys.path.insert(0, str(MOSS_MLX_DIR))
    from moss_music_mlx.convert import load_pretrained
    from moss_music_mlx.generate import stream_generate

    model = _load_model(load_pretrained, model_path)
    processor = _load_processor(model_path)

    tokens = [0]
    pieces: list[str] = []
    start = time.time()
    for piece in stream_generate(
        model,
        processor,
        prompt,
        audio_path,
        max_new_tokens,
        temp,
        top_p,
        top_k,
        on_token=lambda _t: tokens.__setitem__(0, tokens[0] + 1),
    ):
        pieces.append(piece)
    elapsed = time.time() - start
    return "".join(pieces), tokens[0], elapsed


def _load_model(load_pretrained, model_path: str):
    cached = _model_cache.get(model_path)
    if cached is None:
        cached = load_pretrained(model_path)
        _model_cache[model_path] = cached
    return cached


def _load_processor(model_path: str):
    cached = _processor_cache.get(model_path)
    if cached is None:
        from src.processing_moss_music import MossMusicProcessor

        cached = MossMusicProcessor.from_pretrained(
            model_path, trust_remote_code=True, enable_time_marker=True
        )
        _processor_cache[model_path] = cached
    return cached
