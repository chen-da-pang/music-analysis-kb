# Library map（库可视化）

把 publisher master 的三条听感通道投影成一张 2D 地图，生成只读静态页面；不写数据库，生成物放在仓库外。

## 依赖

- 发布者嵌入环境：Python 3.12 + `numpy` + `umap-learn` + `hdbscan`（本机用
  `/Users/wycm/Documents/克莱儿/.scratch/mir-song-description/bakeoff/venv-muq/bin/python`）。
- `ffmpeg` / `ffprobe`：生成每首 20 秒试听片段。
- 主库：schema v8，含 overall / rhythm / timbre 三通道向量。

## 生成

```sh
VENV=/Users/wycm/Documents/克莱儿/.scratch/mir-song-description/bakeoff/venv-muq/bin/python
OUT=/Users/wycm/Documents/网易云热榜抓取/research/music-kb-similarity/viz
HF_HUB_OFFLINE=1 NUMBA_CACHE_DIR=/tmp/music-kb-numba \
  "$VENV" runners/library-viz/build_viz.py \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --output-dir "$OUT" \
  [--audio-root /path/to/music_downloads/KugouMusicClient] \
  [--reuse-clips]
```

`--reuse-clips` 复用输出目录里已有的试听片段，只重算投影与数据。

## 查看

```sh
"$OUT/serve.sh"     # http://127.0.0.1:8792/
```

页面：左画布 + 右详情面板；顶部切换整体听感 / 节奏 / 音色；滚轮缩放、拖拽平移、双击重置；搜索高亮；簇图例可隐藏。点击点查看歌曲、试听和三通道 Top-5 邻域。

## 数据契约

`data.js` 暴露 `window.VIZ_DATA`：

- `channels[]`：`key`、`label`、`model_id`、`recipe`、`clusters[]`、`songs[]`。
- `clusters[]`：`{id, name, size, color}`；`id=-1` 为散点。
- `songs[]`：`{id, title, artists, cluster, x, y, clip, neighbors: [[index, distance], ...]}`。
- `x/y` 归一化到 `[-1, 1]`；`clip` 为相对路径，缺失时为空串。

## 说明

- 聚类是探索性浏览：HDBSCAN 在 UMAP 30 维空间上运行，簇对参数敏感，**不是**质量结论。
- 邻域与距离来自对应通道的原始向量空间（余弦距离，越小越像）。
- 页面与试听片段是生成物，不提交仓库；服务只绑定 `127.0.0.1`。
