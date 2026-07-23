#!/usr/bin/env python3
"""Regression tests for the CNB Music Flamingo run contract."""

from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


class RunContextContractTests(unittest.TestCase):
    def test_run_context_is_run_scoped_and_rejects_path_traversal(self) -> None:
        from music_flamingo_run_context import RunContextError, resolve_run_context

        with tempfile.TemporaryDirectory() as temp_dir:
            context = resolve_run_context(
                {
                    "WORK_DIR": temp_dir,
                    "MUSIC_FLAMINGO_OUTPUT_NAME": "final_batch",
                    "MUSIC_FLAMINGO_RUN_ID": "cnb-demo-001",
                }
            )
            self.assertEqual(
                context.run_dir,
                Path(temp_dir) / "final_batch" / "cnb-demo-001",
            )
            with self.assertRaises(RunContextError):
                resolve_run_context(
                    {
                        "WORK_DIR": temp_dir,
                        "MUSIC_FLAMINGO_OUTPUT_NAME": "../escape",
                        "MUSIC_FLAMINGO_RUN_ID": "cnb-demo-001",
                    }
                )

    def test_status_is_single_run_atomic_document(self) -> None:
        from music_flamingo_run_context import read_status, write_status

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "batch" / "cnb-demo-001"
            running = write_status(
                run_dir,
                state="running",
                run_id="cnb-demo-001",
                pid=123,
                command_fragment="devgpu_run_batch.sh",
            )
            self.assertEqual(running["state"], "running")
            self.assertFalse((run_dir / "exit_code.txt").exists())
            completed = write_status(
                run_dir,
                state="success",
                run_id="cnb-demo-001",
                pid=123,
                exit_code=0,
            )
            self.assertEqual(completed["state"], "success")
            self.assertEqual(read_status(run_dir)["exit_code"], 0)
            self.assertEqual(list(run_dir.glob(".status-*.tmp")), [])

    def test_inference_paths_write_into_the_run_scoped_directory(self) -> None:
        from run_one_music_flamingo_smoke import paths

        with tempfile.TemporaryDirectory() as temp_dir:
            previous = os.environ.copy()
            try:
                os.environ.update(
                    {
                        "WORK_DIR": temp_dir,
                        "MUSIC_FLAMINGO_OUTPUT_NAME": "final_batch",
                        "MUSIC_FLAMINGO_RUN_ID": "cnb-demo-001",
                    }
                )
                _root, work_dir, output_dir, _model_dir, _model_id, _revision = paths()
            finally:
                os.environ.clear()
                os.environ.update(previous)
            self.assertEqual(work_dir, Path(temp_dir))
            self.assertEqual(output_dir, Path(temp_dir) / "final_batch" / "cnb-demo-001")


class BatchInstrumentationContractTests(unittest.TestCase):
    def test_generated_token_count_and_telemetry_flag_are_explicit(self) -> None:
        from run_music_flamingo_batch import (
            detailed_cuda_telemetry_enabled,
            generated_token_count,
            generation_controls_from_environment,
        )

        class TensorLike:
            def __init__(self, length: int):
                self.shape = (1, length)

        self.assertEqual(generated_token_count(TensorLike(15), {"input_ids": TensorLike(10)}), 5)
        self.assertEqual(generated_token_count(TensorLike(8), {"input_ids": TensorLike(10)}), 0)
        self.assertTrue(detailed_cuda_telemetry_enabled({"MUSIC_FLAMINGO_DETAILED_CUDA_TELEMETRY": "true"}))
        self.assertFalse(detailed_cuda_telemetry_enabled({"MUSIC_FLAMINGO_DETAILED_CUDA_TELEMETRY": "0"}))
        self.assertEqual(generation_controls_from_environment({}), {})
        self.assertEqual(
            generation_controls_from_environment(
                {
                    "MUSIC_FLAMINGO_REPETITION_PENALTY": "1.08",
                    "MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE": "4",
                }
            ),
            {"repetition_penalty": 1.08, "no_repeat_ngram_size": 4},
        )
        with self.assertRaises(ValueError):
            generation_controls_from_environment({"MUSIC_FLAMINGO_REPETITION_PENALTY": "0.99"})
        with self.assertRaises(ValueError):
            generation_controls_from_environment({"MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE": "-1"})


class RuntimeImplementationContractTests(unittest.TestCase):
    def test_generation_accounting_and_optional_memory_telemetry_are_wired_into_batch_path(self) -> None:
        text = (REPO_ROOT / "scripts/run_music_flamingo_batch.py").read_text(encoding="utf-8")
        self.assertIn("generated_tokens = generated_token_count(outputs, inputs)", text)
        self.assertIn("total_generated_tokens += generated_tokens", text)
        self.assertGreaterEqual(text.count("cuda_memory_snapshot(torch, enabled=detailed_cuda_telemetry)"), 4)

    def test_devgpu_wrapper_marks_interrupted_runs_terminally(self) -> None:
        text = (REPO_ROOT / "scripts/devgpu_run_batch.sh").read_text(encoding="utf-8")
        supervisor = (REPO_ROOT / "scripts/devgpu_batch_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("exec python scripts/devgpu_batch_supervisor.py", text)
        self.assertIn("signal.signal(signal.SIGTERM", supervisor)
        self.assertIn("interrupted:", supervisor)

    def test_devgpu_wrapper_persists_success_and_term_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("devgpu_run_batch.sh", "devgpu_batch_supervisor.py", "music_flamingo_run_context.py"):
                (scripts / name).write_text((REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8")
            (scripts / "devgpu_run_batch.sh").chmod(0o755)
            (scripts / "check_remote_env.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (scripts / "check_remote_env.sh").chmod(0o755)
            fake_batch = scripts / "run_music_flamingo_batch.py"
            env = os.environ | {
                "WORK_DIR": str(root / "out"),
                "MUSIC_FLAMINGO_OUTPUT_NAME": "batch",
                "MUSIC_FLAMINGO_RUN_ID": "success-run",
            }
            fake_batch.write_text("raise SystemExit(0)\n", encoding="utf-8")
            success = subprocess.run(["bash", "scripts/devgpu_run_batch.sh"], cwd=root, text=True, capture_output=True, env=env)
            self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
            status = json.loads((root / "out/batch/success-run/run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "success")
            self.assertEqual(status["exit_code"], 0)

            env["MUSIC_FLAMINGO_RUN_ID"] = "term-run"
            fake_batch.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            process = subprocess.Popen(["bash", "scripts/devgpu_run_batch.sh"], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            status_path = root / "out/batch/term-run/run_status.json"
            for _ in range(50):
                if status_path.exists():
                    break
                time.sleep(0.05)
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["reason"], "interrupted:TERM")

    def test_kugou_devgpu_wrapper_skips_runner_for_zero_pending_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("devgpu_run_kugou_campaign.sh", "music_flamingo_run_context.py"):
                source = REPO_ROOT / "scripts" / name
                target = scripts / name
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                target.chmod(0o755)

            (scripts / "check_remote_env.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (scripts / "check_remote_env.sh").chmod(0o755)
            (scripts / "campaign_ledger_git.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (scripts / "campaign_ledger_git.sh").chmod(0o755)
            (scripts / "prepare_kugou_campaign_shard.sh").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "run_dir=\"$WORK_DIR/$MUSIC_FLAMINGO_OUTPUT_NAME/$MUSIC_FLAMINGO_RUN_ID\"\n"
                "mkdir -p \"$run_dir\"\n"
                "printf '%s\\n' '{\"pending_item_count\": 0, \"shard_item_count\": 2}' > \"$run_dir/campaign_shard_plan.json\"\n",
                encoding="utf-8",
            )
            (scripts / "prepare_kugou_campaign_shard.sh").chmod(0o755)
            marker = root / "runner-called"
            (scripts / "run_music_flamingo_batch.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n"
                "raise SystemExit(99)\n",
                encoding="utf-8",
            )

            env = os.environ | {
                "WORK_DIR": str(root / "out"),
                "MUSIC_FLAMINGO_OUTPUT_NAME": "campaign",
                "MUSIC_FLAMINGO_RUN_ID": "zero-pending",
            }
            result = subprocess.run(
                ["bash", "scripts/devgpu_run_kugou_campaign.sh"],
                cwd=root,
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists(), result.stdout + result.stderr)
            run_dir = root / "out/campaign/zero-pending"
            status = json.loads((run_dir / "run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "success")
            self.assertEqual(status["exit_code"], 0)
            self.assertEqual((run_dir / "campaign_runner_exit_code.txt").read_text(encoding="utf-8"), "0\n")
            report = json.loads((run_dir / "campaign_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["campaign_status"], "already_complete_for_selected_shard")
            self.assertEqual(report["campaign"]["pending_item_count"], 0)


class LogViewerContractTests(unittest.TestCase):
    def test_log_viewer_uses_current_atomic_status_not_stale_exit_file(self) -> None:
        from devgpu_log_server import job_status
        from music_flamingo_run_context import write_status

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "devgpu_batch" / "cnb-demo-001"
            run_dir.mkdir(parents=True)
            (run_dir / "exit_code.txt").write_text("1\n", encoding="utf-8")
            write_status(run_dir, state="running", run_id="cnb-demo-001", pid=999999, command_fragment="missing")
            current = job_status(run_dir)
            self.assertEqual(current["state"], "stale")
            self.assertFalse(current["running"])
            self.assertIn("reason", current)
            self.assertNotIn("exit_code", current)
            write_status(run_dir, state="success", run_id="cnb-demo-001", pid=999999, exit_code=0)
            completed = job_status(run_dir)
            self.assertEqual(completed["state"], "success")
            self.assertEqual(completed["exit_code"], 0)


class PythonCompatibilityContractTests(unittest.TestCase):
    def test_remote_python_311_can_compile_all_runner_scripts(self) -> None:
        python311 = shutil.which("python3.11")
        self.assertIsNotNone(python311, "python3.11 is required to match the CNB runtime")
        scripts = sorted(str(path) for path in SCRIPTS.glob("*.py"))
        result = subprocess.run([python311, "-m", "py_compile", *scripts], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PipelineSelectionContractTests(unittest.TestCase):
    def test_selector_requires_explicit_pipeline_when_multiple_exist(self) -> None:
        from cnb_pipeline_selector import PipelineSelectionError, select_pipeline_id

        pipelines = {"cnb-z-001": {"status": "success"}, "cnb-a-001": {"status": "success"}}
        with self.assertRaises(PipelineSelectionError):
            select_pipeline_id(pipelines)
        self.assertEqual(select_pipeline_id(pipelines, "cnb-a-001"), "cnb-a-001")

    def test_selector_allows_the_only_pipeline(self) -> None:
        from cnb_pipeline_selector import select_pipeline_id

        self.assertEqual(select_pipeline_id({"cnb-only-001": {} }), "cnb-only-001")


class ImageBuildContractTests(unittest.TestCase):
    def test_image_build_uses_minimal_context_and_pinned_inputs(self) -> None:
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        dockerfile = (REPO_ROOT / ".cnb/Dockerfile.flamingo").read_text(encoding="utf-8")
        requirements = (REPO_ROOT / ".cnb/requirements.runtime.txt").read_text(encoding="utf-8")
        self.assertIn(".git", dockerignore)
        self.assertIn("data/input", dockerignore)
        self.assertIn(".env", dockerignore)
        self.assertIn("FROM pytorch/pytorch@sha256:", dockerfile)
        self.assertIn("COPY .cnb/requirements.runtime.txt", dockerfile)
        self.assertIn("accelerate==", requirements)
        self.assertIn("bitsandbytes==", requirements)
        self.assertIn("librosa==", requirements)
        self.assertIn("soundfile==", requirements)
        self.assertIn("huggingface_hub[hf_transfer]==", requirements)


class CNBConfigurationContractTests(unittest.TestCase):
    def load_config(self) -> dict:
        return yaml.safe_load((REPO_ROOT / ".cnb.yml").read_text(encoding="utf-8"))

    def test_final_analysis_parameters_are_not_reduced(self) -> None:
        config = self.load_config()["$"]
        for event_name in (
            "api_trigger",
            "api_trigger_official_music_flamingo",
            "api_trigger_batch10",
            "api_trigger_devgpu_batch10",
            "vscode",
        ):
            env = config[event_name][0]["env"]
            self.assertEqual(str(env["MUSIC_FLAMINGO_MAX_NEW_TOKENS"]), "2048", event_name)
            self.assertEqual(str(env["MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS"]), "240", event_name)

    def test_all_runtime_pipelines_use_one_immutable_image_and_run_scoped_persistent_output(self) -> None:
        config = self.load_config()["$"]
        expected_image = None
        for event_name in (
            "api_trigger",
            "api_trigger_official_music_flamingo",
            "api_trigger_batch10",
            "api_trigger_devgpu_batch10",
            "vscode",
        ):
            pipeline = config[event_name][0]
            image = pipeline["docker"]["image"]
            self.assertIn("@sha256:", image, event_name)
            expected_image = expected_image or image
            self.assertEqual(image, expected_image, event_name)
            env = pipeline["env"]
            self.assertEqual(env["CNB_RUNTIME_IMAGE"], image, event_name)
            self.assertEqual(env["WORK_DIR"], "/workspace/cache/output/music_flamingo_pipeline", event_name)
            self.assertEqual(env["MUSIC_FLAMINGO_RUN_ID"], "${CNB_BUILD_ID}", event_name)
            lock = pipeline["lock"]
            self.assertEqual(lock["key"], "music-flamingo-output-writer", event_name)
            self.assertTrue(lock["wait"], event_name)
            self.assertGreaterEqual(int(lock["timeout"]), 14_700, event_name)
            self.assertGreaterEqual(int(lock["expires"]), 14_700, event_name)

    def test_model_image_build_outputs_unique_candidate_not_the_promoted_tag(self) -> None:
        text = (REPO_ROOT / ".cnb.yml").read_text(encoding="utf-8")
        self.assertIn("music-flamingo-cuda-model-candidate-${CNB_BUILD_ID}", text)
        self.assertIn("candidate_image_digest", text)
        self.assertNotIn('image="${CNB_DOCKER_REGISTRY}/${CNB_REPO_SLUG_LOWERCASE}:music-flamingo-cuda-model"', text)
        candidate = self.load_config()["$"]["api_trigger_verify_candidate_image"][0]
        self.assertEqual(candidate["docker"]["image"], "${MUSIC_FLAMINGO_CANDIDATE_IMAGE}")
        self.assertEqual(candidate["env"]["CNB_RUNTIME_IMAGE"], "${MUSIC_FLAMINGO_CANDIDATE_IMAGE}")

    def test_kugou_campaign_is_sparse_lfs_sharded_and_has_a_bounded_preflight(self) -> None:
        campaign = self.load_config()["$"]["api_trigger_campaign_kugou_20260706"][0]
        self.assertFalse(campaign["git"]["lfs"])
        self.assertEqual(campaign["runner"]["tags"], "cnb:arch:amd64:gpu:L40")
        self.assertIn("@sha256:", campaign["docker"]["image"])
        env = campaign["env"]
        self.assertEqual(str(env["MUSIC_FLAMINGO_MAX_NEW_TOKENS"]), "2048")
        self.assertEqual(str(env["MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS"]), "240")
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT"]), "927")
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT"]), "4")
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_TRANSPORT"]), "lfs")
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_GIT_OBJECTS_MAX_BYTES"]), "5000000000")
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_GIT_OBJECTS_MAX_FILE_BYTES"]), "268435456")
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_PREFLIGHT_ONLY"]), "0")
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS"]), "0")
        self.assertEqual(campaign["lock"]["key"], "kugou-20260706-ledger-writer")
        self.assertGreaterEqual(int(campaign["lock"]["expires"]), 18_000)
        stages = {stage["name"]: stage.get("script", "") for stage in campaign["stages"]}
        self.assertIn("expected_shard_id", stages["Validate immutable runtime and shard request"])
        hydrate = stages["Hydrate and run sparse KuGou campaign shard"]
        shard_helper = (REPO_ROOT / "scripts/prepare_kugou_campaign_shard.sh").read_text(encoding="utf-8")
        self.assertIn("git lfs pull --include=", shard_helper)
        self.assertIn("git-objects)", shard_helper)
        self.assertIn("LFS hydration skipped", shard_helper)
        self.assertIn("Git-object campaign exceeds total cap", shard_helper)
        self.assertNotIn("git lfs install --local", shard_helper)
        self.assertIn("MUSIC_FLAMINGO_CAMPAIGN_PREFLIGHT_ONLY", hydrate)
        self.assertIn("MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS=1", hydrate)
        self.assertIn("campaign_ledger_git.sh checkpoint", hydrate)

    def test_generated_disposable_campaign_route_has_no_weekly_hardcoding(self) -> None:
        campaign = self.load_config()["$"]["api_trigger_music_flamingo_campaign"][0]
        self.assertFalse(campaign["git"]["lfs"])
        self.assertEqual(campaign["runner"]["tags"], "cnb:arch:amd64:gpu:L40")
        self.assertIn("@sha256:", campaign["docker"]["image"])
        self.assertEqual(campaign["env"]["CNB_RUNTIME_IMAGE"], campaign["docker"]["image"])
        self.assertEqual(campaign["stages"][0]["script"], "bash scripts/run_music_flamingo_campaign.sh")
        text = (REPO_ROOT / "scripts/run_music_flamingo_campaign.sh").read_text(encoding="utf-8")
        self.assertIn("MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID", text)
        self.assertIn("MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256", text)
        self.assertIn("Campaign manifest SHA-256 mismatch", text)
        self.assertIn("campaign_ledger_git.sh restore", text)
        self.assertIn("prepare_kugou_campaign_shard.sh", text)
        self.assertNotIn("20260716", text)

    def test_kugou_quality_rerun_is_isolated_and_uses_anti_repetition_controls(self) -> None:
        rerun = self.load_config()["$"]["api_trigger_kugou_quality_rerun_12"][0]
        self.assertFalse(rerun["git"]["lfs"])
        self.assertEqual(rerun["runner"]["tags"], "cnb:arch:amd64:gpu:L40")
        env = rerun["env"]
        self.assertEqual(str(env["MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT"]), "12")
        self.assertEqual(str(env["MUSIC_FLAMINGO_MAX_NEW_TOKENS"]), "1400")
        self.assertEqual(str(env["MUSIC_FLAMINGO_REPETITION_PENALTY"]), "1.08")
        self.assertEqual(str(env["MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE"]), "4")
        self.assertEqual(env["MUSIC_FLAMINGO_LEDGER_BRANCH"], "campaign-results/kugou-20260706-quality-rerun-12")
        stages = {stage["name"]: stage.get("script", "") for stage in rerun["stages"]}
        self.assertIn("prepare_kugou_quality_rerun.sh", stages["Hydrate and rerun selected KuGou outputs"])
        self.assertIn("campaign_ledger_git.sh restore", stages["Restore isolated quality-rerun ledger"])

    def test_vscode_is_manual_only_and_cannot_start_the_primary_campaign_runner(self) -> None:
        workspace = self.load_config()["$"]["vscode"][0]
        env = workspace["env"]
        stages = {stage["name"]: stage.get("script", "") for stage in workspace["stages"]}
        self.assertEqual(env["MUSIC_FLAMINGO_MANUAL_QUALITY_ROUTE"], "1")
        self.assertEqual(env["MUSIC_FLAMINGO_MANUAL_GPU_NAME"], "L40")
        self.assertEqual(str(env["MUSIC_FLAMINGO_MANUAL_GPU_MIN_FREE_MIB"]), "40000")
        self.assertEqual(str(env["MUSIC_FLAMINGO_MANUAL_MAX_SELECTED_COUNT"]), "5")
        self.assertEqual(str(env["MUSIC_FLAMINGO_REPETITION_PENALTY"]), "1.08")
        self.assertEqual(str(env["MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE"]), "4")
        for forbidden in (
            "MUSIC_FLAMINGO_CAMPAIGN_ID",
            "MUSIC_FLAMINGO_LEDGER_BRANCH",
            "MUSIC_FLAMINGO_QUALITY_SELECTION_FILE",
            "MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT",
            "MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256",
        ):
            self.assertNotIn(forbidden, env)
        terminal_script = stages["Open manual-only Dev GPU terminal"]
        self.assertNotIn("devgpu_run_kugou_campaign.sh", terminal_script)
        self.assertNotIn(
            "bash scripts/devgpu_run_manual_kugou_quality_rerun.sh",
            [line.strip() for line in terminal_script.splitlines()],
        )

        manual = (REPO_ROOT / "scripts/devgpu_run_manual_kugou_quality_rerun.sh").read_text(encoding="utf-8")
        self.assertNotIn("prepare_kugou_campaign_shard.sh", manual)
        self.assertNotIn("devgpu_run_kugou_campaign.sh", manual)
        self.assertIn("manual_kugou_quality_route.py", manual)
        self.assertIn("prepare_kugou_quality_rerun.sh", manual)
        self.assertIn('[[ "$repetition_penalty" == "1.08" ]]', manual)
        self.assertIn('[[ "$no_repeat_ngram_size" == "4" ]]', manual)
        self.assertIn("--repetition-penalty \"$repetition_penalty\"", manual)
        self.assertIn("--no-repeat-ngram-size \"$no_repeat_ngram_size\"", manual)
        self.assertLess(
            manual.index('[[ "$repetition_penalty" == "1.08" ]]'),
            manual.index("manual_gpu_gate_before_hydrate.json"),
        )
        self.assertLess(
            manual.index("manual_gpu_gate_before_hydrate.json"),
            manual.index("prepare_kugou_quality_rerun.sh"),
        )
        self.assertLess(
            manual.index("prepare_kugou_quality_rerun.sh"),
            manual.index("manual_gpu_gate_pre_model.json"),
        )

    def test_manual_quality_shell_rejects_wrong_controls_before_gpu_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts = root / "scripts"
            scripts.mkdir()
            shell = scripts / "devgpu_run_manual_kugou_quality_rerun.sh"
            shell.write_text(
                (REPO_ROOT / "scripts" / shell.name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            shell.chmod(0o755)
            (scripts / "music_flamingo_run_context.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                f"root = Path({str(root)!r})\n"
                "if sys.argv[1] == 'print-dir':\n"
                "    print(root / 'out')\n",
                encoding="utf-8",
            )
            (scripts / "manual_kugou_quality_route.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            marker = root / "gpu-gate-called"
            (scripts / "check_manual_gpu_gate.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n",
                encoding="utf-8",
            )
            base_env = os.environ | {
                "MUSIC_FLAMINGO_MANUAL_QUALITY_ROUTE": "1",
                "MUSIC_FLAMINGO_CAMPAIGN_ID": "campaign-a",
                "MUSIC_FLAMINGO_QUALITY_SOURCE_MANIFEST": "input/manifest.jsonl",
                "MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT": "input",
                "MUSIC_FLAMINGO_QUALITY_SELECTION_FILE": "selection.txt",
                "MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256": "0" * 64,
                "MUSIC_FLAMINGO_QUALITY_SOURCE_EXPECTED_COUNT": "1",
                "MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT": "1",
                "MUSIC_FLAMINGO_LEDGER_BRANCH": "campaign-results/campaign-a-quality-rerun-l40-probe-1",
                "MUSIC_FLAMINGO_EXECUTION_PROFILE": "nvidia-l40/full_precision/bfloat16",
                "MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED": "1",
                "MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY": "1",
            }
            for controls, expected_error in (
                (
                    {
                        "MUSIC_FLAMINGO_REPETITION_PENALTY": "1.07",
                        "MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE": "4",
                    },
                    "MUSIC_FLAMINGO_REPETITION_PENALTY=1.08",
                ),
                (
                    {
                        "MUSIC_FLAMINGO_REPETITION_PENALTY": "1.08",
                        "MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE": "3",
                    },
                    "MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE=4",
                ),
            ):
                with self.subTest(controls=controls):
                    result = subprocess.run(
                        ["bash", "scripts/devgpu_run_manual_kugou_quality_rerun.sh"],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        env=base_env | controls,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(expected_error, result.stderr)
                    self.assertFalse(marker.exists(), result.stdout + result.stderr)


class ArtifactRetentionContractTests(unittest.TestCase):
    def test_remote_batch_routes_package_and_upload_run_evidence(self) -> None:
        config = yaml.safe_load((REPO_ROOT / ".cnb.yml").read_text(encoding="utf-8"))["$"]
        for event_name in ("api_trigger", "api_trigger_official_music_flamingo", "api_trigger_batch10", "api_trigger_devgpu_batch10"):
            stages = config[event_name][0]["stages"]
            names = [stage["name"] for stage in stages]
            self.assertIn("Package Music Flamingo run evidence", names, event_name)
            upload = next(stage for stage in stages if stage["name"] == "Upload Music Flamingo run evidence")
            self.assertEqual(upload["image"], "cnbcool/attachments:latest", event_name)
            self.assertIn("cnb-artifacts/music-flamingo-run.tar.gz", upload["settings"]["attachments"], event_name)
        self.assertTrue((REPO_ROOT / "scripts/package_music_flamingo_run.sh").exists())

    def test_packager_archives_the_exact_run_scoped_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir) / "work"
            run_dir = work_dir / "batch" / "cnb-demo-001"
            run_dir.mkdir(parents=True)
            (run_dir / "batch_report.json").write_text('{"status":"success"}\n', encoding="utf-8")
            artifact = REPO_ROOT / "cnb-artifacts/music-flamingo-run.tar.gz"
            try:
                result = subprocess.run(
                    ["bash", "scripts/package_music_flamingo_run.sh"],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    env=os.environ | {
                        "WORK_DIR": str(work_dir),
                        "MUSIC_FLAMINGO_OUTPUT_NAME": "batch",
                        "MUSIC_FLAMINGO_RUN_ID": "cnb-demo-001",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(artifact.exists())
                listing = subprocess.run(["tar", "-tzf", str(artifact)], text=True, capture_output=True, check=True).stdout
                self.assertIn("cnb-demo-001/batch_report.json", listing)
            finally:
                artifact.unlink(missing_ok=True)
                try:
                    artifact.parent.rmdir()
                except OSError:
                    pass


class ShellBehaviorContractTests(unittest.TestCase):
    def test_prepare_watcher_returns_nonzero_for_failed_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            responses = temp / "responses"
            responses.mkdir()
            status = {
                "data": {
                    "pipelinesStatus": {
                        "cnb-test-001": {
                            "stages": [{"id": "prepare", "status": "failed", "duration": 1}]
                        }
                    }
                }
            }
            stage = {"data": {"status": "failed", "duration": 1, "content": ["prepare failed"]}}
            (responses / "status.json").write_text(json.dumps(status), encoding="utf-8")
            (responses / "stage.json").write_text(json.dumps(stage), encoding="utf-8")
            fake_cnb = fake_bin / "cnb"
            fake_cnb.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [[ \"$*\" == *get-build-status* ]]; then cat \"$CNB_TEST_STATUS\"; else cat \"$CNB_TEST_STAGE\"; fi\n",
                encoding="utf-8",
            )
            fake_cnb.chmod(0o755)
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "CNB_TEST_STATUS": str(responses / "status.json"),
                "CNB_TEST_STAGE": str(responses / "stage.json"),
                "CNB_WATCH_INTERVAL_SECONDS": "1",
                "CNB_WATCH_MAX_POLLS": "1",
            }
            result = subprocess.run(
                ["bash", "scripts/watch_cnb_prepare.sh", "cnb-test"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("terminal_status=failed", result.stdout + result.stderr)

    def test_prepare_watcher_rejects_invalid_explicit_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            status = {"data": {"pipelinesStatus": {"cnb-valid-001": {"stages": []}}}}
            status_path = temp / "status.json"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            fake_cnb = fake_bin / "cnb"
            fake_cnb.write_text("#!/usr/bin/env bash\nset -eu\ncat \"$CNB_TEST_STATUS\"\n", encoding="utf-8")
            fake_cnb.chmod(0o755)
            env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "CNB_TEST_STATUS": str(status_path)}
            result = subprocess.run(
                ["bash", "scripts/watch_cnb_prepare.sh", "cnb-test", "cnb-invalid-001"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("could not select pipeline", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
