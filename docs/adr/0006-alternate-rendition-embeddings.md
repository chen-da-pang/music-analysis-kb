# 跨 rendition 音频嵌入：用 source_audio_sha256 承载实际嵌入字节（不升 schema）

## Status

accepted（2026-10-28；用户决策「mp3 就够了」）。关联：1571 首缺失音频恢复
（`research/similarity-backfill-20261028/`）、ADR-0003（publisher 音频向量）、
ADR-0004（MERIT 多窗口采样）。

## 背景

1571 首 canonical recordings 的本地音频在 2026-07 清理后已被删除，master 的
`recording.audio_sha256` 仍指向当年那份（多为 Kugou FLAC）原文件。实测：

- 酷狗免费接口只能拿到 128k mp3 转码（533/1571 首可下），原画质全部在付费墙后；
- 免费重下的字节与当年文件不同（抽样 sha256 匹配 0/15）；
- 现有两处硬门槛（`embed_audio.py` 扫描、`import_embeddings` 校验）都要求
  `sha256(音频) == recording.audio_sha256`，转码件无法进入向量链路。

用户接受「向量来自 128k 转码、非原始字节」，要求不升 schema。

## 决策

不做 schema 变更（保持 v9、不加 `rendition` 列），用**现有列的组合语义**承载来源：

- `recording_embedding.source_audio_sha256` 的语义定为「**实际被嵌入的音频文件的
  sha256**」。canonical 行等于 `recording.audio_sha256`；alternate 行（跨 rendition）
  不等于它——这个不等式就是可审计的 alternate 标记，查询时 join `recording` 推导。
- `recording.audio_sha256` 永远不被修改。
- runner（`runners/audio-embed/embed_audio.py`）新增 `--identity-map`：JSONL 行
  `{recording_id, path, downloaded_sha256, asset_kind=alternate, platform_file_hash,
  source_url, served_quality}`。扫描时按 map 校验文件 sha；输出行 `audio_sha256`
  写实际文件 sha，并带 `asset_kind` 与来源字段。不带 flag 时行为与 v9 完全一致。
- `music-kb import-embeddings` 新增 `--allow-alternate-rendition`：默认仍拒绝
  sha 不匹配；flag 下允许 `audio_sha256 != recording.audio_sha256`，并把实际文件
  sha 写入 `source_audio_sha256`。JSON 输出区分 `imported_canonical` /
  `imported_alternate`。alternate 行**不允许覆盖**已存在的 canonical 行（反向升级
  允许：canonical 导入会覆盖 alternate）。
- `validate()`：两个 sha 列都必须是 64 位小写 hex；canonical 行两值相等、alternate
  行不相等（由列比较推导，无新列）；不再把「source != recording sha」当错误。
- snapshot / publish / 客户端 schema 均不变；发布端与客户端继续使用 v9。

## 代价与边界

- 没有结构字段区分「alternate」与「损坏的 canonical 行」，只能靠 sha 比较推导；
  `validate()` 因此只能校验 sha 形状与关系，不能证明某行一定来自合法下载。发布端
  通过 `--identity-map` 的下载清单与 identity-map receipt 保留证据链。
- 向量来自 128k 转码/不同母带：overall（MuQ 512d）相对稳健，MERIT rhythm/timbre
  对音色更敏感。任何相似度结论不得声称「同一音频」；报告与 UI 需注明 rendition。
- alternate 覆盖只限于「平台当前还能免费拿到的版本」；平台换件/下架的曲目无法恢复。

## 后果

- 向量覆盖可从 1114/2685（41.5%）提升到最多 1647/2685（61.4%）。
- 不引入迁移与快照版本风险；回滚只需回退代码（DB 行仍是合法 v9 数据，alternate
  行可用 `DELETE FROM recording_embedding WHERE source_audio_sha256 != (SELECT audio_sha256 ...)`
  精确移除）。
