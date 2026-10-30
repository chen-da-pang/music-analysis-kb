# Weekly coverage gate and embedding/audience stages (issue #147)

## Status

accepted (2026-10-30)

## Context

The 2026w44 weekly run (see issue #147) exposed that the weekly pipeline could
finish and install a snapshot while the library had silent coverage gaps:

- 357 newly imported canonical recordings had no vectors; global vector
  coverage fell from 95.4% to 84.1% (2561/3044).
- 357 newly imported canonical recordings had no audience profile (2735/3044
  coverage); no pending-research list was produced.
- import/weekly-update failed with JSON ok:false while the shell exit code was
  0, so the run reported success; lyrics gate failures behaved the same way.
- MOSS analysis failed for 3 of 362 queue items and nothing retried them.
- The operator scripts live in research/weekly-auto (outside this repository),
  so the repository could not describe or verify the weekly coverage contract.

## Decision

1. research/weekly-auto/run_weekly.sh gains three stages, all using the
   existing run_stage contract (per-stage log, summary and fail-fast):
   - embedding, after import_delivery and before lyrics_backfill;
   - audience_pending, after embedding;
   - coverage_gate, after doctor_validate.
2. The embedding stage only processes canonical recordings imported by the
   current run that have no recording_embedding rows. It plans by
   recording.audio_sha256 (canonical audio), symlinks the planned files into
   run/embedding/audio, runs embed_audio.py --channel overall and
   --channel merit, then imports overall (muq-mulan-v3 / OpenMuQ/MuQ-MuLan-large
   / v3 / cosine / CC-BY-NC-4.0 / 512), rhythm and timbre
   (merit-mert330m-w3-v2 / amaai-lab/merit /
   mert330m-l3-4-5-6-23-window3-v2 / cosine /
   CC-BY-NC-4.0 (MERT); MIT (heads) / 128, --factor rhythm|timbre).
   It does not pass --allow-alternate-rendition: weekly canonical downloads are
   byte-identical to recording.audio_sha256 (verified on
   rec_kugou_51dcddf9... in w44). Existing alternate-rendition vectors are
   untouched. A second embed_audio.py process is refused.
   DRY_RUN plans only: embedding.receipt.json has status=planned and the model
   is not run.
3. The audience_pending stage writes audience-pending.jsonl,
   audience-pending.summary.json, and the machine-readable
   audience-pending.research.json (run_id, generated_at, record count, list
   path, suggested research entry and import command). It never fabricates
   profiles and never fails the run. Research is network/API work and may run
   in parallel with the local embedding stage; the weekly snapshot is not
   required to contain the new profiles.
4. The coverage_gate stage compares the post-run master with a pre-run baseline
   captured at backup_master time and fails the run when a newly imported
   canonical recording has no vector or when the global vector ratio drops more
   than 2.0 percentage points. ALLOW_GAPS=1 downgrades the gate to a warning and
   records coverage-allow-gaps.flag. Audience gaps are warnings only; the final
   report status becomes succeeded-with-gaps.
5. MOSS batch gets up to three attempts under the same run id (sidecar resume).
   Each attempt parses the batch JSON tail for completed/failed/delivered and
   writes moss.retry.json; residual failures are recorded and do not fail the
   stage (the coverage gate decides whether the run may claim success).
6. The operator scripts and helper stay in research/weekly-auto (outside git).
   Only this ADR and the operations note are versioned. No schema change, no
   recording.audio_sha256 change, no publish/peer distribution.

## Coverage gate contract

The helper (research/weekly-auto/helpers/weekly_coverage.py) is read-only
against master and writes JSON under the run automation directory:

- plan-embedding --db --audio-root --out [--new-ids FILE] [--allow-missing]
  writes embedding-plan.jsonl/json and exits 2 if a planned canonical recording
  has no locatable audio file;
- coverage --db --new-ids FILE --out --baseline-ratio X writes
  coverage-report.json with canonical, embeddings, vector_ratio,
  baseline_vector_ratio, delta_pp, new_canonical, new_embedded,
  new_missing_embedding, audience_pending_n, audience_research_ready, gate_ok
  and warnings; gate_ok is false when new_missing_embedding is non-empty or
  delta_pp < -2.0;
- audience-pending --db --out writes the pending list, summary and research
  entry;
- baseline --db --out writes coverage-baseline.json before import.

## Failure modes

- Master already has 124 canonical recordings whose audio is not retained
  (platform had no free source). plan-embedding reports them as missing files;
  the embedding stage scopes planning to newly imported ids so these historical
  gaps do not abort the run.
- Partial embedding failures are recorded in embedding.receipt.json and then
  fail the coverage gate through new_missing_embedding.
- A gate failure stops before the report can claim success; rerunning with
  ALLOW_GAPS=1 produces succeeded-with-gaps plus the allow-gaps flag.
- Residual MOSS failures appear in moss.retry.json and in the coverage report
  rather than being silently dropped.

## Verification

- DRY_RUN=1 run (kugou-2026w45-dry): status succeeded, embedding.receipt.json
  status=planned, coverage-report.json present, no master/release writes.
- Helper pytest and repository fallback pytest green; bash -n on
  run_weekly.sh and helper.
- Real plan-embedding probe on 2026-10-30: canonical 3044, already_embedded
  2561, selected 483, locatable 359, missing_files 124 (historical
  no-audio recordings). No model was run for this probe.
- Existing fallback merge regression coverage lives in
  tests/test_fallback_parallel_merge.py (flat root file move, existing
  target-directory merge, preserved media, dry-run) and
  tests/test_run_claude_fallback.py (refusal when the inventory changed); no
  additional test was needed in tests/test_fallback_parallel.py.
