#!/usr/bin/env python3
import argparse
import gc
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from music_flamingo_run_context import resolve_run_context


DEFAULT_PROMPT = (
    "Describe this track in full detail - tell me the genre, tempo, broad tonal center, "
    "rhythmic feel, instrumentation, vocal character, production style, structure, "
    "and overall mood and atmosphere it creates. "
    "Focus only on audible musical and performance details. Ignore lyrical content entirely "
    "and do not mention, summarize, quote, or transcribe any lyrics."
)

RUNTIME_SOURCE_NOTE = (
    "CNB should run this script inside the promoted CNB Docker Artifact image. "
    "That image carries the CUDA/Python runtime, "
    "Music Flamingo dependencies, and model files under /opt/models, so normal "
    "smoke runs do not pip install dependencies or download model weights."
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def paths() -> tuple[Path, Path, Path, Path, str, str]:
    root = project_root()
    context = resolve_run_context(
        default_work_dir=root / "data/output/music_flamingo_pipeline",
        default_output_name="one_smoke",
    )
    model_dir = Path(os.environ.get("MUSIC_FLAMINGO_MODEL_DIR", "/opt/models/music-flamingo-think-2601-hf"))
    model_id = os.environ.get("MUSIC_FLAMINGO_MODEL", "nvidia/music-flamingo-think-2601-hf")
    revision = os.environ.get("MUSIC_FLAMINGO_REVISION", "1ea2109")
    return root, context.work_dir, context.run_dir, model_dir, model_id, revision


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def model_inventory(model_dir: Path, model_id: str, revision: str) -> dict:
    files = [p for p in model_dir.rglob("*") if p.is_file()] if model_dir.exists() else []
    weight_files = sorted(
        [p for p in model_dir.rglob("*.safetensors") if p.is_file()]
        + [p for p in model_dir.rglob("*.bin") if p.is_file()]
    )
    required = ["config.json", "processor_config.json", "chat_template.jinja"]
    return {
        "model_id": model_id,
        "revision": revision,
        "model_dir": str(model_dir),
        "exists": model_dir.exists(),
        "file_count": len(files),
        "total_bytes": sum(p.stat().st_size for p in files),
        "missing_required_files": [name for name in required if not (model_dir / name).exists()],
        "weight_files": [
            {"name": p.name, "relative_path": str(p.relative_to(model_dir)), "bytes": p.stat().st_size}
            for p in weight_files
        ],
    }


def inventory_is_available(inventory: dict) -> bool:
    return bool(inventory["exists"] and inventory["weight_files"] and not inventory["missing_required_files"])


def model_is_available(model_dir: Path, model_id: str, revision: str) -> bool:
    return inventory_is_available(model_inventory(model_dir, model_id, revision))


def command_output(command: list[str]) -> str:
    try:
        return subprocess.run(command, check=False, text=True, capture_output=True).stdout.strip()
    except Exception as exc:
        return repr(exc)


def load_one_audio(output_dir: Path) -> Path:
    one_jsonl = output_dir / "one_audio.jsonl"
    if not one_jsonl.exists():
        raise SystemExit("Missing one_audio.jsonl. Run: bash scripts/pick_one_audio.sh")
    first = one_jsonl.read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(first)
    audio_path = Path(record["audio_path"])
    if not audio_path.exists():
        raise SystemExit(f"Audio file missing: {audio_path}")
    return audio_path


def ffprobe_duration(audio_path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    try:
        return float(result.stdout.strip())
    except Exception:
        return None


def prompt_text() -> tuple[str, str]:
    override = os.environ.get("MUSIC_FLAMINGO_PROMPT")
    if override:
        return override, "MUSIC_FLAMINGO_PROMPT"
    prompt_file = os.environ.get("MUSIC_FLAMINGO_PROMPT_FILE")
    if prompt_file:
        path = Path(prompt_file)
        if not path.is_absolute():
            path = project_root() / path
        return path.read_text(encoding="utf-8").strip(), str(path)
    return DEFAULT_PROMPT, "DEFAULT_PROMPT"


def verify_model(_args) -> None:
    _root, _work_dir, output_dir, model_dir, model_id, revision = paths()
    inventory = model_inventory(model_dir, model_id, revision)
    report = {
        "status": "success" if inventory_is_available(inventory) else "missing",
        "runtime_source": "cnb_docker_artifact_image",
        "runtime_image": os.environ.get("CNB_RUNTIME_IMAGE", ""),
        "runtime_source_note": RUNTIME_SOURCE_NOTE,
        **inventory,
    }
    write_json(output_dir / "model_report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if report["status"] != "success":
        raise SystemExit(f"Music Flamingo model files not found under {model_dir}")


def infer(_args) -> None:
    import librosa
    import soundfile as sf
    import torch
    from transformers import BitsAndBytesConfig, Qwen2TokenizerFast, WhisperFeatureExtractor
    from transformers.models.musicflamingo.configuration_musicflamingo import MusicFlamingoConfig
    from transformers.models.musicflamingo.modeling_musicflamingo import MusicFlamingoForConditionalGeneration
    from transformers.models.musicflamingo.processing_musicflamingo import MusicFlamingoProcessor

    root, work_dir, output_dir, model_dir, model_id, revision = paths()
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = load_one_audio(output_dir)
    prompt, prompt_source = prompt_text()
    max_new_tokens = int(os.environ.get("MUSIC_FLAMINGO_MAX_NEW_TOKENS", "2048"))
    clip_seconds = float(os.environ.get("MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS", "240"))
    report = {
        "status": "started",
        "backend": "transformers",
        "runtime_source": "cnb_docker_artifact_image",
        "runtime_image": os.environ.get("CNB_RUNTIME_IMAGE", ""),
        "runtime_source_note": RUNTIME_SOURCE_NOTE,
        "model_id": model_id,
        "revision": revision,
        "model_dir": str(model_dir),
        "audio_path": str(audio_path),
        "prompt_source": prompt_source,
        "prompt": prompt,
        "max_new_tokens": max_new_tokens,
        "audio_clip_seconds": clip_seconds,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "nvidia_smi_before": command_output(["nvidia-smi"]) if shutil.which("nvidia-smi") else "",
    }
    write_json(output_dir / "run_report.json", report)

    started = time.time()
    try:
        inventory = model_inventory(model_dir, model_id, revision)
        if not inventory_is_available(inventory):
            raise RuntimeError(f"Music Flamingo model files not found under {model_dir}")
        report["model_inventory"] = inventory
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
                print(f"[flamingo] trying load strategy: {strategy_name}", flush=True)
                model = loader()
                report["load_strategy"] = strategy_name
                break
            except Exception as exc:
                load_errors.append({"strategy": strategy_name, "error": repr(exc)})
                print(f"[flamingo] load strategy failed: {strategy_name} {exc!r}", flush=True)
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        if model is None:
            report["load_errors"] = load_errors
            raise RuntimeError("All Music Flamingo model loading strategies failed.")
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
                module.to(dtype=multimodal_dtype)
        audio_tower_dtype = multimodal_dtype
        if hasattr(model, "audio_tower"):
            try:
                audio_tower_dtype = next(model.audio_tower.parameters()).dtype
            except StopIteration:
                pass
        report["audio_tower_dtype"] = str(audio_tower_dtype).replace("torch.", "")

        original_duration = ffprobe_duration(audio_path)
        clip_path = output_dir / "input_clip.wav"
        audio_samples, sample_rate = librosa.load(str(audio_path), sr=16000, mono=True, duration=clip_seconds)
        sf.write(str(clip_path), audio_samples, sample_rate)
        processing_duration = len(audio_samples) / float(sample_rate)
        report.update(
            {
                "original_duration_seconds": round(original_duration, 3) if original_duration else None,
                "processing_duration_seconds": round(processing_duration, 3),
                "was_truncated": bool(original_duration and original_duration > clip_seconds),
            }
        )

        conversation = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "audio", "path": str(clip_path)},
                ],
            }
        ]
        input_device = torch.device("cuda:0")
        inputs = processor.apply_chat_template(
            conversation,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
        ).to(input_device)
        if "input_features" in inputs:
            inputs["input_features"] = inputs["input_features"].to(audio_tower_dtype)

        generate_started = time.time()
        print("[flamingo] starting generation", flush=True)
        outputs = model.generate(**inputs, max_new_tokens=max_new_tokens)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        generate_seconds = time.time() - generate_started
        input_token_count = int(inputs.input_ids.shape[-1])
        generated_token_count = max(0, int(outputs.shape[-1]) - input_token_count)
        final_text = processor.batch_decode(
            outputs[:, input_token_count:],
            skip_special_tokens=True,
        )[0]
        report.update(
            {
                "status": "success",
                "generate_seconds": round(generate_seconds, 3),
                "generated_token_count": generated_token_count,
                "elapsed_seconds": round(time.time() - started, 3),
                "output_chars": len(final_text),
                "nvidia_smi_after": command_output(["nvidia-smi"]) if shutil.which("nvidia-smi") else "",
            }
        )
        write_text(output_dir / "stdout_raw.txt", final_text)
        write_text(output_dir / "stdout.txt", final_text.replace("<think>", "").replace("</think>", "").strip() + "\n")
        write_json(output_dir / "run_report.json", report)
        print(final_text, flush=True)
    except Exception:
        tb = traceback.format_exc()
        report.update({"status": "error", "error": tb, "elapsed_seconds": round(time.time() - started, 3)})
        write_text(output_dir / "stderr.txt", tb)
        write_json(output_dir / "run_report.json", report)
        print(tb, flush=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify")
    subparsers.add_parser("infer")
    args = parser.parse_args()
    if args.command == "verify":
        verify_model(args)
    elif args.command == "infer":
        infer(args)


if __name__ == "__main__":
    main()
