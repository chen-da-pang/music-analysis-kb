"""Runner core tests: prompt, think/answer split, stubbed single-song run.

No test loads the real model; the generator is a stub at the model-call seam.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path


from moss_local_music import build_prompt, run_single, split_answer  # noqa: E402


class BuildPromptTest(unittest.TestCase):
    def test_prompt_contains_official_question_and_all_constraints(self):
        prompt = build_prompt()
        self.assertIn("风格与速度", prompt)
        self.assertIn("调性与和声", prompt)
        self.assertIn("乐器编配", prompt)
        self.assertIn("结构安排", prompt)
        self.assertIn("整体情绪", prompt)
        self.assertIn("###", prompt)
        self.assertIn("bpm", prompt)
        self.assertIn("major", prompt)
        self.assertIn("minor", prompt)
        self.assertIn("歌曲名称", prompt)
        self.assertIn("歌手", prompt)
        self.assertIn("歌词主题", prompt)


class SplitAnswerTest(unittest.TestCase):
    def test_with_think_block(self):
        answer, think = split_answer("<think>\ninternal reasoning\n</think>\n\nAnswer body.\n")
        self.assertEqual(answer, "Answer body.")
        self.assertEqual(think, "internal reasoning")

    def test_without_think_block(self):
        answer, think = split_answer("Just an answer.\n")
        self.assertEqual(answer, "Just an answer.")
        self.assertEqual(think, "")

    def test_unterminated_think_block_treated_as_answer(self):
        answer, think = split_answer("<think>\nunfinished")
        self.assertEqual(answer, "<think>\nunfinished")
        self.assertEqual(think, "")


RAW = (
    "<think>\nchain of thought\n</think>\n\n"
    "### 速度:\nThe tempo is about 88 bpm.\n\n"
    "### 调性与和声:\nE major throughout.\n"
)


class RunSingleTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake_generate(self, audio_path, model_path, prompt, *, temp, top_p, top_k, max_new_tokens):
        self.calls.append({"temp": temp, "top_p": top_p, "top_k": top_k, "max_new_tokens": max_new_tokens})
        return RAW, 42, 12.5

    def test_run_single_writes_sidecar_answer_and_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "song.mp3"
            audio.write_bytes(b"fake")
            sidecar = run_single(
                str(audio),
                Path(tmp) / "out",
                model_path="/models/moss-8bit",
                run_id="t2-test",
                generate_fn=self.fake_generate,
            )
            out = Path(tmp) / "out"
            answer = (out / "song.answer.txt").read_text(encoding="utf-8")
            self.assertNotIn("<think>", answer)
            self.assertIn("88 bpm", answer)
            raw = (out / "song.raw.md").read_text(encoding="utf-8")
            self.assertIn("<think>", raw)
            self.assertEqual(sidecar["generated_token_count"], 42)
            self.assertEqual(sidecar["elapsed_seconds"], 12.5)
            self.assertEqual(sidecar["thinking"], "chain of thought")
            self.assertEqual(sidecar["answer"], answer.strip())
            self.assertEqual(
                sidecar["prompt_sha256"],
                hashlib.sha256(build_prompt().encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                sidecar["generation_controls"],
                {"temp": 1.0, "top_p": 0.8, "top_k": 50, "max_new_tokens": 1500},
            )
            persisted = json.loads((out / "song.sidecar.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted, sidecar)

    def test_generation_controls_reach_the_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.mp3"
            audio.write_bytes(b"x")
            run_single(
                str(audio),
                Path(tmp) / "out",
                model_path="/m",
                run_id="r",
                generate_fn=self.fake_generate,
                temp=0.5,
                top_p=0.9,
                top_k=40,
                max_new_tokens=100,
            )
            self.assertEqual(self.calls[0]["temp"], 0.5)
            self.assertEqual(self.calls[0]["top_p"], 0.9)
            self.assertEqual(self.calls[0]["top_k"], 40)
            self.assertEqual(self.calls[0]["max_new_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
