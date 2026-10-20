# 受众研究方法回测（#142）

日期：2026-10-20
ticket：[#142](https://github.com/chen-da-pang/music-analysis-kb/issues/142) · `feat: 山风回测与验收报告（第二首 blocked 口径）`
上位依据：[Spec v2.1](https://github.com/chen-da-pang/music-analysis-kb/issues/137#issuecomment-6286448586)（唯一实现依据）
状态：**山风 PASS**；**第二首 `blocked: 无离线输入`**（不产出 PASS/FAIL）
约束：全程离线——不联网、不跑飞瓜 CLI、不新建或改写任何基线数据。

## 一句话结论

山风 `full_lifecycle` 画像对票内写死基线的复算与票内预期完全一致：**年龄 L1 = 24.05pp**（≤25 通过）、**31–50 误差 = 8.36pp**（≤10 通过）、**性别方向 correct** → **山风 PASS**，偏差 0.00pp。第二首没有任何离线基线或画像输入，按票记 `blocked: 无离线输入`，不给 PASS/FAIL、不伪造基线；作为 fallback，对山风同一条 profile 做契约字段 / 八条反偏见 / 每条数值有 `source_id` / 无平台占比事实自洽检查，**全部通过**。

复核补充（code-review 复核项）：① **每个进入回测的数字都能指回 `source_id`**——8/8 条带数值 observation 均解析到 `src_shanfeng_kugou_search`（§3）；② **省份 / 城市层级无数据，标「不可核」**——基线没有省份/城市层级字段，画像只有一条 4_inferred 的城市层级推断，本回测不比较、报告不引用（§窗口与口径偏差 4、`not_compared`）；③ **榜单证据导入不改变调研画像数值**——`chart_snapshot` 证据与本回测的 `full_lifecycle` 数值隔离，导入后 8 条 research observation 与 profile 逐字段不变、L1/31–50/性别方向复算仍为 24.05 / 8.36 / correct（§自洽检查 5，测试锁定）。

## 输入（写死，不刷新）

| 项 | 值 |
|---|---|
| 画像 fixture | [`tests/fixtures/audience/shanfeng.jsonl`](../../plugins/music-kb/tests/fixtures/audience/shanfeng.jsonl) 的 `full_lifecycle` profile（`prof_shanfeng_full_lifecycle`，v2 契约，由 #139 升级） |
| 画像年龄 mid | 18-23 = 0.05 ｜ 24-30 = 0.14 ｜ 31-40 = 0.32 ｜ 41-50 = 0.32 ｜ 50+ = 0.17（合计 1.00） |
| 画像性别 mid | male = 0.56 ｜ female = 0.44（合计 1.00） |
| 画像窗口 | `full_lifecycle`，`window_start=2026-07-20`，`window_end=2026-10-19`，`researched_at=2026-10-20` |
| 基线 | 飞瓜 **2026-07-05 ~ 2026-08-07**，**388/628 条视频详情**；口径 = **「使用该 BGM 的高表现视频观众」**，不是全量抖音用户、也不是纯听歌用户 |
| 基线 age | `{"18-23": 0.0182, "24-30": 0.1297, "31-40": 0.4402, "41-50": 0.2834, "50+": 0.1284}` |
| 基线 gender | `{"male": 0.6040, "female": 0.3960}` |

基线与窗口逐字来自 #142 正文「输入（写死）」，**不依赖仓库外文件**；画像数值来自 fixture 的 `value_mid`（0–1），不读 `value_low/high`。

> fixture 修正（review fix #3）：原 `coverage_note` 写「年龄未重算前先留空」，与同一条 profile 里的 5 个 age `value_mid` 矛盾。现改为声明平台口径差异，并明确「年龄/性别/城市层级均为 `4_inferred` 推断区间，不是平台报表」——数值本身未改。

## 方法（写死口径）

- **年龄 L1** = Σ（5 档）|p_i − q_i| × 100，单位 pp，**不除 2**；p = 画像 `value_mid`，q = 票内基线。
- **31–50** = p(31-40) + p(41-50)，q(31-40) + q(41-50)，误差 = |Σp − Σq| × 100 pp；**由两档相加，不读取任何冗余汇总字段**。
- **性别方向**：画像 male 份额与基线 male 份额是否同侧偏离 50%（双侧都 >50% 即 direction correct）。
- **窗口**固定 `full_lifecycle`；不混用 `first_wave` 或 `chart_snapshot`。
- **通过线**：性别方向正确 且 31–50 误差 ≤10pp 且 年龄 L1 ≤25pp。

复现（脚本内基线为常量，无“换基线”参数）：

```bash
cd plugins/music-kb
uv run --offline python scripts/audience_backtest.py            # 人读回测单
uv run --offline python scripts/audience_backtest.py --json     # 机器可读证据
uv run --offline pytest -q tests/test_audience_backtest.py      # 断言票内预期值
```

## 复算结果

| 年龄档 | 画像 p | 基线 q | \|Δ\| pp |
|---|---:|---:|---:|
| 18-23 | 0.0500 | 0.0182 | 3.18 |
| 24-30 | 0.1400 | 0.1297 | 1.03 |
| 31-40 | 0.3200 | 0.4402 | **12.02** |
| 41-50 | 0.3200 | 0.2834 | 3.66 |
| 50+ | 0.1700 | 0.1284 | 4.16 |
| **L1** | 合计 1.0000 | 合计 1.0000 | **L1 = 24.05** |

- **年龄 L1 = 3.18 + 1.03 + 12.02 + 3.66 + 4.16 = 24.05pp**（≤25 → 通过；余量 0.95pp）
- **31–50**：p = 0.32 + 0.32 = **0.6400**，q = 0.4402 + 0.2834 = **0.7236**，误差 = |0.64 − 0.7236| × 100 = **8.36pp**（≤10 → 通过；余量 1.64pp）
- **性别**：male 0.56 vs 基线 0.6040，双侧均 >50% → **direction correct**（通过）
- **与票内预期偏差**：L1 24.05 vs 预期 24.05 → **0.00pp**；31–50 8.36 vs 预期 8.36 → **0.00pp**；性别方向同为 correct。复算没有出现需要另找理由解释的数值漂移。

| 检查项 | 阈值 | 复算值 | 结论 |
|---|---|---|---|
| 年龄 L1 | ≤25pp | 24.05pp | PASS |
| 31–50 误差 | ≤10pp | 8.36pp | PASS |
| 性别方向 | correct | correct | PASS |
| **山风总判定** | 三项全过 | — | **PASS** |

## 山风 PASS/FAIL 与理由

**PASS。** 理由：三项写死检查全部过线，且复算值与票内预期逐位一致；fixture 的年龄五档、性别两档均合计 1.00，数值在 [0,1] 区间，`31-50` 由两档相加得到、未使用冗余字段，L1 按“不除 2”口径计算。回测对象是已提交的 v2 契约 fixture profile，不是临时手写数字；测试另行验证该 fixture 可经 `import-audience` 写入临时库、从库内 observation 复算出同一组数值。

必须同时说明 PASS 的边界：L1 距通过线只剩 0.95pp，本质是**擦线通过**；单案例 n=1 的 PASS 只证明“这份山风画像复算过线”，不能证明方法在第二首或全库上成立。按票「不通过只调方法或降置信度，不调数字贴基线」的原则，本次**未改动画像任何数值**。

## 窗口与口径偏差（读数前必读）

1. **窗口不对齐**：基线采集窗是 **2026-07-05 ~ 08-07**（首波为主，含发行日 2026-07-20 前后），画像窗是 **2026-07-20 ~ 10-19** 的 `full_lifecycle`。票把窗口写死为 `full_lifecycle`，所以本回测是“按 full_lifecycle 口径对首波基线复算”，**不是同窗口复现**。SOP v1.1 §1 明确要求两窗口分开、基线按首波采集时只用首波那套对比，否则会系统性低估男性/31–40、高估女性/50+。
2. **偏差方向与 SOP v1.1 的预测一致**：31–40 低估 **12.02pp**（最大单档误差）；male 低估 4.40pp（female 高估 4.40pp）；50+ 高估 4.16pp；18-23 高估 3.18pp。整体上重建画像比基线**更平**——把 31–40 主峰的份额摊到 18–23 与 41–50/50+ 两侧。这正是窗口错位的已知方向，**不应把它当作方法误差来调参**，而应作为“首波基线 vs 全周期画像不可直接等同”的证据。
3. **基线本身是代理窗口**：388/628 条视频详情、按视频加权的“使用该 BGM 的高表现视频观众”，不是平台人口报表，也不是纯听众；引用时保留这句口径。
4. **不可核维度**：基线只有省份、没有城市层级字段；画像没有省份数值，只有一条 4_inferred 的城市层级推断 → **省份/城市层级不可核，不参与回测**，也不引用。

## 第二首：blocked 记录

- 状态：**`blocked: 无离线输入`**
- **不产出 PASS/FAIL**：不知道就不编——仓库内没有第二首歌曲的离线飞瓜基线，也没有第二首的 v2 契约调研画像；产出一份画像必须按 SOP 做公开信息调研（联网/人工），被本票「不联网、不跑飞瓜 CLI」的约束挡住。
- **未伪造基线**：未新增任何基线数值、未拿其他歌的画像或榜单证据顶替。
- 因此按票的 fallback，把验收改为对山风 `full_lifecycle` 画像做下面的方法自洽检查。若后续 Lead 提供第二首离线画像/基线，则按同一通过线复算，并删掉本节的 blocked 状态。

## 自洽检查（山风 `full_lifecycle`）

判定：**PASS**（由 `scripts/audience_backtest.py::self_consistency` 复算，测试锁定）。

### 1. 契约字段齐全

| 对象 | 检查 | 结果 |
|---|---|---|
| profile | `target_platform` / `time_window` / `method_id` / `method_version` / `status` / `researched_at` 必填，`coverage_note` 非空 | 全齐 |
| observations | 8 条，`dimension` / `bucket` / `evidence_level` 齐全 | 全齐 |
| sources | 1 条，`source_platform` / `source_key` / `source_type` / `fetched_at` / `evidence_level` 齐全 | 全齐 |
| entries | 2 条，`entry_type` 齐全；未知 `performer`/`released_at` 由导入器写空字符串 | 全齐（导入往返测试已验） |
| 契约约束 | 年龄五档/性别两档合计 1.00，`value_low ≤ value_mid ≤ value_high` 且均在 [0,1] | 通过 |

### 2. 八条反偏见逐条作答（SOP L3，记录层面）

| # | 问题 | 作答 | 记录依据 |
|---|---|---|---|
| 1 | 把出镜者/创作者性别当听众性别？ | 否 | observation 无 creator/出镜者维度；gender 为 4_inferred |
| 2 | 用单场演出/单个地方账号/歌手籍贯推省份？ | 否 | 记录无 province 维度；唯一地域相关是 4_inferred 的 city_tier |
| 3 | 把内容形态直接映射年龄？ | 无法判断 | 记录不含内容模板/场景维度，无法从单条记录复核原始推断链 |
| 4 | 把后续翻红波当首波？ | 否 | 窗口显式声明为 full_lifecycle（2026-07-20 ~ 10-19），未冒充首波 |
| 5 | 串用平台口径？ | **是（已声明）** | 方法用酷狗 ownercount 版本权重、target_platform=douyin；缓释 = `is_proxy=1` + 观测全 4_inferred + coverage_note 明示「无抖音受众报表/平台口径不同」；回测只比方向与距离，不比平台数值 |
| 6 | 把翻唱/改名版受众直接算进原版？ | 否 | entries 区分 original/cover（cover 的 `relation_to_original=cover_remix`），版本矩阵按入口加权后再合成总体 |
| 7 | 引语/评论不是真实抓取？ | 无法判断 | 记录不含评论文本/引语字段；本报告不引用任何引语 |
| 8 | 数字指不到来源？ | 否 | 每条带数值 observation 均有 `source_id`（见下） |

说明：第 5 条如实答「是」，因为方法本身就是跨平台口径的推断；自洽检查的标准是“逐条作答且声明缓释”，不是“全部答否”。第 3、7 条答「无法判断」而不是硬判「否」——记录层面复核不了原始调研过程，不冒充已核。

### 3. 每条数值有 `source_id`

- 8/8 条带数值 observation 都有 `source_id`，全部解析到已声明 source `src_shanfeng_kugou_search`（酷狗同名检索，`1_platform_public`）。
- 无孤立数值：不存在“有 `value_*` 但 `source_id` 为空或指向不存在 source”的 observation。
- 2 条 entry 中，`original` 带 `source_id`；`cover` 是 fixture README 声明的**合成行**（只为钉住“未知 performer/released_at 存空字符串”规则），本身不带数值与 source，不构成“数字无来源”。

### 4. 无平台占比事实

- 全部 observation 证据等级为 **`4_inferred`**（没有 `1_platform_public` / `2_media` / `3_proxy` 的“事实级/代理级”占比），profile `is_proxy=1`。
- `coverage_note` 明写「平台口径不同；无抖音受众报表」→ 对外只能表述为“推断区间”，不是平台报表/投放百分比。
- 唯一可能被误读的数字是城市层级 0.60–0.73（mid 0.66）：它是 4_inferred，且基线根本没有城市层级字段，本回测**未做数值对比**、报告未引用为事实。
- 结论：记录中不存在“平台占比事实”表述。

### 5. 榜单证据导入不改变调研画像数值（回归锁定）

- 在同一临时库里先导入山风 `full_lifecycle` research profile，再导入 `chart_snapshot` 榜单证据；导入前后 8 条 research observation 的 `dimension` / `bucket` / `value_low` / `value_mid` / `value_high` / `evidence_level` / `source_id` 逐项相同，profile 行逐字段相同。
- 导入榜单证据后从库内 observation 复算仍为 **L1 24.05pp / 31–50 8.36pp / 性别方向 correct / PASS**，不存在“榜单证据污染回测数值”。
- 榜单证据分组自身不产生覆盖缺口：命中数写在 `basis_note`（如「单期快照命中 N 榜」），`coverage_note` 只留给真正缺口；该断言由 `tests/test_audience_backtest.py::test_chart_evidence_import_does_not_change_research_profile_values` 与 `tests/test_audience_tags.py` 的投影回归共同锁定。

## 不能支持什么

- **不能支撑投放级百分比或平台受众报表**：全部年龄/性别/城市层级数字是 4_inferred 推断区间，不是抖音后台数据。
- **不能证明方法泛化**：n=1；山风擦线 PASS（L1 余量 0.95pp），不构成第二首或全库的方法验收。
- **不能证明 `full_lifecycle` 与首波基线等价**：窗口错位是已知缺口，偏差方向与 SOP v1.1 预测一致；不能拿本次 24.05/8.36 当作同窗口精度。
- **不能支撑省份/城市层级结论**：两维均不可核，不引用。
- **不能验证分版本画像**：fixture 只保留 1 条总体 profile，不含 SOP L2 的分版本画像表；方法“先分版本再合总体”的关键一步未被本次回测覆盖。
- **不能校准置信度**：profile `confidence=low` 未经任何置信度回测，“low 是否恰当”无证据。
- **不能用于榜单证据数值回测**：榜单证据不产出受众占比，本报告不包含 `chart_snapshot` 的受众数值；只做了「导入榜单证据后调研画像数值不变」的隔离回归（§5）。
- **不能替代 #137 正文的验收改写**：本报告只作为 #142 的 PR Evidence；不改写/不回贴 #137。

## 交付物与测试证据

| 交付物 | 路径 |
|---|---|
| 回测脚本 | [`plugins/music-kb/scripts/audience_backtest.py`](../../plugins/music-kb/scripts/audience_backtest.py) |
| 离线测试 | [`plugins/music-kb/tests/test_audience_backtest.py`](../../plugins/music-kb/tests/test_audience_backtest.py) |
| 本报告 | [`docs/reviews/audience-backtest-2026-10-20.md`](audience-backtest-2026-10-20.md) |

离线测试覆盖：票内预期值断言（24.05 / 8.36 / correct）、L1 不除 2 的公式性质、31–50 两档相加、性别方向翻转/并列、第二首 blocked 且无 verdict、自洽检查通过 + 4 类破坏样本各自失败、fixture 经 `import-audience` 导入临时 SQLite 后从库内 observation 复算同值、**导入 `chart_snapshot` 榜单证据后 research observation/profile 逐字段不变且复算仍 PASS**（无网络）。全量测试命令：`cd plugins/music-kb && uv run --offline pytest -q`。
