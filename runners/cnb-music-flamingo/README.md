# CNB Music Flamingo Runner

> This directory is the GitHub-owned source tree for the GPU runtime. GitHub
> `chen-da-pang/music-analysis-kb` is the only source of truth for this code,
> its tests, and its operational documentation. CNB is a disposable execution
> mirror: it receives a pinned export of this subtree, temporary audio and
> ledgers for one run, and nothing that must be retained as project history.

## One-way GitHub-to-CNB export

After merging a change, export the exact GitHub commit with the deterministic
tool from the repository root:

```bash
git fetch origin main
python runners/cnb-music-flamingo/tools/export_cnb_runtime.py \
  --github-commit "$(git rev-parse HEAD)" \
  --output /tmp/cnb-music-flamingo-runtime
```

The tool writes `.github-source.json` containing the source repository, pinned
commit, subtree, and SHA-256 for every exported file. It copies only the runner
allowlist (`.cnb.yml`, `.cnb/`, `.dockerignore`, `.gitignore`, `README.md`,
`config/`, `scripts/`, and `tests/`). It fails closed on audio, `data/`, cache,
ledger, canonical-delivery, quality-run, database, model, or oversized files.
The resulting directory is the complete code-only CNB checkout; do not merge
CNB commits back into GitHub.

Runtime inputs, outputs, ledgers, canonical deliveries, raw analyses, and run
receipts belong to the local publisher or a temporary disposable CNB campaign
repository. They are not
fixtures and must never be added under this subtree.

The exported runner performs final-quality Music Flamingo audio analysis on CNB. It preserves
**`MUSIC_FLAMINGO_MAX_NEW_TOKENS=2048`** and
**`MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS=240`** for normal runs: this project does not
use a shortened fast-screening mode.

MOSS-Music is retired for this project; all current runtime entries use Music
Flamingo.

## Runtime strategy

All inference pipelines use one reviewed, immutable CNB Docker Artifact digest:

```text
docker.cnb.cool/wuyoumusic/moss-music-runner@sha256:a04cdbc02ef0f0958282e7bbf8c3a15b3a3105f4d17c95db88c98d1fc5f3657b
```

The image contains the CUDA/PyTorch runtime, FFmpeg, the Music Flamingo-compatible
Transformers fork, pinned direct Python dependencies, and
`nvidia/music-flamingo-think-2601-hf` revision `1ea2109` under `/opt/models/`.
Normal inference loads those local files; it does not install packages or download
weights during a run.

### Candidate image builds and promotion

`api_trigger_model_image` no longer overwrites the production tag. It builds and
pushes a unique candidate tag:

```text
music-flamingo-cuda-model-candidate-${CNB_BUILD_ID}
```

The build logs emit `candidate_image_digest`, source commit, model name, and model
revision. Verify a candidate through `api_trigger_verify_candidate_image` by passing
that immutable digest as `MUSIC_FLAMINGO_CANDIDATE_IMAGE`; it has the same ten-song
run evidence attachment path but never changes production. Promotion is then an
explicit, reviewed update of the immutable digest in `.cnb.yml`. This retains a
rollback point and prevents a failed rebuild from silently changing production runs.

The Docker build context is allowlisted by `.dockerignore`. It contains only
`.cnb/Dockerfile.flamingo` and `.cnb/requirements.runtime.txt`, not `.git`, input
songs, generated output, `.env`, or local caches. The base image and direct Python
dependencies are pinned; the promoted artifact digest is the reproducibility boundary
for production. A candidate rebuild can still vary in unpinned OS/transitive package
inputs, so candidate promotion always requires a fresh verification run.

## CNB pipeline contracts

Every inference event (`api_trigger`, the official smoke, `api_trigger_batch10`,
`api_trigger_devgpu_batch10`, and `vscode`) now has the following invariants:

- uses the same immutable runtime image digest;
- keeps `2048` max-new-tokens and a `240` second audio ceiling;
- mounts `music-flamingo-output` and writes into
  `/workspace/cache/output/music_flamingo_pipeline`;
- uses `${CNB_BUILD_ID}` as the run identity;
- serializes output writers with the CNB lock key `music-flamingo-output-writer`, with a 15,000 second wait/expiry window that covers the four-hour GPU ceiling plus teardown headroom.

CNB creates a run-scoped directory:

```text
${WORK_DIR}/${MUSIC_FLAMINGO_OUTPUT_NAME}/${CNB_BUILD_ID}/
```

A retry or another pipeline therefore cannot overwrite the previous run's
`batch_report.json`, `progress.jsonl`, `run.log`, or status file. For manual reruns
inside the same workspace, set a new `MUSIC_FLAMINGO_RUN_ID` before launching the
batch.

## Dev GPU batch workspace

Use `api_trigger_devgpu_batch10` for the observable ten-song verification run. It
starts the log viewer **and automatically runs exactly one ten-song batch**. Do not
run `bash scripts/devgpu_run_batch.sh` again from its WebIDE terminal unless you first
supply a new `MUSIC_FLAMINGO_RUN_ID`; the automatic stage is the authoritative route
for this event.

The generic `vscode` event opens a **manual-only** Dev GPU terminal. It never
starts a model, restores a ledger, hydrates LFS, or invokes
`devgpu_run_kugou_campaign.sh`. This is the only route for a controlled
single-song or selected-quality retry after changing GPU family.

The terminal command must declare all of the following explicitly:

- a source manifest and a strictly increasing selection file;
- the exact source-manifest SHA-256 from the receipt-bound campaign;
- `MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT`, matching the selection exactly
  (the route caps a manual run at five tracks);
- a ledger branch named
  `campaign-results/<campaign-id>-quality-rerun-<attempt>`; the primary
  `campaign-results/<campaign-id>` branch is refused;
- a GPU family/profile pair (`L40` + `nvidia-l40/full_precision/bfloat16`, or
  `H20` + `nvidia-h20/full_precision/bfloat16`), with the corresponding clean
  allocation floor; and
- durable checkpointing for every selected track.

For example, a one-song L40 probe is launched from the terminal only after an
overlay branch has supplied the campaign-specific values:

```bash
export MUSIC_FLAMINGO_MANUAL_QUALITY_ROUTE=1
export MUSIC_FLAMINGO_CAMPAIGN_ID=kugou-weekly-20260721
export MUSIC_FLAMINGO_QUALITY_SOURCE_MANIFEST=data/input/campaign-kugou-weekly-20260721/manifest.jsonl
export MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT=data/input/campaign-kugou-weekly-20260721
export MUSIC_FLAMINGO_QUALITY_SELECTION_FILE=data/input/campaign-kugou-weekly-20260721/quality_probe_1.txt
export MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256=516aa4cab74a9d9c0ec426659a2f221257934fbe77e6ec23d774adf9108549f6
export MUSIC_FLAMINGO_QUALITY_SOURCE_EXPECTED_COUNT=229
export MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT=1
export MUSIC_FLAMINGO_LEDGER_BRANCH=campaign-results/kugou-weekly-20260721-quality-rerun-l40-probe-1
export MUSIC_FLAMINGO_EXECUTION_PROFILE=nvidia-l40/full_precision/bfloat16
export MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED=1
export MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY=1
bash scripts/devgpu_run_manual_kugou_quality_rerun.sh
```

The route validates the request without reading the primary ledger, creates a
compact sparse-LFS manifest, then performs a clean-GPU gate both before LFS
hydration and immediately before model load. A failed gate writes its receipt
and exits without an inference attempt.

The viewer uses one atomic `run_status.json` document. It never combines a newly
started PID with an exit code from a previous run; a hard-killed process is shown as
`stale` rather than falsely shown as running.

Live output files are under the current run directory:

```text
/workspace/cache/output/music_flamingo_pipeline/devgpu_batch10_smoke/<CNB_BUILD_ID>/
├── run_status.json
├── run.log
├── progress.jsonl
├── batch_report.json
├── model_report.json
└── stderr.txt
```

`batch_report.json` now records both `elapsed_seconds` and
`script_elapsed_seconds`, generated-token counts per item and in total, and the
configured telemetry mode. This separates imports/startup from true batch work and
makes future speed comparisons trustworthy.

Each completed remote smoke/batch route packages the full run directory as
`cnb-artifacts/music-flamingo-run.tar.gz` and uploads it with the CNB attachment
plugin. That attachment is the durable evidence record; the mounted Docker volume is
only a node-local working cache.

### Avoiding repeat cold starts

The heavy runtime image pull happens in CNB `prepare`. Group work into one ready Dev
GPU workspace when safe, then stop that workspace immediately when the grouped batch
finishes. This avoids paying the image-pull startup cost repeatedly while avoiding
idle GPU billing.

Large workloads must be divided into independently run-scoped batches; do not try to
send hundreds of tracks through one sequential GPU pipeline. CNB's GPU task limit is
four hours, so final batch size must be chosen from measured stage timing, not a fixed
song count.

## Disposable KuGou campaign runs

The weekly publisher creates a new CNB repository named
`wuyoumusic/music-flamingo-campaign-<run-id>` for each fresh run. The repository
contains a code-only export from a commit already reachable from GitHub
`origin/main`, one generated `.cnb.yml`, and that run's manifest/audio only. The
protected `wuyoumusic/moss-music-runner` repository remains the runtime source;
it is never used as the weekly campaign's mutable input/result store.

The generic `api_trigger_music_flamingo_campaign` event is injected into every
disposable repository. The publisher supplies these identity variables per
shard, so the runner has no hard-coded week, song count, repository URL, or
shard:

- `MUSIC_FLAMINGO_CAMPAIGN_ID`, source manifest path, and expected count;
- `MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX`, `..._SHARD_COUNT`, and `..._SHARD_ID`;
- the immutable `CNB_RUNTIME_IMAGE` digest and manifest SHA-256;
- the campaign ledger repository URL and `campaign-results/<run-id>` branch.

`scripts/run_music_flamingo_campaign.sh` restores the same durable ledger,
prepares one deterministic shard, runs Music Flamingo only for pending items,
checkpoints the ledger, and packages run evidence. A new campaign repository
starts with only `main`; the first ledger checkpoint creates its result branch.
The publisher waits for every configured shard, clones the ledger branch, and
uses `scripts/build_kugou_canonical_delivery.py` to create the canonical JSONL.
Build logs are evidence, not a delivery.

The normal contract remains `2048` generated tokens and a `240` second audio
ceiling. A failed shard or ledger recovery leaves the exact repository and
receipt for retry; it must not be hidden by creating a new campaign slug. After
local import/release and the peer gate, the publisher deletes the exact
receipt-bound repository and verifies 404/zero volume and protected runtime
survival. Dry-run preparation never creates a repository, pushes objects, or
starts a build.

The older fixed `api_trigger_campaign_kugou_20260706` and quality-rerun events
remain in `.cnb.yml` only for historical recovery/inspection. They are not the
weekly route and must not be used to create new campaign data.

### Historical quality rerun for token-cap degeneration (not weekly)

For historical recovery only, `api_trigger_kugou_quality_rerun_12` is an isolated route for source-manifest
rows `24, 25, 39, 165, 414, 463, 470, 483, 545, 598, 667, 847`.  It pulls only those
12 LFS objects and writes to `campaign-results/kugou-20260706-quality-rerun-12`; it
never changes the completed 927-item ledger.  The route uses a `1400` token cap plus
`repetition_penalty=1.08` and `no_repeat_ngram_size=4`, records those controls in the
batch report/progress log, and checkpoints every item.  Review this isolated ledger
before choosing any result to replace in a downstream delivery manifest.

## Prepare and run inspection

Immediately after creating a Dev GPU workspace, inspect prepare without modifying
CNB state:

```bash
bash scripts/watch_cnb_prepare.sh <SN> [PIPELINE_ID]
```

The watcher exits zero only for a successful prepare. A failed, cancelled, skipped,
or errored prepare is a nonzero result. If a build has multiple pipelines, both the
watcher and the inspector require an explicit `PIPELINE_ID`; they no longer choose an
opaque ID lexicographically.

For completed, failed, or cancelled runs:

```bash
bash scripts/inspect_cnb_run.sh <SN> [PIPELINE_ID]
```

## Final-analysis defaults

```text
MUSIC_FLAMINGO_MODEL=nvidia/music-flamingo-think-2601-hf
MUSIC_FLAMINGO_REVISION=1ea2109
MUSIC_FLAMINGO_MAX_NEW_TOKENS=2048
MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS=240
```

The default prompt asks for genre, tempo, tonal center, rhythm, instrumentation,
vocal character, production, structure, and mood, while excluding lyrics.

The loader strategy remains unchanged:

- 40 GiB or larger GPU: try full precision first, then 4-bit, then 8-bit.
- smaller GPU: try 4-bit first, then 8-bit, then full precision.

## Local/manual runs

```bash
cp config/env.example .env
bash scripts/check_remote_env.sh
bash scripts/pick_one_audio.sh
python scripts/run_one_music_flamingo_smoke.py verify
python scripts/run_one_music_flamingo_smoke.py infer
```

For a local rerun, use a new `MUSIC_FLAMINGO_RUN_ID` to preserve the previous output.
The CNB Docker Artifact image remains the production execution path.
