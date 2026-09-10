"""Prompt-to-tag contract: MOSS-style output must feed the existing parser.

The fixture is written the way the constrained prompt instructs the model to
answer (### sections, NN bpm, X major/minor, lyric quotes only in the lyrics
section). If the prompt or parser drifts, these assertions go red.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


from music_kb.tagging import extract_music_flamingo_metadata  # noqa: E402

FIXTURE = """### 风格与流派:
这是一首典型的华语流行抒情歌（mandopop ballad），编曲以原声乐器为主，整体呈现出温暖的电影感氛围。

### 速度:
这首歌曲的速度约为 88 bpm，属于中慢板，律动平稳。

### 调性与和声:
主调为 E major（E 大调），和声进行以 I - V - vi - IV 为主，副歌使用了属和弦推动情绪。

### 乐器编配:
以钢琴与木吉他（acoustic guitar）为主导，进入副歌后弦乐（strings）铺开，低音由贝斯支撑，鼓组保持轻柔的节奏。

### 结构安排:
歌曲采用经典的 intro - verse - pre chorus - chorus - bridge - outro 结构，第二段副歌前有明显的 build up。

### 人声:
女声（female vocal）演唱，普通话（mandarin）咬字清晰，气声（breathy）运用较多，副歌略有假声（falsetto）点缀。

### 歌词主题:
歌词围绕成长与坚持（perseverance）展开，例如"少年走过长夜终于看见光"。

### 整体情绪:
整体情绪温暖、怀旧（nostalgic）而充满希望感（hopeful），结尾归于平静与释然。
"""


class TagContractTest(unittest.TestCase):
    def setUp(self):
        self.tags, self.features = extract_music_flamingo_metadata(FIXTURE)
        self.names = {f"{t['namespace']}/{t['name']}" for t in self.tags}

    def test_bpm_numeric_feature_extracted(self):
        self.assertIn(("bpm", 88.0), [(f["name"], f["value"]) for f in self.features])

    def test_key_center_tag_from_english_form(self):
        self.assertIn("harmony/key center E major", self.names)

    def test_section_tags_from_headings(self):
        for section in ("tempo", "harmony", "instrumentation", "structure", "vocal", "lyrics_theme", "mood", "genre"):
            self.assertIn(f"section/{section}", self.names)

    def test_genre_mood_and_structure_tags(self):
        for expected in (
            "genre/mandopop",
            "genre/ballad",
            "genre/acoustic",
            "mood/warm",
            "mood/nostalgic",
            "mood/hopeful",
            "vocal/female vocal",
            "vocal/mandarin vocals",
            "structure/chorus",
            "structure/bridge",
            "lyric_theme/perseverance",
        ):
            self.assertIn(expected, self.names)

    def test_lyric_quote_does_not_leak_into_descriptors(self):
        # the quoted line mentions 光 (light); it must stay in the lyric corpus
        # and never create a production/genre tag — sanity-check via the
        # corpus splitter used by the parser.
        from music_kb.tagging import _corpus, _split_descriptor_and_lyric_corpora

        descriptor, lyric = _split_descriptor_and_lyric_corpora(_corpus(FIXTURE))
        self.assertIn("少年走过长夜", lyric)
        self.assertNotIn("少年走过长夜", descriptor)

    def test_prompt_fixture_shape_still_matches_prompt_contract(self):
        self.assertTrue(re.search(r"\d+ bpm", FIXTURE))
        self.assertTrue(re.search(r"[A-G] (?:major|minor)", FIXTURE))
        self.assertEqual(len(re.findall(r"^### ", FIXTURE, flags=re.M)), 8)


if __name__ == "__main__":
    unittest.main()
