"""Publisher-only, offline audio embeddings. Never opens the master for writing."""

import argparse
import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import stat
import struct
import sys
from datetime import datetime, timezone

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/music-kb-numba")
os.environ["HF_HUB_OFFLINE"] = "1"

CHANNELS = {
    "overall": {"model_id": "muq-mulan-v3", "model_name": "OpenMuQ/MuQ-MuLan-large",
                "recipe": "v3", "dim": 512, "factor": ""},
    "rhythm": {"model_id": "merit-mert330m-w3-v2", "model_name": "amaai-lab/merit",
               "recipe": "mert330m-l3-4-5-6-23-window3-v2", "dim": 128, "factor": "rhythm"},
    "timbre": {"model_id": "merit-mert330m-w3-v2", "model_name": "amaai-lab/merit",
               "recipe": "mert330m-l3-4-5-6-23-window3-v2", "dim": 128, "factor": "timbre"},
}
MERT_REVISION = "5240c2708a5acaee1007f43fb9735c7dcd0b78c9"
MERIT_REVISION = "a85df30eca1ba112eb594285f3ba1d96488e7883"
MUQ_REVISION = "2e01c796b71dca71b45251384c04cd7b237c9020"
EXTENSIONS = {".flac", ".mp3", ".ogg", ".m4a"}
ALTERNATE_ASSET_KIND = "alternate"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect_readonly(database):
    return sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)


def load_identity_map(path):
    """Load per-file alternate-rendition identity rows (JSONL).

    Required row fields: recording_id, path, downloaded_sha256, asset_kind
    (alternate). Optional provenance copied onto output rows: platform_file_hash,
    source_url, served_quality, title, artist. The digest is verified in
    scan_audio so a file that changed after the download is never embedded.
    """

    path = Path(path)
    entries = {}
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                raise ValueError("Identity map cannot contain blank lines")
            try:
                row = json.loads(raw_line)
            except ValueError as error:
                raise ValueError(f"Identity map line {line_number} is not valid JSON") from error
            if not isinstance(row, dict):
                raise ValueError(f"Identity map line {line_number} must be a JSON object")
            recording_id = str(row.get("recording_id") or "").strip()
            file_path = str(row.get("path") or "").strip()
            digest = str(row.get("downloaded_sha256") or row.get("sha256") or "").strip().lower()
            asset_kind = str(row.get("asset_kind") or ALTERNATE_ASSET_KIND).strip()
            if not recording_id:
                raise ValueError(f"Identity map line {line_number} needs a recording_id")
            if not file_path:
                raise ValueError(f"Identity map line {line_number} needs a path")
            if not SHA256_HEX.fullmatch(digest):
                raise ValueError(f"Identity map line {line_number} needs a downloaded_sha256 of 64 lowercase hex characters")
            if asset_kind != ALTERNATE_ASSET_KIND:
                raise ValueError(f"Identity map line {line_number} asset_kind must be alternate")
            if recording_id in entries:
                raise ValueError(f"Identity map repeats recording_id {recording_id}")
            entries[recording_id] = {
                "recording_id": recording_id,
                "path": str(Path(file_path).expanduser()),
                "audio_sha256": digest,
                "asset_kind": asset_kind,
                **{
                    key: row[key]
                    for key in ("platform_file_hash", "source_url", "served_quality", "title", "artist")
                    if row.get(key) is not None
                },
            }
    if not entries:
        raise ValueError("Identity map is empty")
    return entries


def scan_audio(root, db, identity_map=None):
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"Audio root does not exist: {root}")
    with connect_readonly(db) as connection:
        identities = {}
        known_recordings = set()
        for recording_id, audio_sha256 in connection.execute("SELECT id, audio_sha256 FROM recording"):
            identities.setdefault(audio_sha256, []).append(recording_id)
            known_recordings.add(recording_id)
    candidates, failures, seen = [], [], set()

    def scan_error(error):
        failures.append({"path": str(error.filename), "error": str(error)})

    paths = sorted(Path(directory) / name
                   for directory, _, names in os.walk(root, onerror=scan_error) for name in names)
    for path in paths:
        if path.suffix.lower() not in EXTENSIONS:
            continue
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                continue
            audio_sha256 = sha256_file(path)
            for recording_id in identities.get(audio_sha256, []):
                if recording_id not in seen:
                    candidates.append({"recording_id": recording_id, "audio_sha256": audio_sha256,
                                       "path": str(path.resolve())})
                    seen.add(recording_id)
        except OSError as error:
            failures.append({"path": str(path), "error": str(error)})
    if identity_map:
        for recording_id in sorted(identity_map):
            if recording_id in seen:
                continue
            row = identity_map[recording_id]
            file_path = Path(row["path"])
            if recording_id not in known_recordings:
                failures.append({"path": str(file_path), "recording_id": recording_id,
                                 "error": "Identity map recording does not exist"})
                continue
            try:
                info = file_path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                    raise ValueError("identity map audio is not a regular non-empty file")
                if sha256_file(file_path) != row["audio_sha256"]:
                    raise ValueError("identity map audio sha256 mismatch")
            except (OSError, ValueError) as error:
                failures.append({"path": str(file_path), "recording_id": recording_id,
                                 "error": f"{type(error).__name__}: {error}"})
                continue
            candidate = {"recording_id": recording_id, "audio_sha256": row["audio_sha256"],
                         "path": str(file_path.resolve()), "asset_kind": row["asset_kind"]}
            for key in ("platform_file_hash", "source_url", "served_quality", "title", "artist"):
                if key in row:
                    candidate[key] = row[key]
            candidates.append(candidate)
            seen.add(recording_id)
    return candidates, failures


def vector_bytes(vector, dim):
    if len(vector) != dim:
        raise ValueError(f"Expected {dim} dimensions, got {len(vector)}")
    payload = struct.pack(f"<{dim}f", *vector)
    values = struct.unpack(f"<{dim}f", payload)
    if not all(math.isfinite(value) for value in values) or sum(value * value for value in values) <= 0:
        raise ValueError("Vector must be finite and nonzero")
    return payload


def read_resume(path, dim, identities, alternate_sha_by_recording=None):
    completed = set()
    if not path.exists():
        return completed
    with path.open("r+b") as source:
        while True:
            offset = source.tell()
            line = source.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                source.truncate(offset)
                break
            entry = json.loads(line)
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) and entry[key]
                for key in ("recording_id", "audio_sha256", "vector_b64")
            ):
                raise ValueError("Resume row must contain nonempty recording_id, audio_sha256, vector_b64 strings")
            recording_id = entry["recording_id"]
            if not isinstance(recording_id, str) or not identities.get(recording_id):
                raise ValueError(f"Resume identity mismatch: {recording_id}")
            if entry.get("asset_kind") == ALTERNATE_ASSET_KIND:
                expected_sha = (alternate_sha_by_recording or {}).get(recording_id)
                if expected_sha != entry["audio_sha256"]:
                    raise ValueError(f"Resume alternate identity mismatch: {recording_id}")
            elif entry["audio_sha256"] != identities[recording_id]:
                raise ValueError(f"Resume identity mismatch: {recording_id}")
            payload = base64.b64decode(entry["vector_b64"], validate=True)
            if len(payload) != dim * 4:
                raise ValueError("Resume vector dimension mismatch")
            vector_bytes(struct.unpack(f"<{dim}f", payload), dim)
            if recording_id in completed:
                raise ValueError(f"Duplicate resume recording: {recording_id}")
            completed.add(recording_id)
    return completed


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


def all_clips(audio):
    import numpy as np

    clip_size = 240_000
    if len(audio) < clip_size:
        raise ValueError("MuQ v3 requires at least 10 seconds")
    clips, offset = [], 0
    while offset + clip_size <= len(audio):
        clips.append(audio[offset:offset + clip_size])
        offset += clip_size
    if offset < len(audio):
        clips.append(np.concatenate([audio[offset:], audio[:clip_size - (len(audio) - offset)]]))
    return clips


MERIT_WINDOW_SECONDS = 30.0


def merit_window_starts(duration):
    """Return the 30s window starts (seconds) covering start, middle and end.

    MERIT was trained on 30s clips. Using only the first clip biases every
    vector toward the intro, so we average the head outputs of three windows:
    the first, the middle and the last 30 seconds. Short tracks use one window.
    """

    if duration <= MERIT_WINDOW_SECONDS:
        return [0.0]
    last = duration - MERIT_WINDOW_SECONDS
    return sorted({0.0, round(last / 2, 3), round(last, 3)})


def _normalized_average(vectors):
    import numpy as np

    total = np.sum(np.stack(vectors), axis=0)
    norm = float(np.linalg.norm(total))
    if norm <= 0:
        raise ValueError("MERIT window vectors cancel out")
    return total / norm


def load_embedder(channel, device):
    import librosa
    import numpy as np
    import torch

    if device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS unavailable; choose --device cpu explicitly if intended")
    if channel == "overall":
        from muq import MuQMuLan

        model = MuQMuLan.from_pretrained(
            "OpenMuQ/MuQ-MuLan-large", revision=MUQ_REVISION).to(device).eval()

        def embed(path):
            audio, _ = librosa.load(path, sr=24000, mono=True)
            vectors = []
            with torch.inference_mode():
                for clip in all_clips(audio):
                    vector = model(wavs=torch.tensor(clip).unsqueeze(0).to(device))[0].float().cpu().numpy()
                    vectors.append(vector / np.linalg.norm(vector))
            mean = np.stack(vectors).mean(axis=0)
            return {"": mean / np.linalg.norm(mean)}

    else:
        from huggingface_hub import hf_hub_download
        from transformers import AutoModel, Wav2Vec2FeatureExtractor

        processor = Wav2Vec2FeatureExtractor.from_pretrained(
            "m-a-p/MERT-v1-330M", revision=MERT_REVISION, local_files_only=True)
        model = AutoModel.from_pretrained(
            "m-a-p/MERT-v1-330M", revision=MERT_REVISION,
            trust_remote_code=True, local_files_only=True).to(device).eval()
        model.requires_grad_(False)
        heads = {}
        for factor, folder in (("rhythm", "head_rhy"), ("timbre", "head_tim")):
            checkpoint = torch.load(hf_hub_download(
                "amaai-lab/merit", f"{folder}/best_head.pt", revision=MERIT_REVISION,
                local_files_only=True), map_location="cpu", weights_only=True)
            head = torch.nn.Sequential(
                torch.nn.Linear(checkpoint["in_dim"], checkpoint["hidden_dim"]),
                torch.nn.ReLU(inplace=True),
                torch.nn.Linear(checkpoint["hidden_dim"], checkpoint["out_dim"], bias=False))
            head.load_state_dict({key.removeprefix("net."): value
                                  for key, value in checkpoint["state_dict"].items()})
            heads[factor] = head.to(device).eval().requires_grad_(False)

        def embed(path):
            audio, _ = librosa.load(path, sr=24000, mono=True)
            duration = len(audio) / 24000
            collected = {factor: [] for factor in heads}
            for start in merit_window_starts(duration):
                segment = audio[int(start * 24000):int(start * 24000) + 720_000]
                segment = np.pad(segment, (0, max(0, 720_000 - len(segment))))
                inputs = processor(segment, sampling_rate=24000, return_tensors="pt")
                with torch.inference_mode():
                    output = model(**{key: value.to(device) for key, value in inputs.items()},
                                   output_hidden_states=True)
                    pooled = torch.cat([output.hidden_states[layer].mean(dim=1)
                                        for layer in (3, 4, 5, 6, 23)], dim=-1)
                    for factor, head in heads.items():
                        collected[factor].append(torch.nn.functional.normalize(
                            head(pooled), dim=-1)[0].float().cpu().numpy())
            return {factor: _normalized_average(vectors)
                    for factor, vectors in collected.items()}

    if device == "mps":
        torch.mps.empty_cache()
    return embed


def run(args, embedder=None):
    channels = ["overall"] if args.channel == "overall" else ["rhythm", "timbre"]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / f"receipt-{args.channel}.json"
    identity_map_path = getattr(args, "identity_map", None)
    identity_map = load_identity_map(identity_map_path) if identity_map_path else None
    alternate_sha_by_recording = (
        {recording_id: row["audio_sha256"] for recording_id, row in identity_map.items()}
        if identity_map else None
    )
    config = {"db": str(Path(args.db).resolve()), "audio_root": str(Path(args.audio_root).resolve()),
              "channels": {channel: CHANNELS[channel] for channel in channels}, "device": args.device}
    if identity_map_path:
        config["identity_map"] = str(Path(identity_map_path).resolve())
        config["identity_map_sha256"] = sha256_file(Path(identity_map_path))
    files = {channel: output_dir / f"{channel}.jsonl" for channel in channels}
    if not args.resume and (receipt_path.exists() or any(path.exists() for path in files.values())):
        raise ValueError("Output already exists; use --resume or a fresh output directory")
    if args.resume and receipt_path.exists():
        previous = json.loads(receipt_path.read_text())
        if previous["config"] != config:
            raise ValueError("Resume configuration differs from receipt")
    elif args.resume and any(path.exists() for path in files.values()):
        raise ValueError("Cannot resume JSONL without its recipe receipt")
    print(f"Scanning {args.audio_root} (SHA256 identity matching"
          + (", identity map for alternate renditions" if identity_map else "") + ")", flush=True)
    candidates, failures = scan_audio(args.audio_root, args.db, identity_map)
    with connect_readonly(args.db) as connection:
        identities = dict(connection.execute("SELECT id, audio_sha256 FROM recording"))
        for channel in channels:
            model = CHANNELS[channel]
            registered = connection.execute(
                "SELECT model_name, recipe, dim, modality, metric FROM embedding_model WHERE id=?",
                (model["model_id"],)).fetchone()
            expected = (model["model_name"], model["recipe"], model["dim"], "audio", "cosine")
            if registered is not None and registered != expected:
                raise ValueError(f"Registered model identity differs from runner: {model['model_id']}")
        existing = {channel: {row[0] for row in connection.execute(
            "SELECT recording_id FROM recording_embedding WHERE model_id=? AND factor=?",
            (CHANNELS[channel]["model_id"], CHANNELS[channel]["factor"]))} for channel in channels}
    completed = {
        channel: read_resume(path, CHANNELS[channel]["dim"], identities, alternate_sha_by_recording)
        if args.resume else set()
        for channel, path in files.items()
    }
    receipt = {"config": config, "started_at": datetime.now(timezone.utc).isoformat(),
               "matched_recordings": len(candidates),
               "alternate_candidates": sum(
                   1 for candidate in candidates if candidate.get("asset_kind") == ALTERNATE_ASSET_KIND),
               "inputs": candidates, "failures": failures,
               "model_revisions": {"muq": MUQ_REVISION} if args.channel == "overall" else {
                   "mert": MERT_REVISION, "merit": MERIT_REVISION},
               "skipped_database": {channel: len(existing[channel]) for channel in channels},
               "resumed": {channel: len(completed[channel]) for channel in channels},
               "written": {channel: 0 for channel in channels}, "status": "running"}
    write_json(receipt_path, receipt)
    for path in files.values():
        path.touch(exist_ok=True)
    pending = [candidate for candidate in candidates if any(
        candidate["recording_id"] not in existing[channel] | completed[channel] for channel in channels)]
    try:
        if pending and embedder is None:
            embedder = load_embedder(args.channel, args.device)
        for index, candidate in enumerate(pending, 1):
            try:
                path = Path(candidate["path"])
                digest = sha256_file(path)
                if candidate.get("asset_kind") == ALTERNATE_ASSET_KIND:
                    if digest != candidate["audio_sha256"]:
                        raise ValueError("Audio changed after scan")
                    if (alternate_sha_by_recording or {}).get(candidate["recording_id"]) != digest:
                        raise ValueError("Identity map entry changed after scan")
                elif digest != candidate["audio_sha256"]:
                    raise ValueError("Audio changed after scan")
                vectors = embedder(path)
                encoded = {}
                for channel in channels:
                    if candidate["recording_id"] not in existing[channel] | completed[channel]:
                        encoded[channel] = base64.b64encode(vector_bytes(
                            vectors[CHANNELS[channel]["factor"]], CHANNELS[channel]["dim"])).decode("ascii")
                for channel, payload in encoded.items():
                    entry = {key: candidate[key] for key in ("recording_id", "audio_sha256")}
                    if candidate.get("asset_kind") == ALTERNATE_ASSET_KIND:
                        entry["asset_kind"] = ALTERNATE_ASSET_KIND
                        for key in ("platform_file_hash", "source_url", "served_quality", "title", "artist"):
                            if candidate.get(key) is not None:
                                entry[key] = candidate[key]
                    entry["vector_b64"] = payload
                    with files[channel].open("a", encoding="utf-8", newline="\n") as target:
                        target.write(json.dumps(entry, ensure_ascii=False) + "\n")
                        target.flush()
                        os.fsync(target.fileno())
                    completed[channel].add(candidate["recording_id"])
                    receipt["written"][channel] += 1
            except Exception as error:
                failures.append({**candidate, "error": f"{type(error).__name__}: {error}"})
            write_json(receipt_path, receipt)
            print(f"[{index}/{len(pending)}] {candidate['recording_id']} failures={len(failures)}", flush=True)
        receipt["status"] = "failed" if failures else "complete"
    except Exception as error:
        failures.append({"stage": "model_load", "error": f"{type(error).__name__}: {error}"})
        receipt["status"] = "failed"
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        receipt["output_sha256"] = {channel: sha256_file(path) for channel, path in files.items() if path.exists()}
        write_json(receipt_path, receipt)
    print(json.dumps({"status": receipt["status"], "written": receipt["written"], "failures": failures},
                     ensure_ascii=False), flush=True)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--channel", choices=("overall", "merit"), required=True)
    parser.add_argument("--device", choices=("mps", "cpu"), default="mps")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--identity-map",
        type=Path,
        help=(
            "JSONL rows {recording_id, path, downloaded_sha256, asset_kind=alternate, "
            "platform_file_hash, source_url, served_quality}. Adds alternate-rendition "
            "audio whose bytes differ from the canonical recording.audio_sha256; default "
            "behaviour (no map) is unchanged."
        ),
    )
    args = parser.parse_args(argv)
    try:
        return 0 if run(args)["status"] == "complete" else 1
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
