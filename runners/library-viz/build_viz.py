#!/usr/bin/env python3
"""Build a local, read-only 2D map of the publisher library.

Reads the three embedding channels from the publisher master, projects each
channel with UMAP/HDBSCAN, generates audio previews, and writes a static viewer
(data.js + index.html + audio/) outside the repository. The script never opens
the master for writing.

Run with the publisher embedding environment (numpy + umap-learn + hdbscan):
  /path/to/venv-muq/bin/python runners/library-viz/build_viz.py \
    --db "$HOME/.music-kb/music-master.sqlite" \
    --output-dir "/Users/<you>/.../research/music-kb-similarity/viz"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

CHANNELS = (("overall", "整体听感", ""), ("rhythm", "节奏", "rhythm"), ("timbre", "音色", "timbre"))
PALETTE = [
    "#6ea8fe", "#5fd0a0", "#f0a35e", "#c38ef2", "#f2d14e", "#f27e7e",
    "#7ed6e0", "#b0b8c4", "#9fa8da", "#f5a6c8", "#a7d66e", "#e0b0ff",
]
NOISE_COLOR = "#4a505a"
CLIP_SECONDS = 20
CLIP_BITRATE = "96k"
UMAP_SEED = 42


def connect_readonly(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def channel_models(connection: sqlite3.Connection) -> dict[str, str]:
    """Pick the model id currently holding vectors for each channel."""

    models: dict[str, str] = {}
    default = connection.execute(
        "SELECT value FROM meta WHERE key='default_embedding_model'").fetchone()
    default_id = str(default["value"]) if default and str(default["value"]).strip() else ""
    if default_id:
        models["overall"] = default_id
    else:
        row = connection.execute(
            "SELECT model_id FROM recording_embedding WHERE factor='' "
            "GROUP BY model_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if row is None:
            raise SystemExit("no overall embeddings found")
        models["overall"] = str(row["model_id"])
    for channel, _, factor in CHANNELS[1:]:
        row = connection.execute(
            "SELECT model_id FROM recording_embedding WHERE factor=? "
            "GROUP BY model_id ORDER BY COUNT(*) DESC LIMIT 1", (factor,)).fetchone()
        if row is None:
            raise SystemExit(f"no {channel} embeddings found")
        models[channel] = str(row["model_id"])
    return models


def load_channel(connection: sqlite3.Connection, model_id: str, factor: str):
    rows = connection.execute(
        """
        SELECT e.recording_id, e.vector, r.canonical_title, r.audio_sha256,
               (SELECT group_concat(a.canonical_name, '、')
                FROM recording_artist ra JOIN artist a ON a.id = ra.artist_id
                WHERE ra.recording_id = r.id) AS artists
        FROM recording_embedding e
        JOIN recording r ON r.id = e.recording_id
        WHERE e.model_id = ? AND e.factor = ?
        ORDER BY r.canonical_title, e.recording_id
        """, (model_id, factor)).fetchall()
    if not rows:
        raise SystemExit(f"no rows for {model_id}/{factor}")
    ids = [str(row["recording_id"]) for row in rows]
    titles = [str(row["canonical_title"]) for row in rows]
    artists = [[part for part in str(row["artists"] or "").split("、") if part] for row in rows]
    shas = [str(row["audio_sha256"]) if row["audio_sha256"] else "" for row in rows]
    vectors = np.stack([
        np.frombuffer(row["vector"], dtype="<f4").astype(np.float64) for row in rows
    ])
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    return ids, titles, artists, shas, vectors


def top_neighbors(vectors: np.ndarray, count: int = 5) -> list[list[list[float]]]:
    """Return [[[index, distance], ...], ...] in cosine-distance order."""

    similarity = vectors @ vectors.T
    np.fill_diagonal(similarity, -np.inf)
    order = np.argsort(-similarity, axis=1)[:, :count]
    result: list[list[list[float]]] = []
    for row, indices in enumerate(order):
        result.append([[int(index), round(float(1.0 - similarity[row, index]), 4)]
                       for index in indices])
    return result


def normalize_2d(points: np.ndarray) -> np.ndarray:
    centered = points - points.mean(axis=0, keepdims=True)
    scale = float(np.abs(centered).max())
    if scale <= 0:
        return centered
    return centered / scale


def cluster_payload(labels: np.ndarray) -> list[dict]:
    counts: dict[int, int] = {}
    for label in labels:
        counts[int(label)] = counts.get(int(label), 0) + 1
    ordered = sorted((label for label in counts if label >= 0), key=lambda label: -counts[label])
    clusters = []
    for position, label in enumerate(ordered):
        clusters.append({"id": int(label), "name": f"簇 {position + 1}",
                         "size": counts[label], "color": PALETTE[position % len(PALETTE)]})
    if -1 in counts:
        clusters.append({"id": -1, "name": "散点", "size": counts[-1], "color": NOISE_COLOR})
    return clusters


def project(vectors: np.ndarray):
    import hdbscan
    import umap

    embeddings_30d = umap.UMAP(
        n_components=30, n_neighbors=10, min_dist=0.0, metric="cosine",
        random_state=UMAP_SEED).fit_transform(vectors)
    labels = hdbscan.HDBSCAN(
        min_cluster_size=10, cluster_selection_method="leaf").fit_predict(embeddings_30d)
    points_2d = umap.UMAP(
        n_components=2, n_neighbors=20, min_dist=0.15, metric="cosine",
        random_state=UMAP_SEED).fit_transform(vectors)
    return normalize_2d(points_2d), labels


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audio_index(shas: set[str], roots: list[Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for directory, _, names in os.walk(root):
            for name in names:
                path = Path(directory) / name
                if path.suffix.lower() not in {".flac", ".mp3", ".ogg", ".m4a"}:
                    continue
                try:
                    if not path.is_file() or path.stat().st_size <= 0:
                        continue
                    digest = sha256_file(path)
                except OSError:
                    continue
                if digest in shas:
                    found.setdefault(digest, path)
    return found


def clip_start(duration: float) -> float:
    if duration <= CLIP_SECONDS:
        return 0.0
    return min(duration * 0.25, duration - CLIP_SECONDS)


def media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def make_clip(source: Path, target: Path) -> bool:
    total = media_duration(source)
    if total <= 1:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{clip_start(total):.2f}",
         "-t", str(CLIP_SECONDS), "-i", str(source), "-vn", "-ac", "1",
         "-ar", "44100", "-b:a", CLIP_BITRATE, str(target)],
        capture_output=True)
    return result.returncode == 0 and target.is_file() and target.stat().st_size > 5_000


def build(database: Path, output_dir: Path, audio_roots: list[Path], reuse_clips: bool = False) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = output_dir / "audio"
    shutil.rmtree(audio_dir, ignore_errors=True)
    audio_dir.mkdir(parents=True)

    with connect_readonly(database) as connection:
        models = channel_models(connection)
        recipes = {}
        for channel, model_id in models.items():
            row = connection.execute(
                "SELECT recipe FROM embedding_model WHERE id=?", (model_id,)).fetchone()
            recipes[channel] = str(row["recipe"]) if row else ""

        loaded = {}
        clip_names: dict[str, str] = {}
        for channel, _, factor in CHANNELS:
            ids, titles, artists, shas, vectors = load_channel(connection, models[channel], factor)
            loaded[channel] = {"ids": ids, "titles": titles, "artists": artists,
                               "shas": shas, "vectors": vectors}
            for recording_id in ids:
                clip_names.setdefault(recording_id, "")
        needed = {sha for data in loaded.values() for sha in data["shas"] if sha}

    found = audio_index(needed, audio_roots)
    ordered_ids = sorted(clip_names)
    jobs = []
    for index, recording_id in enumerate(ordered_ids, 1):
        clip_name = f"{index:04d}.mp3"
        clip_names[recording_id] = f"audio/{clip_name}"
        info = None
        for data in loaded.values():
            if recording_id in data["ids"]:
                data_index = data["ids"].index(recording_id)
                info = data["shas"][data_index]
                break
        source = found.get(info or "")
        jobs.append((source, audio_dir / clip_name))
    existing_clips = len(list(audio_dir.glob("*.mp3")))
    if reuse_clips and existing_clips >= len(jobs):
        made = existing_clips
        print(f"reusing {existing_clips} audio clips", flush=True)
    else:
        made = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for ok in pool.map(lambda job: make_clip(job[0], job[1]) if job[0] else False, jobs):
                made += int(bool(ok))
    missing = [str(source) for source, _ in jobs if source is None]

    channels_payload = []
    for channel, label, _ in CHANNELS:
        data = loaded[channel]
        vectors = data["vectors"]
        points_2d, labels = project(vectors)
        neighbors = top_neighbors(vectors)
        clusters = cluster_payload(labels)
        songs = []
        for index, recording_id in enumerate(data["ids"]):
            songs.append({
                "id": recording_id,
                "title": data["titles"][index],
                "artists": data["artists"][index],
                "cluster": int(labels[index]),
                "x": round(float(points_2d[index, 0]), 5),
                "y": round(float(points_2d[index, 1]), 5),
                "clip": clip_names.get(recording_id, ""),
                "neighbors": neighbors[index],
            })
        channels_payload.append({
            "key": channel, "label": label, "model_id": models[channel],
            "recipe": recipes[channel], "clusters": clusters, "songs": songs,
        })
        print(f"{channel}: {len(songs)} songs, {len(clusters)} clusters", flush=True)

    data = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "channels": channels_payload,
    }
    temporary = output_dir / "data.js.tmp"
    temporary.write_text(
        "window.VIZ_DATA = " + json.dumps(data, ensure_ascii=False) + ";",
        encoding="utf-8")
    temporary.replace(output_dir / "data.js")

    here = Path(__file__).resolve().parent
    shutil.copyfile(here / "viewer.html", output_dir / "index.html")
    shutil.copyfile(here / "serve.py", output_dir / "serve.py")
    serve = output_dir / "serve.sh"
    serve.write_text("#!/bin/sh\ncd \"$(dirname \"$0\")\" || exit 1\nexec python3 serve.py\n")
    serve.chmod(0o755)
    return {"songs": len(ordered_ids), "clips": made, "missing_clips": len(missing),
            "channels": {item["key"]: len(item["songs"]) for item in channels_payload}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, action="append",
                        default=[Path("/Users/wycm/Documents/网易云热榜抓取/music_downloads/KugouMusicClient")])
    parser.add_argument("--reuse-clips", action="store_true", help="Reuse existing audio in the output dir")
    args = parser.parse_args()
    print("building library map ...", flush=True)
    summary = build(args.db.expanduser(), args.output_dir.expanduser(), args.audio_root,
                    reuse_clips=args.reuse_clips)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
