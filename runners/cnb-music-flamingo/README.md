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
receipts belong to the local publisher or a temporary CNB run ref. They are not
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
docker.cnb.cool/wuyoumusic/moss-music-runner@sha256:e9c85c9f22efbccaf7c291032a6a85f3103bd8691f6dee45cd80cdbc58413bca
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

The generic `vscode` event starts only the viewer. It is the manual route: set a fresh
`MUSIC_FLAMINGO_RUN_ID` before each terminal-launched batch.

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

## Temporary KuGou campaign runs

`api_trigger_campaign_kugou_20260706` is the resumable route for a KuGou corpus
provided by a temporary CNB run ref. Audio, manifests, and result ledgers are
runtime inputs and are never part of this GitHub subtree or the code-only CNB
`main`; the runner fetches only the audio needed for one static shard instead of
downloading the complete corpus at startup.

- static shards are `232 / 232 / 232 / 231` manifest rows, selected in manifest
  order;
- the current campaign contract remains `2048` generated tokens and a `240` second
  audio ceiling;
- every five outcomes, the runner fsyncs and pushes an append-only result ledger to
  `campaign-results/kugou-20260706`; a new CNB node restores that ledger before it
  chooses pending audio;
- a result is reusable only when its item id, source SHA-256/byte count, runner code,
  model/image, prompt, and execution profile all match the current contract.

Before starting a real shard, trigger the same event with both API environment
overrides below.  This performs no inference: it restores the ledger, installs and
uses `git-lfs` to hydrate exactly one pending audio object, validates its checksum,
and checkpoints the ledger branch.  The preflight guard rejects any selection other
than one object.

```json
{
  "env": {
    "MUSIC_FLAMINGO_CAMPAIGN_PREFLIGHT_ONLY": "1",
    "MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS": "1"
  }
}
```

For the actual four jobs, leave those two variables at `0`.  Start shard 1 with the
event defaults; for shards 2–4 override **both** values together, for example:

```json
{
  "env": {
    "MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX": "2",
    "MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID": "kugou-20260706-927-s2"
  }
}
```

Do not call the campaign complete until the durable ledger contains a current-contract
success record for all 927 manifest ids.  The ledger is authoritative by source id and
audio SHA-256; several source title/artist fields were found to be questionable, so do
not use those labels alone as final delivered metadata.

### Quality rerun for token-cap degeneration

`api_trigger_kugou_quality_rerun_12` is an isolated recovery route for source-manifest
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
