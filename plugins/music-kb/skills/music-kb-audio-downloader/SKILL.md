---
name: music-kb-audio-downloader
description: Prepare a deduplicated Kugou audio queue from a new chart export and execute the Claude Code supervised download path. Primary and QQ/Migu/Kuwo fallback each use two isolated workers with one safe merger. Use only on the publisher machine.
---

# Music KB Audio Downloader

This is the upstream audio-download atom for the weekly publisher workflow.
It is intentionally separate from `music-kb-weekly-publisher`, which starts
from a completed CNB canonical delivery. The atom has four bounded stages:

1. Rebuild `data/song_inventory.json` from the Kugou SQLite source, the legacy
   July 6 progress file, and the actual audio files on disk.
2. Compare a new `kugou-cli` chart export with that inventory. Deduplicate by
   `kugou:<mix_song_id>` first and by normalized title + artist as a fallback.
3. Write a JSONL queue containing only songs that are not already downloaded.
4. Start one short Claude Code launcher for the primary queue. It owns two
   isolated `scripts/download_music_queue.py` shards and one serial merger.
   Each worker uses `musicdl` with `KugouMusicClient`, first resolves the
   queue's exact Kugou mix-song page to one verified audio hash, and falls back
   to title/artist search only when that direct parser cannot produce an audio
   URL. Only the merger updates durable inventory and writes the
   identity-bound lyric receipt from the exact result's `SongInfo.lyric`.

If the primary worker leaves songs as `no_results` or `failed`, run the separate
fallback atom through `scripts/run_claude_fallback.py`. Its direct mode defaults
to two isolated workers through the versioned
`references/fallback-download-profile.json` sources (QQ, Migu, then Kuwo). Each
worker receives its own queue shard, inventory copy, progress file, log, and
staging audio directory. Only after every shard reaches terminal results and the
real inventory hash is unchanged does one serial merger move verified media and
sidecars into the real audio directory and update the real inventory/progress.
`--parallelism 1` is the diagnostic rollback; do not use more than two workers.
Fallback matching is exact on normalized title and artist, with only aliases
listed in the queue/profile accepted. A fallback file is accepted only when it
exists, exceeds 1 MB, and has an `ffprobe` duration of at least 60 seconds.

The atom never calls `kugou-cli` itself. `kugou-cli` is the upstream chart
capture atom; this atom consumes its processed songs JSON/JSONL/CSV export.

## Required boundary

- Run on the publisher Mac only.
- Primary and fallback workers are isolated from durable state. The single
  serial merger is the only owner of inventory, progress, media placement, and
  lyric receipts; do not run shard workers against shared state.
- The fallback wrapper may run two workers only through isolated staging and a
  serial merger. Never start two `download_music_fallback.py` processes against
  the real inventory, progress, or audio directory directly.
- Do not use the historical `batch_download.py` for weekly updates. It scans
  the whole SQLite database and predates the queue-level inventory contract.
- Do not commit `song_inventory.json`, queue runs, audio, progress, logs, or
  credentials to the plugin repository. Lyrics receipts are operational data
  too; only their validated normal text enters the private SQLite snapshot.

## Canonical invocation

From the workspace containing `data/music_trends.sqlite`:

```bash
export MUSIC_WORKSPACE=/path/to/music-workspace
cd "$MUSIC_WORKSPACE"
python3 music-analysis-kb/plugins/music-kb/scripts/run_claude_download.py \
  --workspace "$MUSIC_WORKSPACE" \
  --source data/processed/kugou/kugou-charts-full-20260706-105721-songs-dedup.json \
  --run-id kugou-download-2026w29 \
  --proxy http://127.0.0.1:7890
```

Before a real run, use the same command with `--dry-run`. To test a bounded
prefix of a queue, add `--max-items 1` or another small number. A dry run does
not import `musicdl` or touch audio. The output records inventory, queue,
worker, and per-song stage timings for a comparable baseline.
The worker defaults to `--item-timeout-seconds 60` for each musicdl search or
download operation. A timeout is recorded as `failed` and the queue continues;
the wrapper must not hand-edit inventory, progress, queue, or retention state.

The wrapper performs these local commands before starting the fixed worker:

```bash
python3 music-analysis-kb/plugins/music-kb/scripts/build_song_inventory.py \
  --db data/music_trends.sqlite \
  --progress download_progress.json \
  --inventory data/song_inventory.json \
  --audio-root music_downloads/KugouMusicClient

python3 music-analysis-kb/plugins/music-kb/scripts/prepare_download_queue.py \
  --source data/processed/kugou/<new-songs-export>.json \
  --inventory data/song_inventory.json \
  --output data/download_runs/<run-id>/download_queue.jsonl \
  --audio-root music_downloads/KugouMusicClient
```

It then materializes one filtered execution queue and invokes the worker once
(the wrapper supplies absolute paths and captures the result):

```bash
python3 .../download_music_queue.py \
  --queue data/download_runs/<run-id>/download-queue-direct.jsonl \
  --inventory data/song_inventory.json \
  --work-dir music_downloads \
  --progress data/download_runs/<run-id>/progress.json \
  --log data/download_runs/<run-id>/download.log \
  --run-id <run-id> \
  --item-timeout-seconds 60
```

The primary path keeps exact MixSongID validation, atomic merge writes, and
append-only lyric receipts. Its default `--lookup-mode search-only` avoids the
known exact-page parser failure before transfer; `--lookup-mode exact-page-first`
is diagnostic only. The formal weekly contract is `--executor claude
--parallelism 2`; Claude starts only the short launcher and inherits
`http_proxy`/`https_proxy` when `--proxy` is provided.

### Measured publisher profile

On the publisher Mac, the apparent default route is the system TUN proxy, not
a bare public direct connection. The currently fastest validated fallback
profile is the Claude-supervised wrapper with `--parallelism 2` and an explicit healthy
`http://127.0.0.1:7890` proxy. The two-song end-to-end sample completed
2/2 identity- and `ffprobe`-validated downloads in 15.869 seconds, compared
with 24.994 seconds for the same serial wrapper shape. Audio CDN transfer also
benefited from two streams; four streams did not add a repeatable gain.

These are publisher-Mac measurements, not a universal seconds-per-song
promise: provider parsing and the audio format returned by `musicdl` vary per
song. Before a real run, verify that the local proxy listener is healthy. If
it is unavailable, omit `--proxy` and use the system TUN path; do not route
through an invented endpoint or bypass a platform login/paywall.

## Lyrics receipt and historical backfill

The audio worker accepts a search result only when its raw Kugou
`MixSongID`/`ID` exactly equals the queue `platform_track_key`. After a
successful exact result, it normalizes `SongInfo.lyric` and appends a receipt
with `source_name`, `source_track_id`, status, evidence, and a text hash. It
must not scan a generated `.lrc` file. Empty/network/parse/identity errors are
`pending`; only exact platform evidence can produce `instrumental` or
`platform_unavailable`.

For the historical library, do **not** re-download audio. Run the dedicated
wrapper against the publisher master:

```bash
python3 music-analysis-kb/plugins/music-kb/scripts/run_claude_lyrics_backfill.py \
  --workspace "$MUSIC_WORKSPACE" \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --chart-db "$MUSIC_WORKSPACE/data/music_trends.sqlite" \
  --run-id kugou-lyrics-backfill-2026w30 \
  --dry-run
```

It materializes one exact platform identity for each unresolved canonical
source: current rows use `kugou-<MixSongID>` directly, while historical rows
resolve only by an exact `source_url` to chart `play_link` lookup in the
authoritative `--chart-db`. The worker must not receive an inventory argument,
write audio, or inspect existing LRC files. After review, rerun without
`--dry-run`; its identity-validated receipt is imported into the master
automatically.

### Fallback invocation

The fallback queue contains only records whose current inventory status is
`no_results` or `failed`. Each record receives at most two fallback rounds
across runs. A second unsuccessful round becomes the durable, auditable
`abandoned` state and is omitted from both automatic queues; use
`run_claude_download.py --retry-abandoned` only for an explicit recovery.
Before a real run, use `--dry-run` and review the queue count and status
breakdown. The fallback wrapper owns queue preparation, safe two-way sharding,
and the final merger. The formal weekly path uses `--executor claude` to start
the same short launcher:

```bash
export MUSICDL_PYTHON=/absolute/path/to/python-that-imports-musicdl
python3 music-analysis-kb/plugins/music-kb/scripts/run_claude_fallback.py \
  --workspace "$MUSIC_WORKSPACE" \
  --run-id <run-id> \
  --source-queue data/download_runs/<primary-run-id>/download_queue.jsonl \
  --worker-python "$MUSICDL_PYTHON" \
  --proxy http://127.0.0.1:7890
```

For a weekly recovery, `--source-queue` is required: it must be a primary JSONL
queue with unique identities, and only those identities that are currently
`failed` or `no_results` may enter fallback. It never sweeps historical inventory.

For a real run, the wrapper validates the `fallback_download` operation record,
proves `--worker-python` can import `musicdl`, then lets the one Claude Code
launcher start the detached supervisor. The supervisor runs the actual P=2
isolated shards and its serial merger is the only code allowed to touch real
inventory/progress and the configured music directory. Claude must not wait,
kill, wrap, restart, or directly run `download_music_fallback.py`.

Accept a fallback file only after it exists, exceeds 1 MB, and has an ffprobe
duration of at least 60 seconds. Each child may write only its run-local shard
directory; it must not call `kugou-cli`, the old full-database downloader, or
edit real inventory by hand.

## Inventory contract

`data/song_inventory.json` is the durable source of truth for the local audio
library. Each song records:

- `identity_key`: strong platform identity, normally `kugou:<mix_song_id>`;
- `title_artist_key`: normalized fallback identity;
- title, artist, play link, source chart run, and chart appearances;
- `download.status`: `downloaded`, `missing`, `failed`, `no_results`,
  `abandoned`, or `not_attempted`;
- fallback attempts retain their count, per-round history, and terminal reason;
- relative audio path, extension, size, mtime, and optional SHA-256.

The inventory is rebuilt before each queue preparation but historical songs are
retained even when they leave the newest chart. A song is skipped only when
its inventory record says `downloaded` and either the recorded file still
exists or the record is explicitly marked `purged_after_analysis`. Missing
files are queued for repair; failed/no-result records are retried up to the
fallback limit, while `abandoned` records require an explicit retry flag.

## Purge audio after analysis

Once the canonical release has been imported, validated, its source links are
present, and lyric coverage is terminal for every canonical recording, the
local audio tree can be removed without breaking deduplication:

```bash
python3 music-analysis-kb/plugins/music-kb/scripts/prune_audio_library.py \
  --inventory data/song_inventory.json \
  --audio-root music_downloads/KugouMusicClient \
  --knowledge-db "$HOME/.music-kb/music-master.sqlite" \
  --expected-count 927
```

That is a dry-run. The command checks the inventory count, canonical delivery
count, source-track count, non-empty source-link count, and full lyric coverage
before deleting anything. Execute the deletion only with the explicit flag:

```bash
.../prune_audio_library.py \
  --inventory data/song_inventory.json \
  --audio-root music_downloads/KugouMusicClient \
  --knowledge-db "$HOME/.music-kb/music-master.sqlite" \
  --expected-count 927 \
  --confirm-delete-audio
```

The inventory keeps every identity, title, artist, chart appearance, and
historical relative path. It changes only the retention state to
`purged_after_analysis`, so the next weekly queue still skips all previously
acquired songs even though their audio files are gone.

## Provenance from the original Claude Code run

The original July 6 Claude Code conversation is retained locally at:

```text
~/.claude/projects/-Users-wycm-Documents--------/aa501171-fbfe-408b-9724-d2a2071038e4.jsonl
```

It records the validated method: install `musicdl`, test a Kugou search and a
single download, write `batch_download.py`, then run the batch in the
background. The final report was 927/927 successful, 0 failed, 0 no-result,
about 37GB, with FLAC/MP3 output and LRC files. This atom preserves the
effective `MusicClient` + `KugouMusicClient` method but adds queue-level
deduplication and per-run inventory updates.
