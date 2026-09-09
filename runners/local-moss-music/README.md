# Local MOSS-Music runner

Local Apple Silicon analysis runtime replacing the retired CNB Music Flamingo
campaign (ADR-0001). Runs MOSS-Music-8B-Thinking (MLX 8-bit) on whole songs —
one five-dimension analysis per track, no segmentation — and adapts the output
into the canonical delivery JSONL consumed by `weekly-run --delivery`.

The model weights stay in the HuggingFace cache and never enter this
repository. Only code, prompts, and tests live here.

## Layout

- `moss_local_music/prompt.py` — the constrained five-dimension prompt
  (official question skeleton + output-format constraints the deterministic
  tag parser depends on: `###` section headings, `NN bpm`, `X major/minor`,
  no title/artist guessing, lyric quotes only in the lyrics-theme section).
- `moss_local_music/generate_bridge.py` — lazy in-process MLX generation.
  Importing it does not require mlx; calling the generator does, so it must
  run under the publisher's `.venv-moss` interpreter.
- `moss_local_music/runner.py` — single-song end-to-end: raw text (with
  thinking) → audit sidecar + think-stripped answer + raw transcript.
- `moss_local_music/delivery.py` — canonical delivery row builder. `contract`
  is `local-moss-music-v1`, `canonical_source` is `local-moss-music`; both are
  producer conventions consumed as opaque text by the importer.

## Model resolution

The generator resolves the 8-bit snapshot from the HuggingFace cache
(`mlx-community/MOSS-Music-8B-Thinking-8bit`); an explicit model directory
overrides it. Sampling is fixed to the verified profile: temp 1.0 /
top_p 0.8 / top_k 50, `max_new_tokens` 1500 (greedy decoding dead-loops on
this Thinking model).

## Tests

No test loads the real model; the generator is injected as a stub at the
model-call seam, and the delivery/tagging assertions run against the existing
plugin validator and parser.

```bash
cd plugins/music-kb && uv run pytest ../../runners/local-moss-music/tests -q
```

## Status

Covers ticket #113 (vendor + delivery adapter). Batch queueing, resume, and
events/receipt conventions are ticket #114; weekly-run atomization is #117.
