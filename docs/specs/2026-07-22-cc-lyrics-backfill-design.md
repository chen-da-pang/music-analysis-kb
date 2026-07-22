# #52：复用 CC 下载原子补齐并分发完整歌词

状态：设计已获产品确认；实现前等待本文档审阅。

## 目标

让 `music-kb` 的每一首 canonical recording 都具备可验证的歌词最终状态。用户先检索并选中歌曲；随后：

- 要“完整内容”时，得到现有完整音乐分析和该录音版本的完整歌词；
- 只要“歌词”时，只得到完整歌词；
- 首轮候选检索仍保持紧凑，不携带歌词正文。

歌词在私有、本地、只读 SQLite 快照中保存为 UTF-8 普通按行文本。`.lrc` 只是 CC 下载过程中产生的临时副产物，不是插件的读取源，也不向用户返回时间戳或 LRC 元数据。

## 已确认的边界

- 存量 1,348 首 canonical recordings 必须回填；新歌必须在后续周流程中同步补齐。
- 每首歌在发布时只能处于以下一种终态：
  - `available`：有非空、完整的歌词文本；
  - `instrumental`：精确来源明确证明是纯音乐；
  - `platform_unavailable`：按精确平台歌曲 identity 查询后，平台明确无歌词。
- 空结果、网络失败、解析失败、identity 不一致或标题/歌手模糊匹配均不是例外；它们保持未解决并阻断快照、对等端发布和音频清理。
- 不生成歌词、不用 ASR 替代、不用别的版本歌词顶替，也不为补歌词而重下已有音频。
- 生产歌词、音频、生产 SQLite 和抓取凭据都不提交到 Git；测试仅使用合成文本。

## 方案选择

采用“扩展现有 CC 下载原子”的方案。

现有 `claude_download` 调用 `musicdl` 的 `KugouMusicClient`。该客户端在获得歌曲信息时已经请求歌词并放在 `SongInfo.lyric`，底层下载会将其写为同名 `.lrc`。因此不新建第二个歌词爬虫，也不靠扫描遗留 LRC 文件。

未采用的方案：

1. 只扫描现有 `.lrc`：覆盖不足，且目录清理会删除它们，无法证明绑定关系。
2. 只在用户请求时临时抓取：无法满足整库覆盖和离线只读快照的要求。
3. 新建独立歌词下载器：与 CC 原子已有来源重复，增加身份漂移和维护成本。

## 数据模型与迁移

将 schema 从 v6 升级到 v7，并新增 `recording_lyric`。一条歌词记录只对应一条 canonical recording：

| 字段 | 含义 |
| --- | --- |
| `recording_id` | 主键，外键指向 `recording.id`。 |
| `source_track_row_id` | 外键指向用于取得歌词的 `source_track.id`；导入时必须校验它属于同一 `recording_id`。 |
| `status` | `pending`、`available`、`instrumental`、`platform_unavailable`。只有后三种可进入发布快照。 |
| `lyric_text` | 仅 `available` 时存在的普通按行歌词文本。 |
| `text_sha256` | 规范化歌词文本的 SHA-256；仅 `available` 时存在。 |
| `evidence_json` | 精确来源 identity、查询方式、响应类别、异常/纯音乐证明与原始响应摘要哈希。 |
| `normalizer_version` | 清洗规则版本，便于以后审计。 |
| `acquired_at` / `updated_at` | 取得与更新的 UTC 时间。 |

约束如下：

- `available` 必须有非空 `lyric_text` 和 `text_sha256`；其他终态不得伪造文本。
- `instrumental` 与 `platform_unavailable` 必须有非空、可审计的 `evidence_json`。
- `pending` 只允许留在发布端 master；快照验证发现任何 `pending` 或缺行即失败。
- 歌词不进入 `search_fts`，不提供跨库全文歌词搜索。

迁移会保留既有 `recording`、`source_track`、分析和标签数据，并保持 snapshot 不可写约束。

## CC 原子：新歌同步采集

`claude_download` 的固定 worker 保留现有音频下载职责，并增加歌词回执：

1. 取得严格匹配的 `SongInfo` 后，读取返回对象的 `lyric`，而非依赖日后扫描文件名。
2. 将 LRC 的时间标签和元数据行去除，保留原顺序的可读文本行，统一换行和 UTF-8；不翻译、不摘要、不补写。
3. 记录一个结构化 lyric receipt：队列 identity、返回的 source/identifier、歌词状态、规范化文本或明确无歌词/纯音乐证据、文本哈希和错误原因。
4. 只有返回 source identity 与待绑定的 `source_track` 一致时，回执才可导入；标题/歌手匹配只能帮助下载器找到候选，不能成为歌词身份的最终证据。
5. 读取或解析失败写入可重试回执，不能写成 `platform_unavailable`。

现有 `.lrc` 可以继续作为下载器副产物，但 SQLite 中的 `recording_lyric.lyric_text` 才是唯一交付源。即使之后删除音频目录，歌词仍可读取。

## 存量回填：同一 CC 来源、lyrics-only 模式

新增一个受现有 CC 编排管理的 `lyrics_backfill` 模式，不下载音频。

1. 从 master 的 `source_track` 生成队列，每项携带 canonical `recording_id`、`source_track.id`、`source_name`、精确 `source_track_id` 和 `source_url`。
2. 固定 worker 使用同一个 `KugouMusicClient` 来源，按精确 identity 取得歌词回执；不得将同名搜索结果直接当作成功。
3. 将 `available`、有证据的 `instrumental` 和有证据的 `platform_unavailable` 幂等写入 master；其余项目保持 `pending` 并可只重试未解决项。
4. 不读取、移动或重新下载既有音频。历史 `.lrc` 仅可作为人工排障证据，不能绕过 identity 校验。

这样，未来新歌和 1,348 首历史歌共享同一歌词来源、同一清洗规则和同一回执格式。

## 周流程与发布门槛

当前关键顺序为 `claude_download -> knowledge_import -> snapshot -> audio_cleanup`。改为：

```text
claude_download
  -> 分析/CNB 交付
  -> knowledge_import
  -> lyrics_import（导入本次 CC 歌词回执）
  -> lyrics_backfill（仅处理历史或本次未解决项）
  -> lyrics_coverage（硬门槛）
  -> snapshot / 本地安装 / 对等端发布
  -> audio_cleanup
```

`lyrics_coverage` 计算并报告：

```text
recordings = available + instrumental + platform_unavailable + unresolved
```

其中 `unresolved` 包含缺记录、`pending`、身份不一致和任何失败回执。只有 `unresolved == 0` 且三种终态之和等于 canonical recordings 时，才允许创建或安装快照、发布给对等端以及清理音频。

`snapshot` manifest 和 `music_kb_status` 将新增上述状态计数。snapshot 验证会复算计数和表约束，而不是仅信任 manifest 数字。

## 插件读取合约

新增只读 MCP 工具 `music_kb_get_lyrics(recording_id)`：

- 返回精确 recording 的状态、来源 identity 与全文 `lyric_text`；
- `available` 返回完整文本，应用层不按字符数截断；
- 两种例外返回状态和可读说明，不以模型生成、摘要或标签代替歌词；
- unknown recording 返回现有风格的 not-found 错误。

保留 `music_kb_get_canonical_analysis(recording_id)`。更新 retrieval Skill：

- 用户已选歌并说“完整内容”时，依次读取完整分析和完整歌词，再组合给出；
- 用户只要歌词时，只调用歌词工具；
- 首轮 `music_kb_search`、title/artist resolve 和 tag facets 均不带歌词正文。

## 错误处理与重试

- `platform_unavailable` 只能由精确 identity 的明确“无歌词”响应写入；空字符串、超时和 HTTP/解析异常均为 `pending`。
- `instrumental` 只能来自同一精确来源的明确标记或可审计的来源证据；不能从“歌词为空”推断。
- source identity、recording 或版本无法对齐时，拒绝导入回执并列入 unresolved。
- 回填可安全重跑；相同文本哈希和来源不重复写入，改善后的有效回执可替换 `pending`。
- 音频下载失败和歌词失败分别记录；补歌词重试绝不触发音频重下。

## 验证计划

新增合成 fixture 与测试，至少覆盖：

1. v6 master 迁移至 v7，snapshot 保持只读且含歌词表。
2. `available` 的 LRC 清洗、普通行文本保存、哈希和按 recording 读取。
3. `instrumental`、`platform_unavailable` 的证据约束。
4. 空结果、网络失败、解析失败、identity 错配和错误版本均阻断 coverage/snapshot/publish/cleanup。
5. 1,348 类的全量计数校验，以及只重试 unresolved 的幂等行为。
6. 音频目录清理后仍从 SQLite 成功读取歌词。
7. MCP/Skill：首轮候选无全文；lyrics-only 与 full-content 只返回各自应有内容。
8. manifest、status 与快照验证对歌词状态计数一致。

## 实施范围

预计涉及：schema/migration、repository validation/status/read path、CC 固定 worker 与 `run_claude_download` 编排、lyrics importer/coverage、weekly orchestration、snapshot verification、MCP、retrieval Skill 和对应测试。不会修改歌曲检索排序、Music Flamingo 分析逻辑或公共 Git 内容边界。
