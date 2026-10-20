# 发布者主库中的独立音频向量通道

## Status

accepted (2026-10-18); spec #123, foundation ticket #125.

## 决策

- 向量存入发布者 SQLite 主库，不使用 sidecar。schema v8 新增
  `embedding_model` 和 `recording_embedding`；v7 master 经 `init` 增量迁移，
  不改既有分析、标签、歌词、来源或 FTS 数据。旧只读库不会被查询隐式升级。
- 模型 id 指向不可变的模型名、配方、模态、维度、度量、许可证及元数据；
  `(model_name, recipe, modality)` 唯一，换配方使用新 id。
- 向量以 little-endian float32 BLOB 保存，身份只认
  `source_audio_sha256 == recording.audio_sha256`，不以歌名匹配。
  主键为 `(recording_id, model_id, factor)`；删除 recording 级联删除向量，
  有向量引用的模型不可删除。模型登记和整批导入在同一事务中，失败全部回滚。
- 三个通道独立：整体听感为 MuQ-MuLan v3（512 维，factor `''`），
  节奏为 MERIT rhythm（128 维），音色为 MERIT timbre（128 维）。
  不保存旋律，不做融合分，不改变 `search` / `discover` / `recommend`。
- 度量是余弦距离 `1 - cosine`，范围 0–2，越小越像；展示三位小数，
  不用百分比或阈值。以未舍入距离排序、recording_id 打破同分，之后才舍入；
  同归一化标题只保留最近版本，附其他参与检索版本数，排除种子 recording。
- `meta.default_embedding_model` 初始为空，首次成功导入整体音频向量后设为该
  模型 id，后续导入不会覆盖。节奏/音色自动使用该因子的唯一音频模型；
  若存在多个配方，必须显式指定 model id，避免暗中选择配方。
- 导入检查字节长度、有限非零向量、音频身份与范数。范数使用 float64
  累加计算，提供的范数以相对容差 `1e-5`、绝对容差 `1e-6` 校验。
  JSONL 不提供范数时按向量计算；`validate` 同样审计落库范数和音频归属。

## Considered Options

- **sidecar 向量文件**：需要额外同步和身份一致性协议，否决。
- **三通道融合分**：掩盖律动与声音质感的差异，否决。
- **ANN / sqlite-vec**：当前库规模先采用 NumPy 内存穷举，不额外引入索引服务。

## Consequences

SQLite backup 天然携带向量，后续快照分发不需要单独的向量文件。
模型推理、runner/bootstrap、周更嵌入编排、MCP、快照分发升级及 Suno 评估
属于其他工单，本工单不实现。客户端应使用兼容的 v8 库；只读打开 v7 库会明确报错。

许可证：MuQ-MuLan 与 MERT 权重为 CC-BY-NC-4.0，MERIT heads 为 MIT。
本轮限内部研究用途；产品化前必须取得授权或替换受限权重。
