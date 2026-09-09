# 本机 MOSS-Music 取代 CNB Music Flamingo 成为分析运行时

## Status

accepted (2026-09-09)

## 决策

周更流水线的歌曲分析步骤从 CNB GPU 上的 Music Flamingo campaign 改为在本机
Apple Silicon 上用 MLX 运行 MOSS-Music-8B-Thinking（8-bit）。CNB 自 2026-09 起
收费，不再作为回退链路；其代码（`cnb_*` 原子、storage policy、收据契约）原地
保留仅作历史参考，且在编排入口加显式确认阀防止误触发计费。RunningHub 通道同
期废弃：其实例缺少 `MusicFlamingoAnalyzer` 节点，无法运行该模型，非等待可解。

## Considered Options

- **CNB 保留为备用**：被费用否决——收费后闲置回退路径不再是零成本。
- **RunningHub ComfyUI**：被节点缺失否决——平台装不上 `MusicFlamingoAnalyzer`。
- **本机 MLX MOSS-Music**（选定）：2026-08-31 已在真实歌曲上验证（七轮分析
  5 分 27 秒，官方采样参数，零死循环）；吞吐约 9-13 tok/s，单曲 24-94 秒，
  365 首/周的周更量约 6-12 小时串行，可过夜完成。

## Consequences

- 分析模型从 Music Flamingo 换成 MOSS-Music，输出文本风格不同；canonical
  delivery 的 `output_text` 是自由文本（仅 SHA256 校验），因此适配点是提示词
  的维度骨架（对齐 `extract_music_flamingo_metadata` 的双语标签规则，BPM 写
  `NN bpm`、调性写 `X major/minor`），不是交付格式本身。
- 歌词仍走 Kugou 官方源 identity binding（`source_name + source_track_id`），
  MOSS 的歌词转录只留在分析文本内，不参与歌词覆盖统计。
- 模型权重（`mlx-community/MOSS-Music-8B-Thinking-8bit`）留在 HuggingFace
  缓存，不入仓库；推理代码 vendor 进 `runners/local-moss-music/`。
- Thinking 模型在贪心解码（temp=0）下会死循环，采样参数固定为官方
  temp 1.0 / top_p 0.8 / top_k 50，`max_new_tokens` 上限 1500。
