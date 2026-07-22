# Weekly audio download through Claude Code

The July 6 Claude Code session is the source of truth for the download method.
It inspected the 927-song SQLite/Obsidian corpus, installed `musicdl`, tested
`半岛铁盒 周杰伦`, wrote `batch_download.py`, and ran the full batch. The
successful run reported 927/927 downloaded, 0 failures, 0 no-result records,
and about 37GB under `music_downloads/KugouMusicClient/`.

The reusable workflow is now split into two tools:

- `prepare_download_queue.py` owns identity and deduplication.
- `download_music_queue.py` owns the proven `musicdl` search/download loop.
- The same worker writes an append-only exact-identity lyric receipt from
  `SongInfo.lyric`; it never promotes a loose `.lrc` file into the database.
- `prune_audio_library.py` removes local audio only after the release and link
  and lyric-coverage counts pass their safety gates; it preserves the
  deduplication inventory.

`run_claude_download.py` is the atom entry point. It invokes Claude Code with
an explicit prompt that tells the child to run the deterministic worker. This
keeps Claude Code responsible for the execution context while keeping the
download behavior reviewable and repeatable.

## One weekly run

```bash
export MUSIC_WORKSPACE=/path/to/music-workspace
cd "$MUSIC_WORKSPACE"

python3 music-analysis-kb/plugins/music-kb/scripts/run_claude_download.py \
  --workspace "$MUSIC_WORKSPACE" \
  --source data/processed/kugou/<new-songs-export>.json \
  --run-id kugou-download-2026w30 \
  --proxy http://127.0.0.1:7890
```

The source file may be the processed JSON emitted by the chart-capture atom:
`{"summary": ..., "charts": [...], "songs": [...]}`. JSONL and CSV are also
accepted when each row has `mix_song_id`, `song_name`, and `artist_name` (or the
equivalent generic field names).

## Dry run first

```bash
python3 music-analysis-kb/plugins/music-kb/scripts/run_claude_download.py \
  --workspace "$MUSIC_WORKSPACE" \
  --source data/processed/kugou/<new-songs-export>.json \
  --run-id kugou-download-2026w30-dry \
  --dry-run \
  --proxy http://127.0.0.1:7890
```

Review `data/download_runs/<run-id>/queue_manifest.json`. The important fields
are `source_unique_records`, `skipped_existing_download`, `redownload_missing`,
and `queued`. The dry run also captures Claude Code's JSON response but does
not import `musicdl` and does not write audio.

## What Claude Code actually runs

The wrapper writes the exact prompt to
`data/download_runs/<run-id>/claude_prompt.txt` and captures stdout/stderr in
the same directory. The child is told to run only:

```bash
python3 music-analysis-kb/plugins/music-kb/scripts/download_music_queue.py \
  --queue data/download_runs/<run-id>/download_queue.jsonl \
  --inventory data/song_inventory.json \
  --work-dir music_downloads \
  --progress data/download_runs/<run-id>/progress.json \
  --log data/download_runs/<run-id>/download.log \
  --run-id <run-id> \
  --item-timeout-seconds 60
```

The worker initializes:

```python
MusicClient(
    music_sources=["KugouMusicClient"],
    init_music_clients_cfg={
        "KugouMusicClient": {
            "work_dir": "music_downloads",
            "search_size_per_source": 3,
        }
    },
)
```

For each queued song it searches `title + artist`, accepts a result only when
its returned Kugou `MixSongID`/`ID` exactly matches the queue's
`platform_track_key`, downloads the selected result, verifies that a file
exists, and updates the inventory immediately. It also normalizes the exact
result's `SongInfo.lyric` into a JSONL receipt with the canonical
`source_track_id`; a blank/network/parse/identity error remains `pending`.
If a run is interrupted,
the next run sees the file and skips it. A new chart row with an old
`mix_song_id` or an already-downloaded normalized title/artist is not queued.
Each search/download operation is bounded by `--item-timeout-seconds`; a
timeout is recorded as a real `failed` result so one unresponsive Kugou item
cannot block or be falsely marked as downloaded. Claude Code is explicitly
forbidden from editing inventory/progress/queue state by hand.

## Evidence and boundaries

- Chart capture is upstream and uses `kugou-cli`; this download atom does not
  fetch charts.
- `musicdl` is used for search/download, not for chart capture or comments.
- The original full-batch script remains as historical evidence, but weekly
  updates must use the queue worker so a new update cannot re-download all 927
  songs.
- `song_inventory.json`, queue runs, logs, progress, audio, and credentials are
  local operational data and must stay outside Git.

## Historical lyrics without re-downloading audio

For the already analyzed library, use the same CC/Kugou worker in
`--lyrics-only` mode. It first reads unresolved canonical `source_track` rows
from the publisher master. New rows already carry `kugou-<MixSongID>`; the
historical delivery keys are resolved only by an exact join from their stored
`source_url` to `data/music_trends.sqlite.platform_tracks.play_link`, yielding
that row's `platform_track_key` (MixSongID). It never uses URL fragments, title,
or artist as an identity substitute. Dry-run first:

```bash
python3 music-analysis-kb/plugins/music-kb/scripts/run_claude_lyrics_backfill.py \
  --workspace "$MUSIC_WORKSPACE" \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --chart-db "$MUSIC_WORKSPACE/data/music_trends.sqlite" \
  --run-id kugou-lyrics-backfill-2026w30 \
  --dry-run
```

After review, remove `--dry-run`. The real command stores its queue, progress,
and receipt under `data/weekly_runs/<run-id>/lyrics-backfill/`, imports the
receipt via the normal source-identity validation, and exits nonzero if any
canonical lyric remains unresolved. Use `--allow-incomplete` only for an
explicit bounded diagnostic pass; it never permits a snapshot or audio cleanup.

## Removing the local audio after import

The audio is an input to Music Flamingo, not part of the searchable release.
After the canonical records, their listening URLs, and their terminal lyric
outcomes have been verified, run the prune atom in dry-run mode first:

```bash
python3 music-analysis-kb/plugins/music-kb/scripts/prune_audio_library.py \
  --inventory data/song_inventory.json \
  --audio-root music_downloads/KugouMusicClient \
  --knowledge-db "$HOME/.music-kb/music-master.sqlite" \
  --expected-count 927
```

The deletion form requires `--confirm-delete-audio`. It removes the local
audio tree, keeps the path/size/history in `song_inventory.json`, and marks
each record `retention: purged_after_analysis`. Queue preparation treats that
state as already acquired, so deleting the files does not cause a future
weekly run to download the same 927 songs again.
