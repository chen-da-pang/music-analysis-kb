# Music KB v0.7.5 真实对话验收

## 验收结论

`v0.7.5` 已经修复本轮最核心的行为问题：真实模型保留了三个有证据的方向，分别检索并分组
输出，没有再次退化成“两方向 + 平铺”。conversation-behavior validator 为 5/5、100%。

但端到端验收暂不判定为可转 Ready。单次首轮请求耗时约 163 秒，累计输入 390,859
tokens，其中 350,976 为 cached input。加入真实用量后，Plugin Eval 核心评分从静态 A / 95
变为 C / 77、high risk；主要失败项是 observed usage 与静态预算相差过大。答案形态通过，
运行重量没有通过用户“不想把检索做得太重”的 UX 要求。

## 环境与场景

- 使用临时 `CODEX_HOME`，只安装并启用 `music-kb 0.7.5`；没有旧对话、其他已安装插件或
  仓库任务指令参与回答。
- 模型：`gpt-5.6-terra`，reasoning effort `max`。
- 用户原句：`我需要一些 R&B、温暖的、关于爱情的歌`。
- 只读快照：`music-kb-2026w29-kugou-431-final`，1,348 条 recording / canonical analysis，
  `search_projection_state=current`。
- 第一次最小环境预检因没有迁移本机 custom provider 而在采样前 401 退出；没有进入模型或
  Music KB，不计入本次用量。补入同一 provider 后只运行了一次完整真实场景。
- Codex CLI 对该自定义模型使用 fallback metadata；这可能影响模型元数据优化，但不改变
  实际 Skill 注入、检索结果、最终回答和 Responses usage 记录。

## 真实行为

基础检索的 50 条 bounded rows 给出以下关键共现证据：

- `r&b 50`
- `warm 50`
- `love 50`
- `melancholic 45`
- `romantic 30`
- `soul 7`

模型随后明确说“基础结果同时出现浪漫、忧郁和少量 Soul”，并输出三个独立组：

1. **温暖浪漫**：对应 `r&b + warm + love + romantic`，33 条有效结果，展示前 4 条。
2. **温暖微伤**：对应 `r&b + warm + love + melancholic`，47 条有效结果，展示前 4 条。
3. **Soul/R&B 质感**：对应 `soul + warm + love`，7 条有效结果，展示前 4 条。

三个最终组的歌曲和顺序与三次独立 v0.7.5 bounded search 的前四条完全一致。模型同时做到：

- 第一组说明了为什么是当前最可能理解；
- 重叠歌曲没有静默去重，并在可见位置说明也符合另一方向；
- 每首歌显示序号、匹配证据和真实试听链接；
- 给出“再来一些 / 换一批”的白话指引；
- 结尾询问要查看哪些歌曲的完整描述，并接受序号、歌名、“前几首”或“全部”。

一个非阻塞的自然度观察是：三个方向仍同时可见时，指引使用了“保持这个方向”，但没有
紧接着让用户明确选择“温暖浪漫 / 温暖微伤 / Soul/R&B”中的哪一个。用户仍可从组名理解，
但“再来一些”单独出现时的当前方向可能不够明确；该点不影响本次 5 项行为指标通过。

## Plugin Eval 结果

规范化轨迹见
[真实对话 trace](evidence/2026-07-22-music-kb-live-conversation-trace.json)。为控制日志体量，
Codex 事件流只保留了方向声明、最终回答和 usage，未保存逐字节 shell command；trace 中的
三个查询参数由最终每组结果与 v0.7.5 确定性重放逐条核对，因此是规范化证据，不冒充原始
MCP transcript。

| 层 | 结果 |
| --- | --- |
| conversation contract | 13/13，100%，0 failed checks |
| runtime behavior | 5/5，100%，0 failed checks |
| Plugin Eval 静态 Skill | A / 95，invoke estimate 4,414 tokens |
| Plugin Eval + observed usage | C / 77，high risk |
| observed usage 样本数 | 1；因此只能判定当前场景，不建立稳定总体基线 |

五项 runtime behavior 全部通过：

1. trace shape；
2. base facets 与 `returned_results` scope；
3. 三个重要方向完整保留；
4. 三个方向使用不同参数独立检索；
5. 三个有效方向分别成组，没有平铺。

## 真实用量与重量来源

用量原始记录见
[observed usage](evidence/2026-07-22-music-kb-live-observed-usage.jsonl)。

| 指标 | 结果 |
| --- | ---: |
| 完成时间 | 约 163 秒 |
| input tokens | 390,859 |
| cached input tokens | 350,976 |
| 非 cached input tokens | 39,883 |
| output tokens | 4,472 |
| reasoning tokens | 753 |
| static active estimate | 4,478 |
| observed / static input drift | 86.28 倍 |

390,859 是 Codex 多轮采样累计处理的输入量，其中大部分是 cached input，不能直接等同于
390,859 个全价计费 token；但它仍真实反映了长等待、上下文反复携带和宿主处理压力。

当前重量主要不是新增 facets 的数据库计算。先前同进程测量已经确认 facets 只增加约 1.13 ms。
真正的放大链路是：基础查询和三个分支都使用 `limit=50`，每条结果又包含完整 tags、
`source_links` 等字段；多轮模型调用继续携带前面的大结果，因而产生大量 cached input。
4,414-token Skill 本身偏重，但不足以单独解释 390,859-token 的端到端输入。

## 交付判断与下一项产品决策

- 保留 Draft PR #46，不转 Ready、不合并。
- 本次只记录验收事实，不修改 Skill、MCP、Schema、数据库或周更流程。
- 下一步应先讨论一种“扫描范围大、返回载荷小”的检索形态：facets 仍可基于最多 50 条
  bounded rows 计算，但给模型的候选只返回少量必要字段和代表性记录。这样不会因为缩小
  可见候选而错过方向，同时避免四次完整 50-row payload 进入上下文。
- “降低所有查询 limit”可以减少重量，但会削弱方向证据覆盖，不应在没有比较前直接采用。

