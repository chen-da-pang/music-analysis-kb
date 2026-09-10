# Fallback 下载并行度上限从 2 放宽到 6

## Status

accepted (2026-09-10)

## 决策

fallback 下载的并行 worker 上限从 2（`--parallelism choices=(1,2)`）放宽到 6，
默认值仍为 2。周更批量要吃满并行度时显式传 `--parallelism 6`。

## 依据

- P=2 的原始理由是防止多 worker 竞争共享 inventory/progress。分片隔离设计
  （每分片私有 queue/inventory 副本/progress/staging + 唯一串行合并器）已经
  在结构上解决了这个问题——并行度只是分片数量，不再是共享状态竞争。
- 带宽实测（#120）：K=1→10.8 MB/s、K=6→36.1 MB/s（≈290 Mbps，拐点）、
  K=9→30.9 回落。P=2 只用了约 1/3 可用带宽。
- 真正的瓶颈是逐歌搜索匹配（~16-47s/首），并行度的收益在搜索侧而非传输侧。

## Consequences

- 更高并行度会放大平台侧限流风险：若 P=6 下 no_results 率显著上升，应先回
  P=4 再查 CDN 限流证据（可对比同批歌在 P=2 下的命中分布）。
- 合并器语义不变：所有分片到达终态后一次串行合并，中断恢复仍走 progress
  驱动的跳过逻辑。
- 运行中的批次不适用（分片布局在 prepare 时固定）；新并行度从下一次
  fallback 调用（无结果重试轮、后续周更）生效。
