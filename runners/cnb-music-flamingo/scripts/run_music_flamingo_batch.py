#!/usr/bin/env python3
import gc
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import traceback
from pathlib import Path

from music_flamingo_campaign import (
    CampaignItem,
    append_campaign_ledger,
    build_runtime_contract_from_environment,
    campaign_input_config_from_environment,
    campaign_mode_enabled,
    is_reusable_success,
    load_campaign_items,
    make_campaign_ledger_record,
    pending_campaign_items,
    read_campaign_ledger,
    validate_execution_profile,
)
from run_one_music_flamingo_smoke import (
    RUNTIME_SOURCE_NOTE,
    command_output,
    ffprobe_duration,
    inventory_is_available,
    model_inventory,
    paths,
    prompt_text,
    write_json,
    write_text,
)


AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
PROGRESS_LOG_PATH: Path | None = None
SCRIPT_STARTED = time.time()


def detailed_cuda_telemetry_enabled(env: dict | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("MUSIC_FLAMINGO_DETAILED_CUDA_TELEMETRY", "1")).strip().lower() in {"1", "true", "yes", "on"}


def generation_controls_from_environment(env: dict | None = None) -> dict[str, float | int]:
    """Return explicit, opt-in anti-repetition controls for ``generate``.

    The defaults preserve the established campaign behavior.  A recovery run
    can enable these controls without changing the promoted runtime image.
    Keeping the parser here makes the exact generation settings visible in
    progress events and the batch report.
    """
    values = os.environ if env is None else env
    controls: dict[str, float | int] = {}

    penalty_text = str(values.get("MUSIC_FLAMINGO_REPETITION_PENALTY", "1.0")).strip()
    try:
        repetition_penalty = float(penalty_text)
    except ValueError as exc:
        raise ValueError("MUSIC_FLAMINGO_REPETITION_PENALTY must be a finite number >= 1.0") from exc
    if not math.isfinite(repetition_penalty) or repetition_penalty < 1.0:
        raise ValueError("MUSIC_FLAMINGO_REPETITION_PENALTY must be a finite number >= 1.0")
    if repetition_penalty != 1.0:
        controls["repetition_penalty"] = repetition_penalty

    ngram_text = str(values.get("MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE", "0")).strip()
    try:
        no_repeat_ngram_size = int(ngram_text)
    except ValueError as exc:
        raise ValueError("MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE must be a non-negative integer") from exc
    if no_repeat_ngram_size < 0:
        raise ValueError("MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE must be a non-negative integer")
    if no_repeat_ngram_size:
        controls["no_repeat_ngram_size"] = no_repeat_ngram_size
    return controls


def generated_token_count(outputs, inputs) -> int:
    return max(0, int(outputs.shape[-1]) - int(inputs["input_ids"].shape[-1]))


def cuda_memory_snapshot(torch_module, *, enabled: bool = True) -> dict:
    if not enabled or not torch_module.cuda.is_available():
        return {}
    torch_module.cuda.synchronize()
    free_bytes, total_bytes = torch_module.cuda.mem_get_info()
    return {
        "allocated_mb": round(torch_module.cuda.memory_allocated() / (1024**2), 2),
        "reserved_mb": round(torch_module.cuda.memory_reserved() / (1024**2), 2),
        "max_allocated_mb": round(torch_module.cuda.max_memory_allocated() / (1024**2), 2),
        "max_reserved_mb": round(torch_module.cuda.max_memory_reserved() / (1024**2), 2),
        "free_mb": round(free_bytes / (1024**2), 2),
        "total_mb": round(total_bytes / (1024**2), 2),
    }


def cleanup_item_cuda(torch_module) -> None:
    gc.collect()
    if torch_module.cuda.is_available():
        torch_module.cuda.empty_cache()
        torch_module.cuda.ipc_collect()
        torch_module.cuda.synchronize()


def log_progress(message: str, **fields) -> None:
    payload = {
        "event": message,
        "elapsed_since_script_start_seconds": round(time.time() - SCRIPT_STARTED, 3),
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False)
    print("[flamingo-batch-progress] " + line, flush=True)
    if PROGRESS_LOG_PATH is not None:
        PROGRESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PROGRESS_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def discover_audio_files(root: Path, limit: int) -> list[Path]:
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    if limit > 0:
        files = files[:limit]
    if not files:
        raise SystemExit(f"No audio files found under {root}")
    return files


def safe_stem(index: int, audio_path: Path) -> str:
    return f"{index:04d}_{audio_path.stem[:80]}"


def write_batch_index(output_dir: Path, audio_root: Path, audio_files: list[Path]) -> None:
    records = [
        {
            "index": idx,
            "audio_path": str(audio_path),
            "info": audio_path.stem,
            "relative_path": str(audio_path.relative_to(audio_root).as_posix())
            if audio_root in audio_path.parents
            else audio_path.name,
        }
        for idx, audio_path in enumerate(audio_files, 1)
    ]
    write_json(output_dir / "batch_index.json", records)


def campaign_attempt_id() -> str:
    """Return a stable-enough, safe attempt id without changing campaign run id."""
    candidate = str(os.environ.get("CNB_BUILD_ID") or os.environ.get("CNB_PIPELINE_ID") or "local-attempt").strip()
    return candidate or "local-attempt"


def campaign_reusable_records(
    items: list[CampaignItem],
    ledger_path: Path,
    contract: str,
) -> dict[str, dict[str, object]]:
    """Return the final valid durable success record for each selected item."""
    by_id = {item.item_id: item for item in items}
    reusable: dict[str, dict[str, object]] = {}
    for record in read_campaign_ledger(ledger_path):
        item = by_id.get(record.get("id")) if isinstance(record.get("id"), str) else None
        if item is not None and is_reusable_success(record, item, contract):
            reusable[item.item_id] = record
    return reusable


def write_campaign_index(
    output_dir: Path,
    items: list[CampaignItem],
    reusable_records: dict[str, dict[str, object]],
) -> None:
    write_json(
        output_dir / "campaign_index.json",
        [
            {
                "id": item.item_id,
                "manifest_index": item.manifest_index,
                "relative_audio_path": item.relative_audio_path,
                "source_bytes": item.source_bytes,
                "sha256": item.sha256,
                "title": item.title,
                "artist": item.artist,
                "campaign_id": item.campaign_id,
                "reused_from_durable_ledger": item.item_id in reusable_records,
            }
            for item in items
        ],
    )


def restore_campaign_output_files(
    output_dir: Path,
    reusable_records: dict[str, dict[str, object]],
) -> None:
    """Recreate final text files when a resumed job lands on a new CNB node."""
    for item_id, record in reusable_records.items():
        output_text = record.get("output_text")
        if not isinstance(output_text, str):
            raise RuntimeError(f"Reusable campaign success lacks output_text for {item_id}")
        item_dir = output_dir / "items" / item_id
        write_text(item_dir / "stdout.txt", output_text)


def campaign_checkpoint(root: Path, ledger_path: Path, *, required: bool) -> None:
    """Push the local fsynced ledger to its durable Git branch or fail closed."""
    if not required:
        return
    result = subprocess.run(
        ["bash", str(root / "scripts/campaign_ledger_git.sh"), "checkpoint", str(ledger_path)],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Durable campaign ledger checkpoint failed; refusing to continue with node-local-only results.\n"
            + result.stdout
            + result.stderr
        )
    log_progress("campaign_ledger_checkpointed", ledger_path=str(ledger_path), checkpoint_output=result.stdout.strip())


def campaign_execution_profile(gpu_name: str, load_strategy: str, compute_dtype: str) -> str:
    gpu_lower = gpu_name.lower()
    if "l40" in gpu_lower:
        gpu = "nvidia-l40"
    else:
        gpu = "-".join("".join(ch if ch.isalnum() else " " for ch in gpu_lower).split())
    return f"{gpu}/{load_strategy}/{compute_dtype}"


def write_campaign_retry_manifest(output_dir: Path, items: list[CampaignItem], *, reason: str) -> None:
    path = output_dir / "campaign_retry_manifest.jsonl"
    lines = []
    for item in items:
        lines.append(
            json.dumps(
                {
                    "id": item.item_id,
                    "manifest_index": item.manifest_index,
                    "relative_audio_path": item.relative_audio_path,
                    "source_bytes": item.source_bytes,
                    "sha256": item.sha256,
                    "title": item.title,
                    "artist": item.artist,
                    "campaign_id": item.campaign_id,
                    "reason": reason,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def main() -> None:
    global PROGRESS_LOG_PATH

    root, work_dir, output_dir, model_dir, model_id, revision = paths()
    output_dir.mkdir(parents=True, exist_ok=True)
    campaign_enabled = campaign_mode_enabled()
    if campaign_enabled:
        PROGRESS_LOG_PATH = output_dir / f"progress_{campaign_attempt_id()}.jsonl"
    else:
        PROGRESS_LOG_PATH = output_dir / "progress.jsonl"
    PROGRESS_LOG_PATH.write_text("", encoding="utf-8")
    log_progress("script_start", output_dir=str(output_dir), campaign_mode=campaign_enabled)

    prompt, prompt_source = prompt_text()
    max_new_tokens = int(os.environ.get("MUSIC_FLAMINGO_MAX_NEW_TOKENS", "2048"))
    generation_controls = generation_controls_from_environment()
    clip_seconds = float(os.environ.get("MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS", "240"))
    keep_input_clips = os.environ.get("MUSIC_FLAMINGO_KEEP_INPUT_CLIPS", "").lower() in {"1", "true", "yes"}
    detailed_cuda_telemetry = detailed_cuda_telemetry_enabled()
    campaign_items: list[CampaignItem] = []
    campaign_pending: list[CampaignItem] = []
    campaign_reused: dict[str, dict[str, object]] = {}
    campaign_contract = None
    campaign_ledger_path: Path | None = None
    campaign_checkpoint_required = False
    campaign_checkpoint_every = 0
    campaign_max_elapsed_seconds = 0.0
    campaign_shard_id = ""

    if campaign_enabled:
        campaign_config = campaign_input_config_from_environment()
        campaign_items = load_campaign_items(
            campaign_config.manifest_path,
            campaign_config.audio_root,
            expected_count=campaign_config.expected_count,
            expected_campaign_id=campaign_config.expected_campaign_id,
        )
        campaign_contract = build_runtime_contract_from_environment(
            os.environ.get("CNB_RUNTIME_IMAGE", ""),
            prompt,
            max_new_tokens,
            clip_seconds,
            env=os.environ,
        )
        campaign_ledger_path = output_dir / "campaign_ledger.jsonl"
        campaign_reused = campaign_reusable_records(campaign_items, campaign_ledger_path, campaign_contract.fingerprint)
        restore_campaign_output_files(output_dir, campaign_reused)
        campaign_pending = pending_campaign_items(campaign_items, campaign_ledger_path, campaign_contract.fingerprint)
        audio_root = campaign_config.audio_root.resolve()
        audio_files = [item.audio_path for item in campaign_pending]
        batch_limit = 0
        campaign_checkpoint_required = str(
            os.environ.get("MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED", "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        campaign_checkpoint_every = int(os.environ.get("MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY", "5"))
        if campaign_checkpoint_every < 1:
            raise RuntimeError("MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY must be positive in campaign mode")
        campaign_max_elapsed_seconds = float(os.environ.get("MUSIC_FLAMINGO_CAMPAIGN_MAX_ELAPSED_SECONDS", "0"))
        if campaign_max_elapsed_seconds < 0:
            raise RuntimeError("MUSIC_FLAMINGO_CAMPAIGN_MAX_ELAPSED_SECONDS must not be negative")
        campaign_shard_id = str(os.environ.get("MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID", "")).strip()
        write_campaign_index(output_dir, campaign_items, campaign_reused)
        log_progress(
            "campaign_discovered",
            campaign_id=campaign_config.expected_campaign_id,
            selected_item_count=len(campaign_items),
            pending_item_count=len(campaign_pending),
            reusable_success_count=len(campaign_reused),
            ledger_path=str(campaign_ledger_path),
            contract=campaign_contract.fingerprint,
            shard_id=campaign_shard_id,
        )
    else:
        audio_root = Path(os.environ.get("AUDIO_ROOT", root / "data/input"))
        if not audio_root.is_absolute():
            audio_root = root / audio_root
        audio_root = audio_root.resolve()
        batch_limit = int(os.environ.get("MUSIC_FLAMINGO_BATCH_LIMIT", "10"))
        audio_files = discover_audio_files(audio_root, batch_limit)
        write_batch_index(output_dir, audio_root, audio_files)
        log_progress("batch_discovered", audio_count=len(audio_files), audio_root=str(audio_root), batch_limit=batch_limit)

    started = time.time()
    report = {
        "status": "started",
        "backend": "transformers",
        "runtime_source": "cnb_docker_artifact_image",
        "runtime_image": os.environ.get("CNB_RUNTIME_IMAGE", ""),
        "runtime_source_note": RUNTIME_SOURCE_NOTE,
        "model_id": model_id,
        "revision": revision,
        "model_dir": str(model_dir),
        "audio_root": str(audio_root),
        "batch_limit": batch_limit,
        "audio_count": len(audio_files),
        "prompt_source": prompt_source,
        "prompt": prompt,
        "max_new_tokens": max_new_tokens,
        "generation_controls": generation_controls,
        "audio_clip_seconds": clip_seconds,
        "keep_input_clips": keep_input_clips,
        "detailed_cuda_telemetry": detailed_cuda_telemetry,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "nvidia_smi_before": command_output(["nvidia-smi"]) if shutil.which("nvidia-smi") else "",
        "items": [],
    }
    if campaign_enabled:
        assert campaign_contract is not None
        assert campaign_ledger_path is not None
        report["campaign"] = {
            "campaign_id": campaign_items[0].campaign_id if campaign_items else os.environ.get("MUSIC_FLAMINGO_CAMPAIGN_ID", ""),
            "shard_id": campaign_shard_id,
            "selected_item_count": len(campaign_items),
            "pending_item_count": len(campaign_pending),
            "reused_success_count": len(campaign_reused),
            "ledger_path": str(campaign_ledger_path),
            "contract": campaign_contract.fingerprint,
            "contract_fields": campaign_contract.ledger_fields(),
            "checkpoint_every": campaign_checkpoint_every,
            "max_elapsed_seconds": campaign_max_elapsed_seconds,
        }
    write_json(output_dir / "batch_report.json", report)
    log_progress(
        "batch_report_initialized",
        prompt_source=prompt_source,
        max_new_tokens=max_new_tokens,
        generation_controls=generation_controls,
        audio_clip_seconds=clip_seconds,
        runtime_image=report["runtime_image"],
    )

    if campaign_enabled and not audio_files:
        report.update(
            {
                "status": "success",
                "campaign_status": "already_complete_for_selected_shard",
                "elapsed_seconds": round(time.time() - started, 3),
                "script_elapsed_seconds": round(time.time() - SCRIPT_STARTED, 3),
            }
        )
        write_json(output_dir / "batch_report.json", report)
        log_progress("campaign_shard_already_complete", reused_success_count=len(campaign_reused))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return

    import_started = time.time()
    log_progress("python_imports_start")
    import librosa
    import soundfile as sf
    import torch
    from transformers import BitsAndBytesConfig, Qwen2TokenizerFast, WhisperFeatureExtractor
    from transformers.models.musicflamingo.configuration_musicflamingo import MusicFlamingoConfig
    from transformers.models.musicflamingo.modeling_musicflamingo import MusicFlamingoForConditionalGeneration
    from transformers.models.musicflamingo.processing_musicflamingo import MusicFlamingoProcessor
    log_progress("python_imports_done", import_seconds=round(time.time() - import_started, 3))

    try:
        model_verify_started = time.time()
        log_progress("model_files_verify_start", model_dir=str(model_dir), model_id=model_id, revision=revision)
        model_inventory_report = model_inventory(model_dir, model_id, revision)
        model_report = {
            "status": "success" if inventory_is_available(model_inventory_report) else "missing",
            "runtime_source": "cnb_docker_artifact_image",
            "runtime_image": os.environ.get("CNB_RUNTIME_IMAGE", ""),
            "runtime_source_note": RUNTIME_SOURCE_NOTE,
            **model_inventory_report,
        }
        write_json(output_dir / "model_report.json", model_report)
        log_progress(
            "model_files_verify_done",
            status=model_report["status"],
            verify_seconds=round(time.time() - model_verify_started, 3),
            file_count=model_report.get("file_count"),
            total_bytes=model_report.get("total_bytes"),
            weight_file_count=len(model_report.get("weight_files", [])),
        )
        if model_report["status"] != "success":
            raise RuntimeError(f"Music Flamingo model files not found under {model_dir}")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU is not available.")

        gpu_name = torch.cuda.get_device_name(0)
        gpu_mem_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 2)
        cuda_capability = torch.cuda.get_device_capability(0)
        bf16_supported = bool(cuda_capability[0] >= 8 and torch.cuda.is_bf16_supported())
        compute_dtype = torch.bfloat16 if bf16_supported else torch.float16
        report.update(
            {
                "gpu_name": gpu_name,
                "gpu_memory_gb": gpu_mem_gb,
                "cuda_capability": list(cuda_capability),
                "bf16_supported": bf16_supported,
                "compute_dtype": str(compute_dtype).replace("torch.", ""),
            }
        )
        log_progress("gpu_ready", gpu_name=gpu_name, gpu_memory_gb=gpu_mem_gb, compute_dtype=str(compute_dtype).replace("torch.", ""))

        processor_started = time.time()
        log_progress("processor_init_start")
        processor_config = json.loads((model_dir / "processor_config.json").read_text(encoding="utf-8"))
        chat_template = (model_dir / "chat_template.jinja").read_text(encoding="utf-8")
        feature_extractor = WhisperFeatureExtractor.from_pretrained(str(model_dir), local_files_only=True)
        tokenizer = Qwen2TokenizerFast.from_pretrained(str(model_dir), padding_side="left", local_files_only=True)
        processor = MusicFlamingoProcessor(
            feature_extractor=feature_extractor,
            tokenizer=tokenizer,
            chat_template=chat_template,
            audio_token=processor_config.get("audio_token", "<sound>"),
            audio_bos_token=processor_config.get("audio_bos_token", "<|sound_bos|>"),
            audio_eos_token=processor_config.get("audio_eos_token", "<|sound_eos|>"),
            max_audio_len=processor_config.get("max_audio_len", 1200),
        )
        log_progress("processor_init_done", processor_seconds=round(time.time() - processor_started, 3))

        config_started = time.time()
        log_progress("model_config_load_start")
        raw_config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
        allowed_config_keys = {
            "audio_config",
            "text_config",
            "audio_token_id",
            "projector_hidden_act",
            "projector_bias",
            "audio_bos_token_id",
            "audio_eos_token_id",
            "head_dim",
            "rope_parameters",
        }
        config = MusicFlamingoConfig(**{key: value for key, value in raw_config.items() if key in allowed_config_keys})
        log_progress("model_config_load_done", config_seconds=round(time.time() - config_started, 3))

        def load_4bit():
            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=compute_dtype,
            )
            return MusicFlamingoForConditionalGeneration.from_pretrained(
                str(model_dir),
                config=config,
                device_map="auto",
                quantization_config=quant_config,
                low_cpu_mem_usage=True,
                local_files_only=True,
            )

        def load_8bit():
            quant_config = BitsAndBytesConfig(load_in_8bit=True)
            return MusicFlamingoForConditionalGeneration.from_pretrained(
                str(model_dir),
                config=config,
                device_map="auto",
                quantization_config=quant_config,
                low_cpu_mem_usage=True,
                local_files_only=True,
            )

        def load_full_precision():
            return MusicFlamingoForConditionalGeneration.from_pretrained(
                str(model_dir),
                config=config,
                device_map="auto",
                torch_dtype=compute_dtype,
                low_cpu_mem_usage=True,
                local_files_only=True,
            )

        if gpu_mem_gb >= 40:
            load_attempts = [("full_precision", load_full_precision), ("4bit", load_4bit), ("8bit", load_8bit)]
        else:
            load_attempts = [("4bit", load_4bit), ("8bit", load_8bit), ("full_precision", load_full_precision)]

        load_started = time.time()
        load_errors = []
        model = None
        for strategy_name, loader in load_attempts:
            try:
                log_progress("model_load_start", strategy=strategy_name)
                model = loader()
                report["load_strategy"] = strategy_name
                log_progress("model_load_strategy_success", strategy=strategy_name)
                break
            except Exception as exc:
                load_errors.append({"strategy": strategy_name, "error": repr(exc)})
                log_progress("model_load_failed", strategy=strategy_name, error=repr(exc))
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        if model is None:
            report["load_errors"] = load_errors
            raise RuntimeError("All Music Flamingo model loading strategies failed.")
        model.eval()
        report["model_load_seconds"] = round(time.time() - load_started, 3)

        multimodal_dtype = compute_dtype
        try:
            multimodal_dtype = model.get_input_embeddings().weight.dtype
        except Exception:
            pass
        report["multimodal_dtype"] = str(multimodal_dtype).replace("torch.", "")
        for module_name in ("audio_tower", "multi_modal_projector", "pos_emb"):
            module = getattr(model, module_name, None)
            if module is not None and hasattr(module, "to"):
                log_progress("model_module_dtype_cast_start", module=module_name, dtype=str(multimodal_dtype).replace("torch.", ""))
                module.to(dtype=multimodal_dtype)
                log_progress("model_module_dtype_cast_done", module=module_name)
        audio_tower_dtype = multimodal_dtype
        if hasattr(model, "audio_tower"):
            try:
                audio_tower_dtype = next(model.audio_tower.parameters()).dtype
            except StopIteration:
                pass
        report["audio_tower_dtype"] = str(audio_tower_dtype).replace("torch.", "")
        if campaign_enabled:
            assert campaign_contract is not None
            actual_execution_profile = campaign_execution_profile(
                gpu_name,
                str(report["load_strategy"]),
                str(compute_dtype).replace("torch.", ""),
            )
            validate_execution_profile(campaign_contract, actual_execution_profile)
            report["campaign"]["actual_execution_profile"] = actual_execution_profile
            log_progress(
                "campaign_execution_profile_verified",
                expected_profile=campaign_contract.execution_profile,
                actual_profile=actual_execution_profile,
            )

        input_device = torch.device("cuda:0")
        cleanup_item_cuda(torch)
        model_resident_memory = cuda_memory_snapshot(torch, enabled=detailed_cuda_telemetry)
        report["gpu_memory_after_model_load_cleanup"] = model_resident_memory
        log_progress(
            "model_ready",
            load_strategy=report.get("load_strategy"),
            model_load_seconds=report["model_load_seconds"],
            gpu_memory_after_model_load_cleanup=model_resident_memory,
        )
        total_processed_seconds = 0.0
        total_generate_seconds = 0.0
        total_generated_tokens = 0
        campaign_items_by_audio_path = {item.audio_path: item for item in campaign_pending}
        campaign_error_items: list[CampaignItem] = []
        campaign_deferred_items: list[CampaignItem] = []
        campaign_outcomes_since_checkpoint = 0
        campaign_loop_started = time.monotonic()
        continue_on_error = str(os.environ.get("MUSIC_FLAMINGO_CONTINUE_ON_ERROR", "0")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        for sequence, audio_path in enumerate(audio_files, 1):
            campaign_item = campaign_items_by_audio_path.get(audio_path)
            if campaign_enabled and campaign_max_elapsed_seconds and time.monotonic() - campaign_loop_started >= campaign_max_elapsed_seconds:
                campaign_deferred_items = [
                    campaign_items_by_audio_path[path]
                    for path in audio_files[sequence - 1 :]
                    if path in campaign_items_by_audio_path
                ]
                log_progress(
                    "campaign_time_budget_reached",
                    max_elapsed_seconds=campaign_max_elapsed_seconds,
                    attempted_count=sequence - 1,
                    deferred_count=len(campaign_deferred_items),
                )
                break
            index = campaign_item.manifest_index if campaign_item is not None else sequence
            item_started = time.time()
            inputs = None
            outputs = None
            audio_samples = None
            clip_path = None
            item_dir = output_dir / "items" / (campaign_item.item_id if campaign_item is not None else safe_stem(index, audio_path))
            item_dir.mkdir(parents=True, exist_ok=True)
            item = {
                "index": index,
                "sequence": sequence,
                "audio_path": str(audio_path),
                "audio_name": audio_path.name,
                "status": "started",
                "gpu_memory_before": cuda_memory_snapshot(torch, enabled=detailed_cuda_telemetry),
            }
            if campaign_item is not None:
                item.update(
                    {
                        "campaign_item_id": campaign_item.item_id,
                        "campaign_id": campaign_item.campaign_id,
                        "manifest_index": campaign_item.manifest_index,
                        "source_sha256": campaign_item.sha256,
                        "source_bytes": campaign_item.source_bytes,
                    }
                )
            before_allocated_mb = item["gpu_memory_before"].get("allocated_mb")
            log_progress(
                "item_start",
                index=index,
                sequence=sequence,
                total=len(audio_files),
                audio_name=audio_path.name,
                campaign_item_id=campaign_item.item_id if campaign_item is not None else None,
                gpu_memory_before=item["gpu_memory_before"],
            )
            try:
                if torch.cuda.is_available():
                    torch.cuda.reset_peak_memory_stats()
                audio_prepare_started = time.time()
                log_progress("item_audio_prepare_start", index=index, total=len(audio_files), audio_name=audio_path.name)
                original_duration = ffprobe_duration(audio_path)
                clip_path = item_dir / "input_clip.wav"
                audio_samples, sample_rate = librosa.load(str(audio_path), sr=16000, mono=True, duration=clip_seconds)
                sf.write(str(clip_path), audio_samples, sample_rate)
                processing_duration = len(audio_samples) / float(sample_rate)
                total_processed_seconds += processing_duration
                log_progress(
                    "item_audio_prepared",
                    index=index,
                    total=len(audio_files),
                    prepare_seconds=round(time.time() - audio_prepare_started, 3),
                    processing_duration_seconds=round(processing_duration, 3),
                    original_duration_seconds=round(original_duration, 3) if original_duration else None,
                    was_truncated=bool(original_duration and original_duration > clip_seconds),
                )

                template_started = time.time()
                log_progress("item_chat_template_start", index=index, total=len(audio_files))
                conversation = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "audio", "path": str(clip_path)},
                        ],
                    }
                ]
                inputs = processor.apply_chat_template(
                    conversation,
                    tokenize=True,
                    add_generation_prompt=True,
                    return_dict=True,
                ).to(input_device)
                if "input_features" in inputs:
                    inputs["input_features"] = inputs["input_features"].to(audio_tower_dtype)
                log_progress("item_chat_template_done", index=index, total=len(audio_files), template_seconds=round(time.time() - template_started, 3))
                log_progress(
                    "item_generate_start",
                    index=index,
                    total=len(audio_files),
                    max_new_tokens=max_new_tokens,
                    generation_controls=generation_controls,
                )

                generate_started = time.time()
                with torch.inference_mode():
                    outputs = model.generate(**inputs, max_new_tokens=max_new_tokens, **generation_controls)
                if torch.cuda.is_available():
                    torch.cuda.synchronize()
                generate_seconds = time.time() - generate_started
                generated_tokens = generated_token_count(outputs, inputs)
                total_generate_seconds += generate_seconds
                total_generated_tokens += generated_tokens
                decode_started = time.time()
                log_progress("item_decode_start", index=index, total=len(audio_files))
                final_text = processor.batch_decode(
                    outputs[:, inputs.input_ids.shape[1] :],
                    skip_special_tokens=True,
                )[0]
                cleaned_text = final_text.replace("<think>", "").replace("</think>", "").strip() + "\n"
                write_text(item_dir / "stdout_raw.txt", final_text)
                write_text(item_dir / "stdout.txt", cleaned_text)
                after_generate_memory = cuda_memory_snapshot(torch, enabled=detailed_cuda_telemetry)
                log_progress("item_decode_done", index=index, total=len(audio_files), decode_seconds=round(time.time() - decode_started, 3))
                log_progress(
                    "item_generate_done",
                    index=index,
                    total=len(audio_files),
                    generate_seconds=round(generate_seconds, 3),
                    generated_token_count=generated_tokens,
                    output_chars=len(final_text),
                    gpu_memory_after_generate=after_generate_memory,
                )
                item.update(
                    {
                        "status": "success",
                        "original_duration_seconds": round(original_duration, 3) if original_duration else None,
                        "processing_duration_seconds": round(processing_duration, 3),
                        "was_truncated": bool(original_duration and original_duration > clip_seconds),
                        "generate_seconds": round(generate_seconds, 3),
                        "generated_token_count": generated_tokens,
                        "elapsed_seconds": round(time.time() - item_started, 3),
                        "output_chars": len(final_text),
                        "output_dir": str(item_dir),
                        "gpu_memory_after_generate": after_generate_memory,
                    }
                )
                if campaign_item is not None:
                    assert campaign_contract is not None
                    assert campaign_ledger_path is not None
                    success_record = make_campaign_ledger_record(
                        campaign_item,
                        campaign_contract,
                        status="success",
                        attempt_id=campaign_attempt_id(),
                        output_text=cleaned_text,
                        output_text_sha256=hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest(),
                        original_duration_seconds=item["original_duration_seconds"],
                        processing_duration_seconds=item["processing_duration_seconds"],
                        was_truncated=item["was_truncated"],
                        generate_seconds=item["generate_seconds"],
                        generated_token_count=item["generated_token_count"],
                        output_chars=item["output_chars"],
                        generation_controls=generation_controls,
                    )
                    append_campaign_ledger(campaign_ledger_path, success_record)
                    item["campaign_ledger_status"] = "success"
                    log_progress(
                        "campaign_ledger_success_appended",
                        index=index,
                        campaign_item_id=campaign_item.item_id,
                        ledger_path=str(campaign_ledger_path),
                    )
            except Exception:
                item.update(
                    {
                        "status": "error",
                        "error": traceback.format_exc(),
                        "elapsed_seconds": round(time.time() - item_started, 3),
                        "output_dir": str(item_dir),
                    }
                )
                write_text(item_dir / "stderr.txt", item["error"])
                log_progress("item_error", index=index, total=len(audio_files), error=item["error"])
                if campaign_item is not None:
                    assert campaign_contract is not None
                    assert campaign_ledger_path is not None
                    try:
                        append_campaign_ledger(
                            campaign_ledger_path,
                            make_campaign_ledger_record(
                                campaign_item,
                                campaign_contract,
                                status="error",
                                attempt_id=campaign_attempt_id(),
                                error=item["error"],
                                output_dir=str(item_dir),
                                generation_controls=generation_controls,
                            ),
                        )
                        item["campaign_ledger_status"] = "error"
                        campaign_error_items.append(campaign_item)
                        log_progress(
                            "campaign_ledger_error_appended",
                            index=index,
                            campaign_item_id=campaign_item.item_id,
                            ledger_path=str(campaign_ledger_path),
                        )
                    except Exception:
                        item["campaign_ledger_error"] = traceback.format_exc()
                        raise
            finally:
                log_progress("item_cleanup_start", index=index, total=len(audio_files))
                del inputs, outputs, audio_samples
                if clip_path is not None and not keep_input_clips:
                    try:
                        clip_path.unlink(missing_ok=True)
                    except Exception as exc:
                        item["clip_cleanup_error"] = repr(exc)
                cleanup_item_cuda(torch)
                after_cleanup_memory = cuda_memory_snapshot(torch, enabled=detailed_cuda_telemetry)
                item["gpu_memory_after_cleanup"] = after_cleanup_memory
                after_allocated_mb = after_cleanup_memory.get("allocated_mb")
                if before_allocated_mb is not None and after_allocated_mb is not None:
                    item["gpu_memory_allocated_delta_after_cleanup_mb"] = round(after_allocated_mb - before_allocated_mb, 2)
                log_progress(
                    "item_cleanup_done",
                    index=index,
                    total=len(audio_files),
                    status=item["status"],
                    gpu_memory_after_cleanup=after_cleanup_memory,
                    gpu_memory_allocated_delta_after_cleanup_mb=item.get("gpu_memory_allocated_delta_after_cleanup_mb"),
                )
            report["items"].append(item)
            write_json(output_dir / "batch_report.json", report)
            log_progress(
                "item_report_written",
                index=index,
                total=len(audio_files),
                status=item["status"],
                elapsed_seconds=item.get("elapsed_seconds"),
            )
            if campaign_item is not None:
                campaign_outcomes_since_checkpoint += 1
                if campaign_outcomes_since_checkpoint >= campaign_checkpoint_every:
                    assert campaign_ledger_path is not None
                    campaign_checkpoint(root, campaign_ledger_path, required=campaign_checkpoint_required)
                    campaign_outcomes_since_checkpoint = 0
                if item["status"] != "success" and not continue_on_error:
                    raise RuntimeError(f"Campaign item failed: {audio_path}")
            elif item["status"] != "success":
                raise RuntimeError(f"Batch item failed: {audio_path}")

        if campaign_enabled:
            assert campaign_ledger_path is not None
            if campaign_outcomes_since_checkpoint or campaign_error_items or campaign_deferred_items:
                campaign_checkpoint(root, campaign_ledger_path, required=campaign_checkpoint_required)
                campaign_outcomes_since_checkpoint = 0
            retry_by_id = {item.item_id: item for item in [*campaign_error_items, *campaign_deferred_items]}
            retry_items = [item for item in campaign_pending if item.item_id in retry_by_id]
            write_campaign_retry_manifest(
                output_dir,
                retry_items,
                reason="item_error" if campaign_error_items else "campaign_time_budget_reached",
            )
            if campaign_error_items:
                campaign_status = "completed_with_errors"
            elif campaign_deferred_items:
                campaign_status = "partial"
            else:
                campaign_status = "success"
            report["campaign"].update(
                {
                    "campaign_status": campaign_status,
                    "attempted_item_count": len(report["items"]),
                    "new_success_count": sum(1 for item in report["items"] if item.get("status") == "success"),
                    "error_item_count": len(campaign_error_items),
                    "deferred_item_count": len(campaign_deferred_items),
                    "retry_manifest": str(output_dir / "campaign_retry_manifest.jsonl"),
                }
            )
        report.update(
            {
                "status": "success" if not campaign_enabled or not campaign_deferred_items else "partial",
                "total_processed_seconds": round(total_processed_seconds, 3),
                "total_generate_seconds": round(total_generate_seconds, 3),
                "total_generated_tokens": total_generated_tokens,
                "elapsed_seconds": round(time.time() - started, 3),
                "script_elapsed_seconds": round(time.time() - SCRIPT_STARTED, 3),
                "nvidia_smi_after": command_output(["nvidia-smi"]) if shutil.which("nvidia-smi") else "",
            }
        )
        write_json(output_dir / "batch_report.json", report)
        if campaign_enabled:
            write_json(output_dir / "campaign_report.json", report)
        log_progress(
            "batch_done",
            audio_count=len(audio_files),
            total_processed_seconds=report["total_processed_seconds"],
            total_generate_seconds=report["total_generate_seconds"],
            total_generated_tokens=report["total_generated_tokens"],
            elapsed_seconds=report["elapsed_seconds"],
            script_elapsed_seconds=report["script_elapsed_seconds"],
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if campaign_enabled and campaign_error_items:
            raise RuntimeError(f"Campaign shard completed with {len(campaign_error_items)} item error(s)")
    except Exception:
        report.update({
            "status": "error",
            "error": traceback.format_exc(),
            "elapsed_seconds": round(time.time() - started, 3),
            "script_elapsed_seconds": round(time.time() - SCRIPT_STARTED, 3),
        })
        write_text(output_dir / "stderr.txt", report["error"])
        write_json(output_dir / "batch_report.json", report)
        if campaign_enabled:
            write_json(output_dir / "campaign_report.json", report)
        print(report["error"], flush=True)
        raise


if __name__ == "__main__":
    main()
