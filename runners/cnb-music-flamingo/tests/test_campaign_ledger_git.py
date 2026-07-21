#!/usr/bin/env python3
"""Black-box tests for the durable Git campaign ledger transport."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/campaign_ledger_git.sh"


class CampaignLedgerGitTests(unittest.TestCase):
    def test_restore_then_checkpoint_updates_a_dedicated_git_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote = root / "ledger.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "init", str(seed)], check=True, capture_output=True, text=True)
            for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", str(seed), "config", key, value], check=True)
            (seed / "campaign_ledger.jsonl").write_text("", encoding="utf-8")
            (seed / "campaign_state.json").write_text('{"state":"initialized"}\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(seed), "branch", "-M", "campaign-results/test"], check=True)
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:refs/heads/campaign-results/test"], check=True, capture_output=True, text=True)

            ledger = root / "run/campaign_ledger.jsonl"
            signing_home = root / "signing-home"
            signing_home.mkdir()
            (signing_home / ".gitconfig").write_text("[commit]\n\tgpgSign = true\n", encoding="utf-8")
            env = os.environ | {
                "HOME": str(signing_home),
                "MUSIC_FLAMINGO_LEDGER_REPO_URL": str(remote),
                "MUSIC_FLAMINGO_LEDGER_BRANCH": "campaign-results/test",
                "MUSIC_FLAMINGO_LEDGER_GIT_USER_NAME": "Campaign Test",
                "MUSIC_FLAMINGO_LEDGER_GIT_USER_EMAIL": "campaign@example.invalid",
            }
            restored = subprocess.run(["bash", str(SCRIPT), "restore", str(ledger)], text=True, capture_output=True, env=env)
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            self.assertEqual(ledger.read_text(encoding="utf-8"), "")

            record = {"status": "success", "id": "song-a", "contract": "same"}
            ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
            saved = subprocess.run(["bash", str(SCRIPT), "checkpoint", str(ledger)], text=True, capture_output=True, env=env)
            self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)

            verify = root / "verify"
            subprocess.run(["git", "clone", "--branch", "campaign-results/test", str(remote), str(verify)], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads((verify / "campaign_ledger.jsonl").read_text(encoding="utf-8")), record)
            state = json.loads((verify / "campaign_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["ledger_record_count"], 1)

    def test_restore_initializes_an_empty_ledger_when_a_new_campaign_has_no_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote = root / "campaign.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "init", str(seed)], check=True, capture_output=True, text=True)
            for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", str(seed), "config", key, value], check=True)
            (seed / "README.md").write_text("campaign\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "main"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True)
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:refs/heads/main"], check=True, capture_output=True, text=True)
            ledger = root / "run/campaign_ledger.jsonl"
            env = os.environ | {
                "MUSIC_FLAMINGO_LEDGER_REPO_URL": str(remote),
                "MUSIC_FLAMINGO_LEDGER_BRANCH": "campaign-results/new-run",
            }
            restored = subprocess.run(["bash", str(SCRIPT), "restore", str(ledger)], text=True, capture_output=True, env=env)
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            self.assertEqual(ledger.read_text(encoding="utf-8"), "")
            self.assertIn("campaign_ledger_branch_missing_initialized", restored.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
