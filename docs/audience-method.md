# 受众调研方法（SOP v1.1 · audience-method）

- 状态：**v1 方法底稿**（2026-10-20），供 `music-kb-audience-reverse` skill、#140 标签投影、#141 榜单证据、#142 回测共同引用。
- 唯一上位依据：Spec v2.1（<https://github.com/chen-da-pang/music-analysis-kb/issues/137#issuecomment-6286448586>）与 ADR-0005；字段契约见 `docs/import-contract.md` §Audience import contract，表结构见 `docs/schema.md`。
- 方法来源：`通用歌曲受众逆向工程SOP_v1.md`、`handoff_SOP版_歌曲受众测试.md`、`审阅版.md`（原型 v2）、`chart-signal-design.md`、`run-report-2026-10-20.md`（均在发布端本机 research/，不进 git）。
- 一句话：**受众 ≈ 各发行入口受众的加权混合；先分版本，再合总体。** 输出方向、来源与缺口，不输出平台报表、不输出单一「受众分」。

## 0. 目的与边界

做：单歌 × 时间窗，用公开证据合成一条受众记录——0–1 区间（`value_low/mid/high`）+ 证据等级 ④ + 来源 + 缺口，写入 `audience_*` 表，可查询、可审计、可随快照分发。

不做（v1 写死）：分平台画像；`rolling_30d` 真实人口结构；省份/城市层级的数据采集；排序加权；抓榜（榜单抓取属 #141）；飞瓜 CLI 及任何登录态抓取；把榜单写成占比；把两类证据合成「受众分」。

数据边界：原始榜单快照与调研报告留发布端本机，不进 git、不进客户端快照；DB 只存派生记录、来源引用与 `payload_path` / `local_path`。快照裁剪时 `local_path` 会被清空。

## 1. 核心原理

厂牌的版本排期、联名选择、物料投放，是平台后台受众数据的外显。把每个改编版当成一个定向投放入口，**先分版本拆人群、再按权重聚合**，比直接看「谁在拍」更接近真实受众结构。

退化路径：只有原版、无任何改编时，退化为「平台结构 + 物料形态 + 大盘基准」，置信度降一档。

## 2. L0 定标

1. **确认歌曲身份**：原唱、词曲、厂牌、发行日期；排除同名异曲、翻唱误认。
2. **建版本入口清单**：原唱、对唱/合唱、女声/男声翻唱、DJ/Remix、童声、剪辑版、歌词版、改名版、乐器版、KTV 伴奏、车载/8D、方言版、国粤版、影视/综艺版。每版记录演唱者、发行日期、编曲、平台。
   这些行进入 `audience_entry`：`entry_type`（枚举见 import-contract）、`performer`、`released_at`、`platform`、`relation_to_original`（`same_master|alternate_version|cover_remix|unknown`）、`weight_basis`（`ownercount` / 排期 / 等权 / `none`）。未知 performer / released_at 写空字符串 `""`。
3. **划时间窗**：`first_wave`（发行后约 2–3 周）与 `full_lifecycle`（至今）；两套分别作答，不得混写成一个数。
4. 算不出的窗口写 `profile.status='unavailable'` + `coverage_note`；缺口只写 `coverage_note`，**不造 observation**。

## 3. L1 六层证据

每条结论标等级：① 平台公开数据 ② 媒体报道 ③ 评论/二创/榜单旁证 ④ 推断。

| 层 | 抓什么 | 怎么用 | 风险 |
|---|---|---|---|
| 1 发行排期 | 版本上线顺序、联名艺人、KTV/伴奏同步、平台首发、DJ 再版时间 | 厂牌排期 = 后台数据外显，判断每个版本投谁 | 分发日 ≠ 上架日 |
| 2 关联艺人受众池 | 每个合作/翻唱歌手的公开受众画像（性别、年龄、赛道） | 把歌手画像迁移到对应版本再聚合 | 歌手粉丝 ≠ 单曲听众 |
| 3 平台结构 | 歌在酷狗/酷我/汽水/QQ/网易云/抖音/快手/视频号的数据分布 | 平台用户结构 → 城市层级/年龄先验 | 平台口径不能串用 |
| 4 物料形态 | KTV 伴唱、车载/8D、DJ、舞蹈/广场舞、手势舞、亲子萌娃、情感口播、影视剪辑、旅拍、方言、挑战赛 | 只当「场景证据」 | 创作者 ≠ 消费者；出镜者性别 ≠ 听众性别 |
| 5 需求侧信号 | 热榜、搜索指数、收藏、分享、使用量、评论 | 验证「有人在用/在听」，校正二创供给偏差 | 评论者 ≠ 听众；二创数 ≠ 受众数 |
| 6 大盘基准 | 同类赛道公开报告、平台官方公开数据 | 只做方向对照（偏老/偏年轻、偏高线/偏下沉） | 只做方向，不套本歌数值 |

### 证据等级映射（spec §5，写死）

| 事实 | `source.evidence_level` | `observation.evidence_level` | `observation.is_proxy` |
|---|---|---|---|
| 榜单页 / 榜单快照本身 | `1_platform_public` | —（不造 observation） | — |
| 「上榜 ⇒ 进过某代际/场景/圈层池」 | `1_platform_public` | `3_proxy` | `1` |
| 调研报告的公开网页证据 | `1` / `2` / `3` 按实际 | `4_inferred` | `1` |
| 用户显式提供的平台后台导出 | `1_platform_public` | `1_platform_public` | `0` |
| 缺口 | —（写 `coverage_note`） | 不造 observation | — |

来源等级看**页面本身是什么**；观测等级看**结论是怎么来的**；每个数字必须有 `source_id`。

## 4. L2 分版本加权

1. **先分版本，再合总体**：先给每个入口一个人群假设（可以很粗：性别方向 + 年龄区间 + 场景），再合成；禁止先给总体再拆。
2. 权重优先级（从强到弱）：版本级使用/播放/收藏/榜单数据（如有）→ 版本数量 × 排期顺序（首发 > 跟进 > 衍生）→ 等权。每级都要写明用了什么、缺什么、误差往哪边偏。
3. 输出必须同时含「分版本画像表」与「加权总体」；缺数据的版本标缺口，不默认补。
4. 若某版本受众与总体相反（女声版偏女、DJ 版偏男），在结论里点名，不要被平均掉。

数值只以 0–1 区间（`value_low ≤ value_mid ≤ value_high`）写进 `audience_observation`，全部 `4_inferred`；只有方向可进标签桶。

## 5. L3 八条反偏见

逐条答「是 / 否 / 无法判断」并给判定证据；答「是」必须先修正再输出；「无法判断」写缺口。

1. 是否把出镜者/创作者性别当成听众性别？（舞蹈、亲子、情感口播类尤其容易）
2. 是否用单场演出、单个地方账号、歌手籍贯推省份分布？
3. 是否把内容形态直接映射年龄（广场舞→中老年、童声→宝妈、DJ→司机）？
4. 是否把后续翻红波的受众当成首波受众？
5. 是否串用平台口径（网易云/QQ ≠ 抖音；歌手粉丝 ≠ 歌曲听众）？
6. 是否把翻唱/改名版的受众直接算进原版？
7. 是否有引语/评论不是真实抓取（搜索摘要或模型生成）？
8. 是否有数字指不到来源？

**上榜 ≠ 受众占比**要单独写进检查：榜单条目说明「这首歌进过哪个池子」，不能给出任何占比，也不能跨榜平均成人群结构。

## 6. 时间窗与首波（v1.1 重点）

SOP v1.1 的五条修订（基于山风实测）：

1. **必须分时间窗加权**：输出首波与后续翻红波两套，再给全周期；基线按首波采集时只用首波对比，否则系统性低估男性/31–40、高估女性/50+。
2. **省份无数据只写「不可核」**：不输出占位排序（占位排序会被下游误引用）。
3. **权重机制要声明与基线口径的差异**：酷狗 ownercount 加权 ≠ 视频详情受众加权 ≠ 播放量加权；跨口径只比方向，不比数值。
4. **优先押对内容形态第一顺位**：先看平台模板/二创形态份额（口播、翻唱、DJ、舞蹈、亲子、影视），再看人群。
5. **首波权重的快速估算法**：用分版本表发布日期 × 版本热度代理，把「发布日期 ≤ 基线窗口末日」的版本单独加权，其余排除；重算性别/年龄，通常比全周期更贴首波基线。

## 7. 置信度

**榜单证据（spec §5，单期可算）**：同一期快照内命中 1 榜 = `low`；2–3 榜 = `medium`；≥4 榜，或（代际 2 榜 + 任一场景/圈层）= `high`。多期不参与 v1 置信度（persistence 阈值 v1 不设）；不做分族置信度（`audience_observation` 无 confidence 列）。

弱/中/中-强 → `low / medium / high`。

**调研画像**：默认 `low`（④推断、无平台受众报表）；仅当每个上报维度都有 `source_id` 且两个窗口都处理完成，可标 `medium`；`high` 只留给 ① 平台后台导出（公开调研通常拿不到）。山风回测通过只说明方向成立，不自动升级置信度（基线只有 1 首）。

## 8. 标签桶映射（spec §7 v1，写死）

标签投影属 #140；本方法只保证 `bucket` 取值可被投影。规则：`tag.namespace ∈ {audience, audience-hint}`、`tag.name` 路径式键、完整前缀注册为 alias；**`value_*` 数值不进 tag，bucket 标签可以进**。

### 调研侧 `audience`

| dimension | bucket 取值 | 投影 tag |
|---|---|---|
| `age` | `18-23` `24-30` `31-40` `41-50` `50+` | `audience/age/<bucket>` |
| `gender` | `male` `female` | `audience/gender/<bucket>` |
| `city_tier` | `一线/新一线` `二线` `三线及以下+县域乡镇` | `audience/city-tier/{high,mid,low}`（映射：一线/新一线=high，二线=mid，三线及以下+县域乡镇=low） |

其余维度（province / life_stage / scene / creator / emotion）只入库，v1 不投影标签。

### 榜单侧 `audience-hint`（24 榜实况，v1 无 KTV、无热度桶）

- `generation_hint` → `gen/00后|90后|80后|cross-00-90|cross-80-90`（研究里的 `cross:00后+90后` 规范化为 `cross-00-90`）
- `scene` → `scene/shortvideo|dj|variety|soundtrack|recognition|channels`
- `tribe` → `tribe/acg|electronic|rap|folk|rock|rnb|jazz|country|classical|guofeng|opera`（`guofeng` = 国潮+国乐；`opera` = 粤剧+京剧+南音）

chart → bucket 映射（写死）：

| rank_id → bucket | rank_id → bucket | rank_id → bucket |
|---|---|---|
| 49224→gen/00后 | 49223→gen/90后 | 49225→gen/80后 |
| 52144→scene/shortvideo | 52767→scene/shortvideo | 24971→scene/dj |
| 46910→scene/variety | 33163→scene/soundtrack | 37361→scene/recognition |
| 100530→scene/channels | 33162→tribe/acg | 33160→tribe/electronic |
| 44412→tribe/rap | 51341→tribe/folk | 59896→tribe/rock |
| 59895→tribe/rnb | 59897→tribe/jazz | 59898→tribe/country |
| 59899→tribe/classical | 85897→tribe/guofeng | 80025→tribe/guofeng |
| 108332→tribe/opera | 108333→tribe/opera | 108335→tribe/opera |

## 9. 榜单证据的消费方式（调研 skill 侧）

来源：`music-kb --json get-audience <recording_id>` 返回的 `chart_evidence` 分组，或调用方传入的证据文件；调研 skill 不抓榜。

引用方式：把用到的榜单来源复制进输出 `sources[]`——保留 `source_platform='kugou'`、`source_key='chart:<rank_id>:<YYYY-MM-DD>'`、`title`（榜名）、`fetched_at`，并写 `source_type='chart_snapshot'`、`evidence_level='1_platform_public'`、`coverage_note='榜内热度≠人口构成'`、`local_path`（如 master 中有）。据此得出的观察仍是 `4_inferred`；名次只写 `observation.rank`，绝不写成 `value_*`。

三种常见误读要避免：① 上榜 ≠ 占比；② 单期在榜 ≠ persistence（多期才谈稳定性）；③ 多榜命中 ≠ 人群冲突，是「广谱/多场景」信号。`get-audience` 的 `chart_evidence_note` 已固定声明「榜单证据只代表进过哪些池子，不代表受众占比」。

## 10. 输出与回测

输出：按 `docs/import-contract.md` 的 audience JSONL 契约；运行步骤见 `music-kb-audience-reverse` skill 的 Step 6。两条 profile（`first_wave`、`full_lifecycle`）分别写，`target_platform='douyin'`、`method_id='version-matrix'`、`method_version='v1.1'`、`is_proxy=1`。

回测（#142，写死）：山风 profile 的 `full_lifecycle` `value_mid` = age `0.05/0.14/0.32/0.32/0.17`、gender male `0.56`；基线 = age `0.0182/0.1297/0.4402/0.2834/0.1284`、male `0.6040`、31–50 = `0.7236`。指标：年龄 L1 = Σ(5 档)|p−q|（pp，不除 2）= **24.05pp**；31–50 误差 = |0.6400 − 0.7236| = **8.36pp**；性别方向 correct。通过线：性别方向正确、31–50 误差 ≤10pp、年龄 L1 ≤25pp、每个数字有来源、省份/城市无数据标「不可核」；榜单证据不参与画像数值回测。第二首若无离线基线/画像记 `blocked: 无离线输入`，不伪造基线。

## 11. 已验证的失败模式（不要原样重复）

- 只听音频 → 套曲风刻板印象（都市白领/一线）。
- 只套赛道先验 → 结论可能对，但证据是编的（评论区引语为生成物）。
- 只看内容供给 → 把「谁在拍」当「谁在听」（广场舞/幼教内容 → 女性为主）。
- 只信单点事件 → 省份排序被一场演出/一个地方账号带偏。
- 版本矩阵逆推 + 按版本加权 → 聚合方向最接近基线（当前采用）。

## 12. 维护约定

改本文件时同步三处：Spec §5/§7（若规则变了先回到 spec）、`docs/import-contract.md`（字段/枚举）、`plugins/music-kb/skills/music-kb-audience-reverse/SKILL.md`（运行步骤）。`docs/audience-method.md` 是方法底稿（人读+agent 参考），SKILL.md 是运行版（步骤+完成判据）；两者冲突时以 Spec v2.1 与 import contract 为最高事实源。
