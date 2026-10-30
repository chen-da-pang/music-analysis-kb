# Fallback 下载：歌曲内多源并行 + 外层并发 8

## Status

accepted (2026-10-28)

## Context

现有 fallback（ADR-0002）解决的是「跨进程分片并行」，但一首歌内部仍是
Migu → QQ → Kuwo 串行：每源最坏等满 100s 超时，好歌会与无源歌排在同一
worker 后面。2026-10-28 的 A/B 基准（research/similarity-backfill-20261028/
rolling/dl-bench/BENCH-REPORT.md）用固定样本（B=已成功池随机 12 首、A=无向量池
前 24 首，seed=42）测了六类优化，结论是：

- 源并行（Netease 官方直连 + Migu + QQ + Kuwo，先到先得）：B 组 12/12 保持
  100%，p50 38.8s→14.6s（-62%），wall 118.4s→46.0s（-61%），原始下载体积
  815MB→256MB（-69%，并行时常由酷我 128k 先返回）；A 组失败判定 wall
  356s→52s（-85%），救回数不变（1/12）。
- 外层并发 6→8：B 组 wall 118.4s→84.6s（-29%），成功率不变；8→10 无进一步
  收益（84.9s），故 8 是拐点。
- 不采纳：短搜索超时 25s（B 组掉到 91.7%，QQ 慢搜索丢歌）、彻底跳过无损
  （B 组仅 33-50%，尾部歌常只有无损源）、结果缓存（p50 与基线持平）、
  Netease 直连单独前插（A 组无救回、串行 p50 133s）。

## Decision

1. download_music_fallback.py 在 profile 声明 source_parallel=true 时启用
   歌曲内多源并行：parallel_sources 默认
   [NeteaseDirect, MiguMusicClient, QQMusicClient, KuwoMusicClient]。
   Netease 使用 scripts/netease_direct.py 的官方 cloudsearch + eapi(standard)
   直连，不使用 musicdl 的 NeteaseMusicClient（其搜索会串行调用约 30 个第三方
   解析镜像，单曲可挂数分钟）。
2. 每源搜索/下载超时各 100s（search_timeout_seconds /
   download_timeout_seconds），整体每首 deadline 120s
   （per_song_deadline_seconds），先到先得；其它源线程放弃等待。
3. 外层并发默认 8（outer_concurrency），由进程内线程池处理队列，
   inventory/progress 的写入由单锁保护、逐项原子落盘，保持一份持久状态。
4. 保留「无损下载 → ffmpeg 转 128k → 删原件」回退
   （lossless_transcode_128k=true），不跳过无损；并行时通常先返回的是
   128k mp3，只有只剩无损源时才走转码。
5. 代码默认行为不变：profile 未声明 source_parallel=true 时仍是旧的串行源
   逻辑；回滚只需把该字段去掉或设为 false（sources 数组保持原三源顺序）。

## Evidence

见 research/similarity-backfill-20261028/rolling/dl-bench/BENCH-REPORT.md
的指标总表与「落地验证」章节；baseline/combo_fast 对比是在同一固定样本上
单变量测得。落地后用同一样本重跑实际代码路径：B 组 12/12、p50 约 14-18s。

## Consequences

- fallback 每首歌会同时占用最多 4 个源的搜索/下载线程；外层 8 并发下峰值
  线程数约 32 + 队列线程，属可接受范围（原分片方案本身已多进程）。
- 平台限流风险：若某源在并行下错误率上升，可把该源从 parallel_sources
  移除而不改代码。
- 进度/库存写盘比旧逐项串行更频繁（每首歌一次），但仍有单锁与原子替换，
  中断恢复仍由 progress + inventory 驱动。
- 下一轮周更开始生效；运行中的批次不受影响，回滚通过 profile 字段完成。
