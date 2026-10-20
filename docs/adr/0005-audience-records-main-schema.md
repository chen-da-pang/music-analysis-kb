# 受众记录进入主库；榜单作为版本/场景证据之一

## Status

accepted (2026-10-20; 依据 [Spec v2.1](https://github.com/chen-da-pang/music-analysis-kb/issues/137#issuecomment-6286448586)，实现于 #139)。

关联：#137（受众数据调研与 Spec v2.1）、#139（schema v9 + `import-audience` + `get-audience`）、#141（24 榜抓取、匹配与周更接入）、`research/audience-137-prototype/`（受众记录原型）。

## 决策

按《通用歌曲受众逆向工程 SOP v1》和受众记录原型，在 music-kb 主库实现**受众记录**，不建独立的“标签库”或 sidecar：

- `audience_profile`：一首歌 × 时间窗（`first_wave` / `full_lifecycle` /
  `chart_snapshot`；`rolling_30d` 只保留枚举，v1 不写入）的一条全网公开证据合成；
  必须声明 `target_platform`（调研画像 v1 固定 `douyin`，榜单证据固定
  `unspecified`），且 `method_id='chart_signal_v1'` 时必须为 `unspecified`。
- `audience_observation`：年龄、性别、省份、城市层级、生活状态、场景、创作者、
  情绪，以及榜单证据专用的代际/场景/圈层提示等维度；份额用 0–1 区间
  （`value_low ≤ value_mid ≤ value_high`，非 NULL 相邻值必须满足），榜单观察带
  `rank`，不写任何人口占比。
- `audience_entry`：版本入口（原唱/翻唱/DJ/童声/影视/综艺/方言等）建模为独立表，
  带 `entry_type`、表演者、发行时间、与原唱关系与权重依据；`performer` /
  `released_at` 未知写空字符串，保证 `UNIQUE(recording_id, entry_type,
  performer, released_at)` 生效。
- `audience_source`：来源平台、`source_key`、URL、抓取时间、证据等级、覆盖说明，
  以及发布端本机 `local_path`；`UNIQUE(source_platform, source_key)`，重复导入
  原地 upsert。原始榜单快照与调研报告留发布端本机，不进 git、不进客户端快照。

榜单数据作为**受众证据**进入上述记录：每条入榜事实是一个版本/场景/圈层的公开
代理证据（`evidence_level='3_proxy'`、`is_proxy=1`），用于版本权重与人群假设，
不直接写成受众占比。四张表一次随 schema v8→v9 增量迁移创建，旧数据不动，迁移可
重复执行。

## 已定结论（Spec v2.1 对原开放问题的拍板）

1. **版本入口建模**：使用独立 `audience_entry` 表（v9 四表之一），不复用
   `audience_source` 行；原型的“版本入口先欠着”作废。
2. **导入命令**：新增 `import-audience`，接受 `profile`（调研，默认）与
   `chart_evidence`（榜单）两种 `kind`；不扩展 `import-analysis`，也不复用 CNB
   campaign delivery 契约。
3. **原始产物**：DB 只存派生记录、来源引用与 `payload_path` / `local_path` 路径
   字符串；原始榜单 JSON 与调研报告留在发布端本机，不进 git、不进客户端快照，
   快照裁剪时同步清空 `local_path`。
4. **标签投影**：稳定结论投影为弱信号标签（`recording_tag(role='audience')` /
   `audience-hint`，路径式 alias），绝不生成人口占比标签；投影实现归 #140，本
   ADR 只确认方向。
5. **快照内容**：客户端快照带每个
   `(recording_id, target_platform, time_window, method_id)` 的最新一条 profile
   （`researched_at DESC, method_version DESC, id DESC`），master 保留全量历史；
   entries 带所有 canonical recording 的全部 entries；孤儿 source 在裁剪时删除。
6. **首波缺口处理**：首波年龄等算不出的维度先入库——`status='available'` 照常写
   已有观测，缺口只写 `coverage_note`、不造 observation；整个窗口算不出时写
   `status='unavailable'`。

## 为什么

- SOP 的核心：**受众 ≈ 各发行入口受众的加权混合；先分版本，再合总体；版本是
  证据，不是歧义。**
- Bill 已决定开发，受众记录要在库内参与真实工作验证，因此进入主库；本地
  sidecar 方案作废。
- 原始榜单条目属于生产抓取数据，按仓库数据边界不能提交 git；派生受众记录、
  证据与来源引用可以入库并随快照分发。
- 受众证据与 canonical 分析解耦（不写 `analysis_revision`），因此新增受众记录
  的导入失败不会污染已发布的分析语义；覆盖率约 18%，`validate` 只校验新表自身
  不变式，**不设 `require_audience` 硬门**。

## 考虑过的方案

- **本地 sidecar（拒绝）**：无法进入真实检索/分发链路。
- **只输出 CSV 报告（拒绝）**：不能按 recording 查询。
- **主库受众记录 + 榜单证据（选择）**：统一四表 + 一个导入命令 + 一个读回命令，
  周更只记录覆盖缺口、不阻塞发布。

## Consequences

- schema v8→v9 是增量迁移：新增四表与索引，旧数据不动，可重复执行；客户端插件
  必须先升到 0.9.0 才能打开 v9 快照，发布顺序见 `docs/operations.md`。
- 本 PR（#139）只 bump 版本、迁移与文档，不创建/发布 v9 快照、不升级 peer；
  这两步是合并后的发布 runbook。
- 榜单证据只能表述为「进过哪些池子」，对外的任何受众占比表述都不受支持。
