# Library map（库可视化）

把 publisher master 的三条听感通道投影成一张 2D 地图，生成只读静态页面；不写数据库，生成物放在仓库外。

## 依赖

- 发布者嵌入环境：Python 3.12 + `numpy` + `umap-learn` + `hdbscan`（本机用
  `/Users/wycm/Documents/克莱儿/.scratch/mir-song-description/bakeoff/venv-muq/bin/python`）。
- 本地音频：通过 `recording.audio_sha256` 匹配到原文件（`.flac/.mp3/.ogg/.m4a`），页面用符号链接提供完整播放。
- 主库：schema v9，含 overall / rhythm / timbre 三通道向量与 `recording_lyric` 歌词；有受众记录时详情面板读 `audience_*` 四表与投影出的录音级标签。

## 生成

```sh
VENV=/Users/wycm/Documents/克莱儿/.scratch/mir-song-description/bakeoff/venv-muq/bin/python
OUT=/Users/wycm/Documents/网易云热榜抓取/research/music-kb-similarity/viz
HF_HUB_OFFLINE=1 NUMBA_CACHE_DIR=/tmp/music-kb-numba \
  "$VENV" runners/library-viz/build_viz.py \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --output-dir "$OUT" \
  [--audio-root /path/to/music_downloads/KugouMusicClient]
```

## 查看

```sh
"$OUT/serve.sh"     # http://127.0.0.1:8792/
```

页面：左画布 + 右详情面板；顶部切换整体听感 / 节奏 / 音色；滚轮缩放、拖拽平移、双击重置；搜索高亮；簇图例可隐藏。点击点查看歌曲：完整音频播放（本地文件）、歌词、受众记录（调研画像 / 榜单证据 / 证据等级 / 来源 / 「非占比」声明）、当前通道 Top-5 邻域。

## 数据契约

`data.js` 暴露 `window.VIZ_DATA`：

- `channels[]`：`key`、`label`、`model_id`、`recipe`、`clusters[]`、`songs[]`。
- `clusters[]`：`{id, name, size, color}`；`id=-1` 为散点。
- `songs[]`：`{id, title, artists, cluster, x, y, full, lyric, lyric_status, audience, neighbors: [[index, distance], ...]}`。
- `x/y` 归一化到 `[-1, 1]`；`full` 是 `audio/` 下指向原始音频的符号链接（缺失时为空串）；`lyric` 来自 `recording_lyric.lyric_text`，`lyric_status` 为 available / platform_unavailable / missing。
- `audience`：无受众记录时为 `null`；否则 `{research, chart, tags, note}`，由 `build_viz.load_audience()` 只读 master 预生成（静态页无后端查询）：
  - `research` / `chart`：各取该歌最新一条调研画像 / 榜单证据（`null` 表示该组没有记录），字段为 `researched_at`、`time_window`、`method_id`、`method_version`、`confidence`、`status`、`basis_note`、`coverage_note`、`items[]`、`sources[]`。
  - `items[]`：`{dimension, label, bucket, value_low, value_high, value_mid, rank, evidence_level}`；`rank` 只有榜单证据有值，`value_*` 只有调研推断有值且是 0–1 区间。
  - `sources[]`：`{source_platform, source_key, url, title, source_type, evidence_level, coverage_note}`；不含发布端本机路径。
  - `tags[]`：`{namespace, name, path, key}`，即 `recording_tag(role='audience')` 投影出的可检索标签（`key` 形如 `audience/age/31-40`、`audience-hint/gen/00后`）。
  - `note`：固定的「非占比」声明（榜单证据只代表进过哪些池子，不代表受众占比）；榜单命中数写在 `basis_note`（如「单期快照命中 2 榜」），`coverage_note` 只表示真正的覆盖缺口，页面只在非空时显示「缺口」。

## 说明

- 聚类是探索性浏览：HDBSCAN 在 UMAP 30 维空间上运行，簇对参数敏感，**不是**质量结论。
- 邻域与距离来自对应通道的原始向量空间（余弦距离，越小越像）。
- 页面、音频符号链接与歌词数据都是生成物，不提交仓库；服务只绑定 `127.0.0.1`。
- 浏览器需支持对应音频格式（Chrome 支持 flac/ogg/mp3）；不支持时可用"在新标签播放完整音频"。
