# Music Analysis KB

酷狗周更流水线与 music-kb 检索插件的知识库：抓榜 → 下载 → 本机分析 → 导入 → 不可变快照分发。

## Language

**MOSS-Music**:
OpenMOSS 的 8B 音乐理解模型，经 MLX 8-bit 量化在本机 Apple Silicon 运行；迁移中的分析主通道（整曲整体分析，不分段）。
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

**整体听感通道**:
MuQ-MuLan v3 整曲 512 维音频向量的独立检索通道，默认用于找整体听感近邻；向量因子为空字符串。
_Avoid_: 综合分、三通道融合分、相似度百分比

**节奏通道**:
MERIT rhythm 头的 128 维音频向量，只比较律动；不与整体听感或音色加权合成。
_Avoid_: 旋律通道、综合节奏分

**音色通道**:
MERIT timbre 头的 128 维音频向量，只比较声音质感；独立于节奏通道。
_Avoid_: 旋律通道、融合听感分

**距离**:
余弦距离（1 − 余弦相似度），范围 0–2，越小越像，展示三位小数；不设相似阈值，不换算百分比。
_Avoid_: 相似度百分比、匹配率、相似阈值
