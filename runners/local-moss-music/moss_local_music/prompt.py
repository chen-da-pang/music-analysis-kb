"""Constrained five-dimension analysis prompt for local MOSS-Music runs."""

from __future__ import annotations

OFFICIAL_FIVE_DIM_PROMPT = (
    "请从风格与速度、调性与和声、乐器编配、结构安排以及整体情绪几个方面描述这段音乐。"
)

# Constraints the deterministic tag parser downstream depends on: `###`
# section headings feed section tags; `NN bpm` and `X major/minor` feed the
# bpm/key regexes; the identity rule keeps model title/artist hallucinations
# out of the analysis text (delivery title/artist come from chart metadata).
PROMPT_CONSTRAINTS = """
输出格式约束（严格遵守）：
1. 用 Markdown 三级标题分节，每节以 `### ` 开头、以英文冒号结尾（例如：`### 风格与流派:`、`### 速度:`、`### 调性与和声:`、`### 乐器编配:`、`### 结构安排:`、`### 人声:`、`### 歌词主题:`、`### 整体情绪:`）。标题后必须紧跟英文冒号，不要使用中文冒号，不要省略冒号，不要使用"字段: 值"的紧凑列表格式，不要使用代码块。
2. `### 速度` 小节必须包含形如 "112 bpm" 的表达（阿拉伯数字 + 空格 + 小写 bpm，例如：这首歌曲的速度约为 112 bpm）。不要写成"每分钟 112 拍"或"BPM: 112"。无法可靠判断时不要编造数字。
3. `### 调性与和声` 小节必须给出具体主调性，先写"音名 + major/minor"的英文形式，可再附中文说明（例如：主调为 E major（E 大调））。不要只写"E 大调"而不给英文形式；实在无法判断才写"调性不明确"。
4. 不要猜测或输出歌曲名称与歌手名。
5. 歌词引用只能出现在 `### 歌词主题` 小节内；`### 人声` 小节只描述演唱方式（声部、语言、唱法技巧），不要引用歌词原文。
"""


def build_prompt() -> str:
    return f"{OFFICIAL_FIVE_DIM_PROMPT}\n{PROMPT_CONSTRAINTS.strip()}\n"
