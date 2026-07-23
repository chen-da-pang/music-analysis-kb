#!/usr/bin/env python3
"""Contract tests for the one-way GitHub-to-CNB runtime export."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
from export_cnb_runtime import ExportError, GITHUB_REPOSITORY, RUNNER_SUBTREE, export_runtime  # noqa: E402


REQUIRED = {
    ".cnb.yml": "pipeline: true\n",
    ".cnb/Dockerfile.flamingo": "FROM scratch\n",
    ".cnb/requirements.runtime.txt": "pytest==8.0.0\n",
    ".dockerignore": "data\n",
    ".gitignore": "data/\n",
    "README.md": "runner\n",
    "config/env.example": "AUDIO_ROOT=data/input\n",
    "scripts/run.py": "print('ok')\n",
    "scripts/devgpu_run_manual_kugou_quality_rerun.sh": "#!/usr/bin/env bash\nexit 0\n",
    "scripts/manual_kugou_quality_route.py": "print('manual route')\n",
    "scripts/check_manual_gpu_gate.py": "print('gpu gate')\n",
    "scripts/prepare_kugou_quality_rerun.sh": "#!/usr/bin/env bash\nexit 0\n",
    "scripts/prepare_kugou_quality_rerun.py": "print('prepare quality')\n",
    "tests/test_runtime.py": "assert True\n",
}


class ExportRuntimeTests(unittest.TestCase):
    def _repo(
        self, extras: dict[str, bytes | str] | None = None, *, omit: str | None = None
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        files = {**REQUIRED, **(extras or {})}
        if omit is not None:
            files.pop(omit)
        for path, content in files.items():
            target = root / RUNNER_SUBTREE / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        # Force-add the synthetic forbidden files so the exporter sees them even
        # when the fixture's .gitignore correctly excludes runtime data.
        subprocess.run(["git", "-C", str(root), "add", "-f", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        return temporary, root, commit

    def test_export_is_pinned_allowlisted_and_deterministic(self) -> None:
        temporary, root, commit = self._repo({"tools/not-exported.py": "print('helper')\n"})
        self.addCleanup(temporary.cleanup)
        first = Path(temporary.name) / "first"
        second = Path(temporary.name) / "second"
        first_provenance = export_runtime(root, commit, first, require_published=False)
        second_provenance = export_runtime(root, commit, second, require_published=False)
        self.assertEqual(first_provenance, second_provenance)
        self.assertFalse((first / "tools/not-exported.py").exists())
        self.assertEqual(json.loads((first / ".github-source.json").read_text()), first_provenance)
        self.assertEqual(
            hashlib.sha256((first / "scripts/run.py").read_bytes()).hexdigest(),
            hashlib.sha256(b"print('ok')\n").hexdigest(),
        )
        self.assertEqual((first / "scripts/run.py").read_bytes(), (second / "scripts/run.py").read_bytes())
        for path in (
            "scripts/devgpu_run_manual_kugou_quality_rerun.sh",
            "scripts/manual_kugou_quality_route.py",
            "scripts/check_manual_gpu_gate.py",
            "scripts/prepare_kugou_quality_rerun.sh",
            "scripts/prepare_kugou_quality_rerun.py",
        ):
            self.assertTrue((first / path).is_file(), path)
        self.assertEqual(first_provenance["source_repository"], GITHUB_REPOSITORY)
        self.assertEqual(first_provenance["source_commit"], commit)

    def test_forbidden_audio_and_production_paths_fail_closed(self) -> None:
        for path in (
            "scripts/track.mp3",
            "data/input/song.wav",
            "results/canonical_delivery_manifest.jsonl",
            "__pycache__/cached.pyc",
            "results/quality_report.json",
        ):
            temporary, root, commit = self._repo({path: b"not production data"})
            try:
                with self.assertRaisesRegex(ExportError, "forbidden|artifact|audio|database"):
                    export_runtime(root, commit, Path(temporary.name) / "out", require_published=False)
            finally:
                temporary.cleanup()

    def test_oversized_and_nonempty_outputs_fail(self) -> None:
        temporary, root, commit = self._repo({"scripts/too-large.bin": b"x" * (1024 * 1024 + 1)})
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ExportError, "exceeds"):
            export_runtime(root, commit, Path(temporary.name) / "out", require_published=False)

        temporary2, root2, commit2 = self._repo()
        self.addCleanup(temporary2.cleanup)
        output = Path(temporary2.name) / "out"
        output.mkdir()
        (output / "stale").write_text("stale")
        with self.assertRaisesRegex(ExportError, "must not already exist"):
            export_runtime(root2, commit2, output, require_published=False)

    def test_default_export_requires_a_published_github_source(self) -> None:
        temporary, root, commit = self._repo()
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ExportError, "origin"):
            export_runtime(root, commit, Path(temporary.name) / "out")

    def test_export_rejects_a_missing_manual_quality_route_file(self) -> None:
        temporary, root, commit = self._repo(omit="scripts/check_manual_gpu_gate.py")
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ExportError, "check_manual_gpu_gate.py"):
            export_runtime(root, commit, Path(temporary.name) / "out", require_published=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
