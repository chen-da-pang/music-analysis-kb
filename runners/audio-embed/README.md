# Publisher audio embedding runner (#127)

Offline, read-only SHA256 matching against a **v8 publisher master**. This CLI
does not import vectors, migrate databases, publish snapshots, run weekly jobs,
or evaluate Suno. Never point import/migration commands at `current.sqlite`.
Model packages and weights stay outside the plugin and repository.

## Environment and recipes

The verified publisher interpreter is Python 3.12 at:

```sh
export EMBED_PY='/Users/wycm/Documents/克莱儿/.scratch/mir-song-description/bakeoff/venv-muq/bin/python'
export NUMBA_CACHE_DIR=/tmp/music-kb-numba
export HF_HUB_OFFLINE=1
```

It already provides `torch`, `muq`, `librosa`, `numpy`, `transformers` and
`huggingface_hub`. The plugin environment needs none of the model packages.
The default device is MPS; `--device cpu` is an explicit, slower alternative.
The runner forces offline mode. Provision/cache models beforehand, not during
an inference run. If external dependency provisioning requires network access:

```sh
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
```

| Channel | Model ID / factor | Recipe / dimension |
| --- | --- | --- |
| Overall | `muq-mulan-v3` / empty string | `OpenMuQ/MuQ-MuLan-large`, `v3`, 512d |
| Rhythm | `merit-mert330m-v1` / `rhythm` | `amaai-lab/merit`, `mert330m-l3-4-5-6-23-30s-v1`, 128d |
| Timbre | `merit-mert330m-v1` / `timbre` | Same model/recipe as rhythm, independent head, 128d |

- Overall: librosa decodes **the whole song**, 24 kHz mono float32. Exact 10s
  clips, tail wrapped from the beginning (no discarded samples); each clip is
  L2 normalized, then their mean is L2 normalized. Under-10s audio fails rather
  than silently changing the v3 recipe.
- MERIT: librosa decodes the first 30s at 24 kHz mono, truncates/zero-pads to
  720,000 samples, then uses the MERT feature extractor. Frozen MERT hidden
  states `(3,4,5,6,23)` are mean-pooled in time and concatenated (5120d).
  `head_rhy/best_head.pt` and `head_tim/best_head.pt` each project to an
  L2-normalized 128d vector. Both heads share one backbone forward per song.
  No melody vector is calculated or stored. `torchaudio.load` is not used.
- Cached revisions are pinned in `embed_audio.py`: MuQ
  `2e01c796b71dca71b45251384c04cd7b237c9020`, MERT
  `5240c2708a5acaee1007f43fb9735c7dcd0b78c9`, MERIT
  `a85df30eca1ba112eb594285f3ba1d96488e7883`.
  MERT's cached custom modeling code is executed with `trust_remote_code=True`;
  review that cache before provisioning a different revision.
- MuQ/MERT weights: CC-BY-NC-4.0; MERIT heads: MIT. Internal research only;
  obtain authorization or replace restricted weights before commercial use.

## Run and resume

Run from the repository/worktree root. Store artifacts **outside git**:

```sh
export MASTER="$HOME/.music-kb/music-master.sqlite"
export AUDIO_ROOT='/Users/wycm/Documents/网易云热榜抓取'
export OUT='/Users/wycm/Documents/网易云热榜抓取/research/music-kb-similarity/bootstrap'
mkdir -p "$OUT"

"$EMBED_PY" -u runners/audio-embed/embed_audio.py \
  --audio-root "$AUDIO_ROOT" --db "$MASTER" --output-dir "$OUT" \
  --channel overall > "$OUT/overall.log" 2>&1 &
overall_pid=$!

"$EMBED_PY" -u runners/audio-embed/embed_audio.py \
  --audio-root "$AUDIO_ROOT" --db "$MASTER" --output-dir "$OUT" \
  --channel merit > "$OUT/merit.log" 2>&1 &
merit_pid=$!

wait "$overall_pid"
wait "$merit_pid"
```

Keep the parent shell alive for background jobs (or use a persistent terminal
session). On memory-constrained hosts run the channels sequentially.
For restart, repeat the relevant invocation with `--resume` and append its log
using `>>`. Never run two writers for the same channel/output directory.

The recursive scan accepts case-insensitive `.flac/.mp3/.ogg/.m4a` files of at
least **1,000,000 bytes**. Unknown hashes are ignored; same-name files never
establish identity. Each matching recording is included once even if its audio
occurs at several paths. Only the first sorted matching path is used.
The master is opened with SQLite `mode=ro`; an absent database is not created.
Existing vectors are skipped by exact model ID + factor, after checking the
registered model's name/recipe/dimension/modality/metric.

Outputs are physical-LF `overall.jsonl`, `rhythm.jsonl`, `timbre.jsonl`, with
exact row shape:

```json
{"recording_id":"rec_...","audio_sha256":"...","vector_b64":"..."}
```

`vector_b64` is base64 of little-endian float32 (512 or 128 values). Rows are
validated, flushed and fsynced individually. `receipt-overall.json` and
`receipt-merit.json` record configuration, revisions, input identities, counts,
failures and output checksums; receipts are replaced atomically. Receipts use
UTC timestamps. A nonzero exit code and printed failure list signal failure.
Successful rows remain usable and failed recordings are retried on resume.
Resume validates all complete rows, skips completed IDs, and discards only a
non-LF-terminated trailing fragment after interruption. A malformed complete
row or changed configuration/identity fails loudly. Refusing fresh runs over
existing output prevents accidental overwrite. Use a new output directory to
recompute with a new recipe; change model ID/recipe together when changing the
embedding algorithm. Do not import while another publisher is modifying audio
identities; import revalidates every SHA and commits each batch atomically.

## Import (separate, explicit database writes)

First run `uv sync --all-groups` in `plugins/music-kb` (proxy variables above if
needed). Back up a v7 master with SQLite's backup API and verify integrity,
recording count and backup byte size **before** running:

```sh
plugins/music-kb/.venv/bin/music-kb init --db "$MASTER"
```

For this bootstrap, 390 historical NPZ vectors were matched by file SHA256 and
exported byte-for-byte as `reused-overall.jsonl`; the unmatched unreleased guest
track was excluded. Import that file first to avoid recomputing those songs:

```sh
export KB="$PWD/plugins/music-kb/.venv/bin/music-kb"
"$KB" --json import-embeddings --db "$MASTER" \
  --input "$OUT/reused-overall.jsonl" --model-id muq-mulan-v3 \
  --model-name OpenMuQ/MuQ-MuLan-large --recipe v3 --dim 512 \
  --metric cosine --license CC-BY-NC-4.0
```

After the runner completes, import its newly computed rows (44 overall and
434 per MERIT factor in the initial bootstrap):

```sh
"$KB" --json import-embeddings --db "$MASTER" \
  --input "$OUT/overall.jsonl" --model-id muq-mulan-v3 \
  --model-name OpenMuQ/MuQ-MuLan-large --recipe v3 --dim 512 \
  --metric cosine --license CC-BY-NC-4.0
for factor in rhythm timbre; do
  "$KB" --json import-embeddings --db "$MASTER" \
    --input "$OUT/$factor.jsonl" --model-id merit-mert330m-v1 \
    --model-name amaai-lab/merit --recipe mert330m-l3-4-5-6-23-30s-v1 \
    --dim 128 --factor "$factor" --metric cosine --license 'CC-BY-NC-4.0 (MERT); MIT (heads)'
done
"$KB" --json doctor --db "$MASTER"
"$KB" --json similar --db "$MASTER" --recording-id '<known recording ID>' --limit 10
```

Verify three channels have 434 rows each, 434/2005 recording coverage, no
integrity/embedding-validation errors, and 390 reused vectors remain exactly
equal to their original NPZ bytes. Preserve import receipts and a real Top-10
query outside the repository. Historical overall-distance baselines are mean
0.3033 globally and 0.0714 for 26 multi-version pairs: context, **not thresholds**
and not calibrated MERIT baselines. Top-10 distances naturally skew lower than
all-pair distances and do not prove listening quality.

## Model-free tests

```sh
cd plugins/music-kb
uv sync --all-groups
.venv/bin/python -m pytest -q tests/test_audio_embed_runner.py
.venv/bin/python -m pytest -q
```

The tests use fake embedders and explicitly block model imports.

## Initial bootstrap acceptance (2026-10-18 Asia/Shanghai)

- Master migrated v7 → v8; 2005 recordings preserved. The verified 67,563,520
  byte backup is
  `~/.music-kb/backups/music-master.pre-v8-20261017T185847Z.sqlite`.
  All 22 preexisting non-meta tables remain content-identical to that backup.
- 390 historical MuQ vectors were reused byte-for-byte, excluding the guest
  track; exactly 44 additional OGG recordings were computed. Both MERIT heads
  completed all 434 recordings. Total: 1302 rows, two registered models, three
  channels, coverage 434/2005. No inference failures.
- Real initial MPS runs completed in about 8m09s (MuQ) and 9m00s (MERIT), running
  concurrently. Resuming both channels wrote zero additional rows. The final
  pinned runner was also checked against one stored song: overall cosine
  1.000000, rhythm/timbre bit-identical.
- `doctor` reports the expected coverage; `validate` reports zero issues;
  SQLite integrity and foreign-key checks pass. The full plugin suite passes
  **406 tests** (370 existing + 36 model-free runner tests).
- Overall query seed `后来的我们`
  (`rec_kugou_ab956965cc25d2d3198143047d1859d5f35f0c2ee7675744e0b8a7ea204383e0`)
  returns distances 0.033, 0.035, 0.040, 0.041, 0.044, 0.047, 0.048, 0.048,
  0.050, 0.053. The 434-song all-pair mean is 0.303111, consistent in scale with
  the original 391-song mean independently recomputed as 0.303264. The historical
  26-pair multi-version mean 0.0714 remains reference context, not a threshold.
- Each channel contains 434 recording rows but 433 distinct vectors. Independent
  whole-song decoding confirmed that `你有没有真的爱过我` and its `合唱版` entry
  have different file SHA256s but identical decoded audio. Both original
  recording identities are intentionally retained; no records were merged or
  deleted. This is a source-library duplicate, not failed embedding coverage.
- All JSONL, manifests, receipts, three Top-10 query results and verification
  details are outside git under
  `/Users/wycm/Documents/网易云热榜抓取/research/music-kb-similarity/bootstrap/`.
  Key evidence: `verification.json`, `migration.json`, `receipt-*.initial.json`,
  `receipt-reused.json`, `final-runner-smoke.json`, `duplicate-audio-check.json`,
  `sanity-*.json`, `doctor.json`, `validate.json`, and `pytest-final.log`.
  No snapshots, MCP, weekly orchestration, Suno evaluation or pushes were done.
