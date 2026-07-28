---
name: music-kb-audio-downloader
description: Prepare a deduplicated Kugou audio queue from a new chart export and execute the verified direct download path. The primary Kugou worker is serial; its QQ/Migu/Kuwo fallback uses two isolated workers with one safe merger. Use only on the publisher machine.
---

# Music KB Audio Downloader

This is the upstream **primary download** atom for the weekly publisher
workflow (receipt/atom name is still `claude_download` for resume
compatibility — that name does **not** mean Claude must run). It is separate
from `music-kb-weekly-publisher`, which starts from a completed CNB delivery.

## Grok 宿主合同（必读）

| 角色 | 职责 |
| --- | --- |
| **Grok** | 编排员 + 审计员：设路径、启动 fixed 命令、等进程结束、读 progress/atom 收据、判失败/是否进 fallback |
| **Python worker** | 唯一允许写 `song_inventory`、progress、音频、歌词收据的进程 |

代码默认已是 `--executor direct`（musicdl 串行 worker）。**真正下文件的是 worker，不是 Claude。**

### 根路径

Never invent a checkout folder name such as `music-analysis-kb/plugins/...`.

| 变量 | 含义 |
| --- | --- |
| `MUSIC_WORKSPACE` | 发布机数据区（榜、库存、音频、`data/download_runs`） |
| `MUSIC_KB_PLUGIN` | 插件包根（含 `scripts/`、`references/`、`pyproject.toml`） |

```bash
export MUSIC_WORKSPACE="/absolute/path/to/music-workspace"
# 优先 monorepo checkout：
export MUSIC_KB_PLUGIN="/absolute/path/to/music-analysis-kb/plugins/music-kb"
# 或：grok plugin details music-kb 给出的 enabled 安装路径
# 禁止 ls …/music-kb-* | head -1
test -f "$MUSIC_KB_PLUGIN/scripts/run_claude_download.py"
```

脚本调用：

```bash
python3 "$MUSIC_KB_PLUGIN/scripts/<script>.py" ...
```

相对 `data/...` 路径时 `cwd` = `$MUSIC_WORKSPACE`。

### Grok 上禁止

- `--executor claude`（以及再 spawn Claude/Codex 去下载）
- 手改 inventory / 伪造成功
- 多个 worker 同时写正式库存
- worker 仍在跑就宣称成功
- 直接对正式库存跑 `download_music_fallback.py` 分片

### 长任务怎么盯（Grok，不用 Claude Monitor）

1. 启动 fixed 命令（**必须** `--executor direct`，可省略则依赖默认 direct）
2. 长任务 background / 等进程退出
3. 成功 = 进程 exit 0 **且** `progress.json` / wrapper JSON 为终端态
4. 失败 = 只读 `status`/`reason`，按合同进 fallback 或停下

### 历史命名对照

| 对外称呼 | 收据/脚本（勿改字段以免 resume 坏） |
| --- | --- |
| 主下载 primary download | atom `claude_download`，`run_claude_download.py` |
| 跨平台 fallback | atom `fallback_download`，`run_claude_fallback.py` |

## Grok 操作剧本（主下载 → fallback）

1. 设好 `MUSIC_WORKSPACE` / `MUSIC_KB_PLUGIN`。
2. 需要时先 `--dry-run`，再实跑主下载：

```bash
cd "$MUSIC_WORKSPACE"
python3 "$MUSIC_KB_PLUGIN/scripts/run_claude_download.py"   --workspace "$MUSIC_WORKSPACE"   --source data/processed/kugou/<songs-export>.json   --run-id <run-id>   --executor direct   --proxy http://127.0.0.1:7890
```

3. 盯 `data/download_runs/<run-id>/progress.json`。
4. 若连续大量 `direct_musicdl_no_download_url` / 平台验证失败：
   **不要**为空跑完整队酷狗 title-search 浪费时间；记录失败并进入 fallback 决策。
5. Fallback **只处理库存里已是 `failed` / `no_results` 的歌**。
   主下载从未尝试、尚未写入 inventory 的歌 **不会**进入 fallback 队列——须先让主路径写出终端态（或承认需业务侧快失败，P0 不改 worker）。
6. 启动 fallback（direct only）：

```bash
export MUSICDL_PYTHON=/absolute/path/to/python-that-imports-musicdl
python3 "$MUSIC_KB_PLUGIN/scripts/run_claude_fallback.py"   --workspace "$MUSIC_WORKSPACE"   --run-id <run-id>   --worker-python "$MUSICDL_PYTHON"   --executor direct   --proxy http://127.0.0.1:7890
```

先 `--dry-run` 看 queued 数量与 status 分解。

## Worker 内部四阶段（由 wrapper 调用，Grok 不手搓写库存）

The atom has four bounded stages:

1. Rebuild `data/song_inventory.json` from the Kugou SQLite source, the legacy
   July 6 progress file, and the actual audio files on disk.
2. Compare a new `kugou-cli` chart export with that inventory. Deduplicate by
   `kugou:<mix_song_id>` first and by normalized title + artist as a fallback.
3. Write a JSONL queue containing only songs that are not already downloaded.
4. Run the deterministic `scripts/download_music_queue.py` worker directly in
   one serial process. It uses `musicdl` with `KugouMusicClient`, first resolves
   the queue's exact Kugou mix-song page to one verified audio hash, and falls
   back to title/artist search only when that direct parser cannot produce an
   audio URL. It updates the inventory after every attempt and writes an
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
- The primary Kugou worker remains the one serial owner of inventory, progress,
  and lyric receipts. It must not be parallelized.
- The fallback wrapper may run two workers only through isolated staging and a
  serial merger. Never start two `download_music_fallback.py` processes against
  the real inventory, progress, or audio directory directly.
- Do not use the historical `batch_download.py` for weekly updates. It scans
  the whole SQLite database and predates the queue-level inventory contract.
- Do not commit `song_inventory.json`, queue runs, audio, progress, logs, or
  credentials to the plugin repository. Lyrics receipts are operational data
  too; only their validated normal text enters the private SQLite snapshot.

## Canonical invocation

From the workspace containing `data/music_trends.sqlite` (set both roots first):

```bash
export MUSIC_WORKSPACE=/path/to/music-workspace
export MUSIC_KB_PLUGIN=/absolute/path/to/plugins/music-kb
cd "$MUSIC_WORKSPACE"
python3 "$MUSIC_KB_PLUGIN/scripts/run_claude_download.py" \
  --workspace "$MUSIC_WORKSPACE" \
  --source data/processed/kugou/kugou-charts-full-20260706-105721-songs-dedup.json \
  --run-id kugou-download-2026w29 \
  --executor direct \
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
cd "$MUSIC_WORKSPACE"
python3 "$MUSIC_KB_PLUGIN/scripts/build_song_inventory.py" \
  --db data/music_trends.sqlite \
  --progress download_progress.json \
  --inventory data/song_inventory.json \
  --audio-root music_downloads/KugouMusicClient

python3 "$MUSIC_KB_PLUGIN/scripts/prepare_download_queue.py" \
  --source data/processed/kugou/<new-songs-export>.json \
  --inventory data/song_inventory.json \
  --output data/download_runs/<run-id>/download_queue.jsonl \
  --audio-root music_downloads/KugouMusicClient
```

It then materializes one filtered execution queue and invokes the worker once
(the wrapper supplies absolute paths and captures the result):

```bash
python3 "$MUSIC_KB_PLUGIN/scripts/download_music_queue.py" \
  --queue data/download_runs/<run-id>/download-queue-direct.jsonl \
  --inventory data/song_inventory.json \
  --work-dir music_downloads \
  --progress data/download_runs/<run-id>/progress.json \
  --log data/download_runs/<run-id>/download.log \
  --run-id <run-id> \
  --item-timeout-seconds 60
```

The direct path keeps exact MixSongID validation, inventory/progress atomic
writes, and append-only lyric receipts in the same worker. Its default
`--lookup-mode exact-page-first` prevents `musicdl` from expanding several
title/artist candidates when the queue already has an exact Kugou source URL;
`--lookup-mode search-only` is the measured rollback path.

### Legacy executor (Grok 禁止)

`--executor claude` still exists for Codex/Claude Code compatibility only.
**Do not use it under Grok Build.** It spawns a nested Claude CLI with Monitor
semantics; that is slower, costlier, and not the supported host model.


### Measured publisher profile

On the publisher Mac, the apparent default route is the system TUN proxy, not
a bare public direct connection. The currently fastest validated fallback
profile is the direct wrapper with `--parallelism 2` and an explicit healthy
`http://127.0.0.1:7890` proxy. The direct two-song end-to-end sample completed
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
python3 "$MUSIC_KB_PLUGIN/scripts/run_claude_lyrics_backfill.py" \
  --workspace "$MUSIC_WORKSPACE" \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --chart-db "$MUSIC_WORKSPACE/data/music_trends.sqlite" \
  --run-id kugou-lyrics-backfill-2026w30 \
  --executor direct \
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

The fallback queue contains **only** inventory rows whose status is
`no_results` or `failed`. Songs never attempted by primary download (not yet
in inventory as terminal) **are not** selected. Each record receives at most
two fallback rounds; a second unsuccessful round becomes `abandoned` (needs
explicit `--retry-abandoned` recovery). Always `--dry-run` first and review
queued counts.

Under Grok, start fallback with **direct** only:

```bash
export MUSICDL_PYTHON=/absolute/path/to/python-that-imports-musicdl
python3 "$MUSIC_KB_PLUGIN/scripts/run_claude_fallback.py" \
  --workspace "$MUSIC_WORKSPACE" \
  --run-id <run-id> \
  --worker-python "$MUSICDL_PYTHON" \
  --executor direct \
  --proxy http://127.0.0.1:7890
```

The wrapper validates the `fallback_download` operation record, proves
`--worker-python` imports `musicdl`, then starts a short detached supervisor.
P=2 isolated shards never touch real inventory; one serial merger is the only
formal-state writer. **Grok must not** wait/kill/wrap/restart that launcher
incorrectly, and must not run `download_music_fallback.py` against the real
inventory by hand.

Accept a fallback file only after it exists, exceeds 1 MB, and has an ffprobe
duration of at least 60 seconds.

`--executor claude` for fallback is **legacy only** (Codex/Claude Code). **Grok
禁止使用。**

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
cd "$MUSIC_WORKSPACE"
python3 "$MUSIC_KB_PLUGIN/scripts/prune_audio_library.py" \
  --inventory data/song_inventory.json \
  --audio-root music_downloads/KugouMusicClient \
  --knowledge-db "$HOME/.music-kb/music-master.sqlite" \
  --expected-count 927
```

That is a dry-run. The command checks the inventory count, canonical delivery
count, source-track count, non-empty source-link count, and full lyric coverage
before deleting anything. Execute the deletion only with the explicit flag:

```bash
python3 "$MUSIC_KB_PLUGIN/scripts/prune_audio_library.py" \
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

## Provenance (historical)

The July 6 2026 publisher session that first validated the download method is
archived outside this plugin (Claude Code session history on the publisher
Mac). It is **not** required to run this atom under Grok Build.

That session recorded: install `musicdl`, test a Kugou search and a single
download, write `batch_download.py`, then run the batch in the background.
The final report was 927/927 successful, 0 failed, 0 no-result, about 37GB,
with FLAC/MP3 output and LRC files. This atom preserves the effective
`MusicClient` + `KugouMusicClient` method but adds queue-level deduplication
and per-run inventory updates. Default executor today is **direct**, not a
Claude-hosted worker.
