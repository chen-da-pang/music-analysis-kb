# 酷狗受众榜弱信号审计（2026-10-20 口径）

- 日期：2026-10-20（初稿随 #141 实现提交）
- 用途：#141 Acceptance「审计报告（作为 PR Evidence）」；回答「这条榜单弱信号值不值得常驻、数字怎么读、哪里会误用」。
- 结论先说：**榜单只能证明「这首歌进过某个人群/场景/圈层池」，给不出任何受众占比。** 26 榜实验实测覆盖库内 18% 的歌；v1 只抓 24 个受众指向榜（去掉 2 个热度榜），离线复算覆盖 16.1%；引用任何一个数字都必须带上「榜数/口径/库快照日期」。

## 1. 数据来源与两个口径

| 项目 | 26 榜实验（原始证据） | v1 实现（本仓库） |
|---|---|---|
| 榜单 | 26 个酷狗榜：3 代际 + 7 场景 + 14 圈层 + 2 热度（TOP500 8888、飙升 6666） | 24 个受众指向榜：3 代际 + 7 场景 + 14 圈层；不抓 8888/6666，不产出热度维度 |
| 抓取 | 仓库外一次性脚本 `curl` 抓 `m.kugou.com/rank/info`，产物落 `research/audience-137/runs/2026-10-20/kugou-charts-2026-10-20.json`（不入 git） | `plugins/music-kb/scripts/capture_kugou_audience_charts.py` 复用 `capture_kugou_chart.py`（kugou-cli），产物落 `run_dir/audience-charts/`（不入 git、不写下载队列） |
| 榜单策 | 只在实验脚本与报告里 | 版本化 `plugins/music-kb/references/kugou-audience-charts-v1.json`：24 榜 `rank_id/chart_name/family/update_frequency/official_intro`，官方简介逐字取自 #141 正文；`official_intro` 不得改写/联网刷新/编造 |
| 匹配 | `match_and_label.py`：规范化标题+歌手，exact-title → base-title，filename 兜底；歌手不匹配=歧义 | 同规则迁入 `plugins/music-kb/src/music_kb/audience_charts.py`，并**优先 MixSongID**（`source_track.source_name='kugou' AND source_track_id='kugou-'||platform_track_key`），再 exact-title+artist，再 base-title+artist；歧义不自动打标、写复核队列 |
| 库内分母 | 同日 master `recording` canonical = **2005** 首 | 每周变化；报表必须写当期 canonical 数，不能复用 2005 |
| 产物 | 实验报告 + 弱标签 JSON/CSV | `audience_source/profile/observation` 四表（schema v9）；原始榜单 JSON 只留发布端 |

引用规则（写死）：任何覆盖率/命中数后面必须注明「26 榜实验」或「v1 24 榜复算」、库快照日期与分母；不得把实验数字当成 v1 生产数字，也不得跨期比较。

## 2. 26 榜实验数字（原始口径）

来源：`research/audience-137/runs/2026-10-20/matched-report-2026-10-20.json`、`run-report-2026-10-20.md`；抓取时间 2026-10-20T10:40:21+08:00；库内 canonical 2005 首。

| 指标 | 数值 | 口径 |
|---|---:|---|
| 榜数 / 榜内条目 | 26 / 2403 | 代际 300、场景 633、圈层 1250、热度 220；热度榜 TOP500 只抓到前 120 条（容量 500） |
| 命中 recording | **361 / 2005 = 18.0%** | 至少 1 条榜内条目匹配到库内 canonical 歌；同一歌多榜只算 1 |
| 命中榜条目 | **524 / 2403 = 21.8%** | 按榜条目计；exact-title+artist 487、base-title+artist 37；同一歌多榜/多版本会重复计 |
| 歧义（歌名命中、歌手不匹配） | **148** | 不自动打标；人工复核对象 |
| 未匹配 | **1731** | 2403 − 524 − 148 |
| 多榜（≥2 个不同榜） | **117** | breadth；多榜只说明「同时进过几个池子」 |
| 有代际信号 | **126 / 2005 = 6.3%** | 00后 48、90后 34、80后 27、跨代际 17（00+90 9 首、80+90 8 首） |

榜族分布（26 榜，recording 数）：代际 126；场景 soundtrack 49 / channels 37 / shortvideo 28 / recognition 26 / variety 20 / dj 11；圈层 rap 19 / guofeng 18 / rock 16 / rnb 13 / folk 13 / electronic 13 / acg 10。

判读：18% 是**上限型**数字——只要一首歌出现在榜单里就计入，且 2 个热度榜贡献了部分命中与多榜交叉；它衡量的是「池子覆盖」，不是「受众信息完整度」。

## 3. v1 24 榜离线复算（本实现口径）

方法：取同一份 2026-10-20 落盘快照，只保留 24 个受众指向榜（2403 → **2183** 条；每榜条目数与实验一致），用本仓库 `match_chart_entries` 对同一份 2005 首 canonical 库跑「规范化标题+歌手（exact → base）」匹配。旧 curl 快照不含 `mix_song_id`，所以本次复算**只相当于兜底路径**；生产 v1 的 kugou-cli 抓取带 `mix_song_id`，会先用 MixSongID 精确匹配，命中数**预计不低于**本复算。

| 指标 | v1 24 榜复算 | 相对 26 榜 |
|---|---:|---:|
| 榜内条目 | 2183 | −220（去掉 2 个热度榜） |
| 命中榜条目 | 425（exact 393 + base 32，19.5%） | −99 |
| 命中 recording | **323 / 2005 = 16.1%** | −38 首（−1.9pp） |
| 歧义 | **134** | −14 |
| 未匹配 | **1624**（74.4%） | −107 |
| 多榜（≥2 榜） | **70** | −47（热度榜曾贡献大量「多榜交叉」） |
| 有代际信号 | **126 / 2005 = 6.3%** | 不变（热度榜不参与代际维度） |

结论：**v1 去掉 2 个热度榜后覆盖率从 18.0% 降到 16.1%，多榜交叉从 117 降到 70**；代际信号不受影响。引用「18% / 21.8% / 117 / 148 / 1731」时必须说明它们来自 **26 榜实验（含热度榜）**；引用「16.1% / 19.5% / 70 / 134 / 1624」时必须说明它们来自 **v1 24 榜离线复算（标题+歌手兜底路径，未用 MixSongID）**。

## 4. 质量与误用风险（写死口径）

1. **榜单命中 ≠ 受众画像。** 产物只能写「进过某代际/场景/圈层池」，`evidence_level=3_proxy`、`is_proxy=1`；不输出年龄/性别/城市层级，不输出任何占比。展示层必须带「榜单证据不代表受众占比」。
2. **分母是当期 canonical 库。** 覆盖率随周更入库变化；`2005` 是 2026-10-20 快照，只能在注明日期时引用。
3. **命中条目 vs 命中歌不是一回事。** 524 > 361 是因为同一首歌可命中多个榜、同一榜可命中多个版本；不要用条目数算「覆盖率」，也不要用歌数算「榜单匹配率」。
4. **歧义不是未匹配。** 歌名命中但歌手不匹配的条目进复核队列、不自动打标；148/134 都是「待人工判断」，不是「已命中」。
5. **多榜 ≠ 更可信受众。** v1 置信度只有单期规则（1 榜 low、2–3 榜 medium、≥4 榜或代际 2 榜+场景/圈层 high）；多期 persistence 阈值尚未设定（实验只有 2 日 top30 对比，最多算短期稳定度），不得用多期累积暗示「更准」。
6. **口径差异。** 旧快照无 `mix_song_id`，复算只走标题+歌手；实验脚本有 filename 兜底，本次迁入实现不使用 filename（kugou-cli 产物没有该字段）。因此 24 榜复算与 26 榜实验的差异里，既包含「去掉热度榜」，也包含匹配细节差异，只能当方向性对照。
7. **52144 重叠榜。** 短视频热歌榜（52144）同时属于原有发现榜；v1 允许 `capture: own` 重复抓取（幂等、约 1 页）或复用 `run_dir/charts/` 同 rank_id 产物，两者都只写一条 `chart:52144:<date>` 来源，不重复计榜。2 个热度榜（8888/6666）归原有 `chart_capture`，v1 不产出热度维度、`scene/ktv` 不产出。
8. **原始快照边界。** `run_dir/audience-charts/` 的原始 JSON 与实验快照都只留发布端本机：不进 git、不进客户端快照；DB 只存派生证据 + `source_key`/`local_path` 引用。

## 5. 实现落点

- 榜策/官方简介：`plugins/music-kb/references/kugou-audience-charts-v1.json`（24 行逐字复制 #141 正文表；52144 `capture: reuse`，其余 `own`）
- 抓取：`plugins/music-kb/scripts/capture_kugou_audience_charts.py` → `run_dir/audience-charts/`（复用 `capture_kugou_chart.py`，不写下载队列）
- 规范化/匹配/歧义/证据构造：`plugins/music-kb/src/music_kb/audience_charts.py`
- 周更接入：`weekly_orchestration.py` 的 `audience_chart_capture`（chart_capture 之后）与 `audience_import`（knowledge_import 之后、lyrics_import 之前）；`workflow.py` 的 weekly-update 同路径（打标后、validate 前）
- 注册表：`references/validated-operations.json` 两个 operation + 受众快照边界 rule
- 离线测试：`tests/test_audience_charts.py`（榜策逐字、匹配顺序、歧义队列、confidence、抓取 fixture、导入统计/幂等/缺输入/`--delivery`），以及 weekly-run/weekly-update/CLI 的接入断言

## 6. Manual gate（真实抓榜，合并后由 Lead 执行）

不做进自动化测试；合并后在发布机跑一次，确认 24 个 rank_id 全部可用：

```bash
cd plugins/music-kb
uv run python scripts/capture_kugou_audience_charts.py \
  --run-id audience-gate-2026w43 \
  --output-dir /tmp/music-kb-audience-gate/audience-charts \
  --operations-file references/validated-operations.json
# 断言：manifest.chart_count == 24、captured_charts + reused_charts == 24、total_source_records > 0、
#       每个 rank_id 至少一个 chart-<rank_id>-p1-songs.json，且 52144 允许 reused
```

再用主库副本做一次离线导入验证（不写生产库）：

```bash
cp ~/.music-kb/music-master.sqlite /tmp/music-kb-audience-gate/master.sqlite
cd /path/to/music-analysis-kb/plugins/music-kb
uv run python - <<'PY'
from music_kb.audience_charts import import_audience_inputs
result = import_audience_inputs(
    database="/tmp/music-kb-audience-gate/master.sqlite",
    chart_directory="/tmp/music-kb-audience-gate/audience-charts",
)
print(result["chart_evidence"]["charts"], result["chart_evidence"]["matched_recordings"],
      result["chart_evidence"]["ambiguous"], result["chart_evidence"]["unmatched"])
PY
```

期望：`chart_evidence.charts == 24`；`matched_recordings` 与当期库相关，量级参考 16% 左右；歧义条目出现在 `audience-review-queue-<date>.json`；不产生任何受众占比字段。

## 7. 复现（离线，不联网）

```bash
cd plugins/music-kb
uv run --offline pytest -q tests/test_audience_charts.py
```

26 榜实验数字来自仓库外一次性运行（`research/audience-137/scripts/{chart_snapshot.py,match_and_label.py}`），只在本报告引用，未提交、不成测试依赖。
