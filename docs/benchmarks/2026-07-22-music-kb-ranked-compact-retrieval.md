# Music KB v0.8.0 代表性紧凑检索与真实对话验收

日期：2026-07-22

场景：`我需要一些 R&B、温暖的、关于爱情的歌`

快照：`music-kb-2026w29-kugou-431-final`，1,348 recordings / canonical analyses，
`search_projection_state=current`。

## 结论

v0.8.0 的当前实现已通过本场景的用户端行为验收：真实模型完成三个有证据的
方向、三次独立紧凑推荐和三组稳定呈现，每组都按后端默认页返回的 5 首原序
展示，不再按歌名删除、跨组去重或临场重排。跨组重复在两边都有用户可见标注，
15 个试听地址全部是 Markdown 链接。runtime behavior 为 16/16，100%。

检索层的重量问题已有实质改善：最终场景的 discovery 和三个分支载荷合计
15,544 bytes，比 v0.7.5 的 165,201 bytes 下降约 90.6%。与 v0.7.5 真实运行相比，
最终空工作目录运行的 input tokens 从 390,859 降至 146,215，下降约 62.6%。

最终端到端模型轮次为 63 秒，Plugin Eval 加入单个 observed-usage 样本后仍为
C / 81、high risk。这不能被静态 A / 100 掩盖；也不应被误读为检索载荷没有下降。
当前只能证明本场景的插件检索层已变轻，不能用一个 max-reasoning 宿主样本建立稳定基线。

## 为什么要改检索层

v0.7.5 使用最多 50 条完整搜索记录做方向发现和分支结果。它有两个问题：

1. 传给模型的中间记录包含完整 tags、summary、source links 等字段，四次载荷合计
   165,201 bytes，且在后续回合反复携带。
2. 旧搜索的顺序最终受 `updated_at DESC, recording_id` 影响，不能回答“为什么是这五首”。

v0.8.0 把一次用户请求拆成三个只读层次：

1. `music_kb_discover` 基于全部 canonical matches 计算 `match_count` 和 facets，不序列化歌曲。
2. `music_kb_recommend` 先严格命中所有条件，再按组内代表性稳定排序；只在与当前最优候选
   分数足够接近时，允许小范围提升有新次级标签侧重的歌。
3. `music_kb_get_canonical_analysis` 只在用户选定歌曲后按最多四首一批取得完整描述。

“一些”没有被 Skill 写成固定数量。当用户未提供数量时，Skill 不传 `limit`，由后端当前
校准参数返回默认 5 首；模型必须把这一页全部按原序展示。未来要改页大小时，只需调整
后端校准参数，不需要让模型先拿更大一页再二次裁剪。

## 两个真实行为失败及根因

### 失败一：方向不完整

第一次 v0.8.0 隔离运行的 discovery 已经明确返回 `hopeful=22`、`melancholic=47`、
`soul=7`，模型却只检索了前两个方向。小结果数不是删掉 Soul 的理由；它会实质改变
用户听到的质感。

修复是先建立完整方向 ledger，再开始分支调用。对这个已经用户确认的回归场景，当
`hopeful / melancholic / soul` 都为非零 facet 时，三个方向必须全部检索。这是场景守卫，
不是对其他请求的固定分类表。

discovery 还保留 namespace cutoff 处的同频并列标签，避免例如 `jazz=7` 恰好占据截断位时，
同样为 7 的 `soul` 因排序先后被意外隐藏。

### 失败二：后端返回 6 首，模型自行变成 5 首

同一次失败运行中，两个分支都显式请求 `limit=6`，最终每组只展示 5 首。模型按标题直觉
删掉《跳楼机》，又为跨组去重删掉《麦恩莉》。这实际上在后端排序之后又做了一次没有证据的
top-5 cutoff，同时违反了重叠歌曲应在每个真实匹配组中保留的决策。

修复不是增加更多“如何挑五首”的模型规则，而是取消第二次选歌：未指定数量时不传
`limit`，后端返回多少就展示多少；最终 preflight 机械比对每组 recording IDs 和原页顺序。

## 真实运行对比

| 阶段 | 行为结果 | 约用时 | input | cached input | output | reasoning |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| v0.7.5 最小环境 | 三方向通过，但 50-row 完整载荷过重 | 163 s | 390,859 | 350,976 | 4,472 | 753 |
| v0.8.0 第一次隔离运行 | 失败：漏 Soul，6 首被自行裁成 5 首 | 64 s | 152,225 | 127,872 | 1,697 | 187 |
| v0.8.0 仓库目录中间复测 | 行为通过，但存在源码探测、重读 Skill 和重复推荐 | 128 s | 307,454 | 273,920 | 2,855 | 512 |
| v0.8.0 空目录路由修复复测 | 路由与页面保真通过；重叠说明和链接格式未通过 | 65 s | 145,934 | 135,552 | 1,693 | 164 |
| v0.8.0 空目录最终复测 | 三方向、页面保真、重叠标注和链接格式全部通过 | 63 s | 146,215 | 123,776 | 1,681 | 149 |

最终运行使用临时 `CODEX_HOME`、只启用 Music KB，并在空工作目录中执行，模型看不到
仓库源码。它没有加载 deferred follow-up 文件，而是直接使用已知 CLI fallback：
`doctor -> discover -> 三个 recommend -> 回答`。

## 最终 16 项 runtime behavior

1. 完整、唯一事件 ID 的 schema v3 trace；
2. Skill 只读取一次；
3. 不探测 MCP resources；
4. 不扫描源码、README 或安装内部；
5. 已知成功场景不调用 `--help`；
6. 从 status 开始并只走直接 MCP / PATH 入口；
7. 不重复成功的 discovery / recommendation；
8. discovery 覆盖 all matches 且不返回歌曲 records；
9. 三个重要方向完整保留；
10. 每个方向有一次参数不同的独立 recommendation；
11. 每个分支返回紧凑字段、代表性排序和受控 payload；
12. 最终答案三组独立呈现；
13. 每组展示 IDs 与后端返回页完全一致，顺序不变；
14. 跨组重复有明确说明，试听地址全部为 Markdown 链接；
15. 同一回答告知“再来一些 / 换一批”，并提供完整描述入口；
16. 用户不可见 `recording_id` 和 `selection_basis` 内部字段。

custom metric pack 合计 32 个 checks（16 个静态契约 + 16 个真实行为）全部通过。

## Plugin Eval 证据边界

| 评估层 | 结果 |
| --- | --- |
| 静态 Skill | A / 100，low risk；`invoke=2,178`，`deferred=1,431` tokens |
| conversation contract | 100%，0 failed checks |
| runtime behavior | 16/16，100%，0 failed checks |
| 加入当前 observed usage | C / 81，high risk |
| observed / static active input | 64.22 倍；只有 1 个当前样本 |

Plugin Eval 把整个 Codex 宿主会话的 input 与 Skill 静态文本预算直接比较，因此仍稳定触发
`observed-usage-estimate-drift`。它是有用的端到端警报，但不能用来定量归因到某一个检索 payload。
本次同时保留后端 payload、真实 usage 和实际用时，避免任何一层替代另一层。

## 范围与交付状态

- 不修改 SQLite schema、快照内容、标签器、周更、CNB、peer、音频或提示词生成。
- 检索仍为本地、只读、retrieval-only。
- 当前功能验收通过不等于已建立稳定性能基线；后续应累积 5–10 个代表性样本，且分开记录
  cold-start 与 warm-cache。
- Draft PR #46 在本地全量验证、完整 diff review 和 GitHub 记录完成前保持 Draft，不在本报告中
  自动转 Ready 或合并。

## 证据

- [最终通过的规范化 trace](evidence/2026-07-22-music-kb-v080-live-passed-trace.json)
- [最终真实 observed usage](evidence/2026-07-22-music-kb-v080-live-passed-observed-usage.jsonl)
- [第一次 v0.8.0 失败 trace](evidence/2026-07-22-music-kb-v080-live-failed-trace.json)
- [第一次 v0.8.0 失败 observed usage](evidence/2026-07-22-music-kb-v080-live-failed-observed-usage.jsonl)
- [v0.7.5 真实对话验收](2026-07-22-music-kb-live-conversation-acceptance.md)
