# Music Analysis KB

酷狗周更流水线与 music-kb 检索插件的知识库：抓榜 → 下载 → 本机分析 → 导入 → 不可变快照分发。

## Language

**MOSS-Music**:
OpenMOSS 的 8B 音乐理解模型，经 MLX 8-bit 量化在本机 Apple Silicon 运行；当前的分析主通道（整曲整体分析，不分段）。
_Avoid_: MOSS（单用时含糊）、moss runner（那是 CNB 镜像名，跑的却是 Music Flamingo）

**Music Flamingo**:
NVIDIA 的音乐理解模型，曾是 CNB GPU 分析通道的模型；CNB 收费后运行时退役，代码仅作历史参考。
_Avoid_: MF（文档中首次出现请写全称）

**Canonical delivery**:
每行一首歌分析结果的 LF JSONL 交付文件，16 个必需字段（含 `source_sha256`/`output_text_sha256` 双哈希），是分析入库（`knowledge_import`）的唯一输入格式；`output_text` 为自由文本，格式校验不解析其内容。
_Avoid_: delivery JSON、分析结果文件（泛称）

**CNB**:
云端 GPU 平台，曾经的 campaign 分析运行时；2026-09 起收费，不再是回退链路，代码与收据仅作历史参考。
_Avoid_: 回退通道、备用算力
