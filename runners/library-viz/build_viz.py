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
from datetime import datetime
from pathlib import Path

import numpy as np

CHANNELS = (("overall", "整体听感", ""), ("rhythm", "节奏", "rhythm"), ("timbre", "音色", "timbre"))
PALETTE = [
    "#6ea8fe", "#5fd0a0", "#f0a35e", "#c38ef2", "#f2d14e", "#f27e7e",
    "#7ed6e0", "#b0b8c4", "#9fa8da", "#f5a6c8", "#a7d66e", "#e0b0ff",
]
NOISE_COLOR = "#4a505a"
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


def audio_target_name(index: int, suffix: str) -> str:
    return f"{index:04d}{suffix.lower()}"


def load_lyrics(connection: sqlite3.Connection, ids: set[str]) -> dict[str, dict]:
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT recording_id, status, lyric_text FROM recording_lyric "
        f"WHERE recording_id IN ({placeholders})", tuple(sorted(ids))).fetchall()
    return {str(row["recording_id"]): {"status": str(row["status"]),
                                       "text": str(row["lyric_text"] or "")} for row in rows}


AUDIENCE_DIMENSION_LABELS = {
    "age": "年龄",
    "gender": "性别",
    "city_tier": "城市层级",
    "generation_hint": "代际",
    "scene": "场景",
    "tribe": "圈层",
    "province": "省份",
    "life_stage": "人生阶段",
    "creator": "创作者",
    "emotion": "情绪",
}
AUDIENCE_TAG_PREFIXES = ("gen", "scene", "tribe", "age", "gender", "city-tier")


def _chunks(values, size: int = 500):
    ordered = list(values)
    for start in range(0, len(ordered), size):
        yield ordered[start:start + size]


def _audience_bucket_label(bucket: str) -> str:
    """Strip a path-style prefix for display while keeping the stored bucket."""

    text = str(bucket)
    for prefix in AUDIENCE_TAG_PREFIXES:
        marker = f"{prefix}/"
        if text.casefold().startswith(marker.casefold()):
            return text[len(marker):]
    return text


def load_audience(connection: sqlite3.Connection, recording_ids) -> dict[str, dict]:
    """Return the newest research/chart audience picture for the given recordings.

    Pure, read-only helper for the static detail panel: the newest research
    profile per recording (``full_lifecycle`` wins a same-``researched_at`` tie,
    then ``method_version``/``id`` descending) and the newest chart snapshot
    profile, their observations, public sources, coverage notes and the
    projected audience recording tags. Publisher-local paths are deliberately
    not included in the page payload.
    """

    ids = sorted({str(recording_id) for recording_id in recording_ids})
    if not ids:
        return {}

    profiles: dict[str, list[dict]] = {}
    for chunk in _chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT p.id, p.recording_id, p.target_platform, p.time_window,
                   p.method_id, p.method_version, p.status, p.confidence,
                   p.is_proxy, p.researched_at, p.window_start, p.window_end,
                   p.basis_note, p.coverage_note
            FROM audience_profile p
            WHERE p.recording_id IN ({placeholders})
            ORDER BY p.recording_id, p.researched_at DESC,
                     CASE p.time_window WHEN 'full_lifecycle' THEN 0 ELSE 1 END,
                     p.method_version DESC, p.id DESC
            """, tuple(chunk)).fetchall()
        for row in rows:
            profiles.setdefault(str(row["recording_id"]), []).append(dict(row))

    selection: dict[str, dict[str, dict]] = {}
    for recording_id, rows in profiles.items():
        research = [row for row in rows
                    if row["time_window"] in ("first_wave", "full_lifecycle")]
        chart = [row for row in rows
                 if row["method_id"] == "chart_signal_v1"
                 or row["time_window"] == "chart_snapshot"]
        chosen = {}
        if research:
            chosen["research"] = research[0]
        if chart:
            chosen["chart"] = chart[0]
        if chosen:
            selection[recording_id] = chosen

    profile_groups = {
        str(profile["id"]): (recording_id, group)
        for recording_id, groups in selection.items()
        for group, profile in groups.items()
    }
    observations: dict[str, list[dict]] = {profile_id: [] for profile_id in profile_groups}
    for chunk in _chunks(profile_groups):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT profile_id, dimension, bucket, value_low, value_high,
                   value_mid, rank, evidence_level, source_id
            FROM audience_observation
            WHERE profile_id IN ({placeholders})
            ORDER BY profile_id, dimension, bucket, rank
            """, tuple(chunk)).fetchall()
        for row in rows:
            observations.setdefault(str(row["profile_id"]), []).append(dict(row))

    source_ids = sorted({
        str(row["source_id"])
        for rows in observations.values()
        for row in rows
        if row["source_id"] is not None
    })
    sources: dict[str, dict] = {}
    for chunk in _chunks(source_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT id, source_platform, source_key, url, title, source_type,
                   evidence_level, coverage_note
            FROM audience_source
            WHERE id IN ({placeholders})
            ORDER BY source_platform, source_key
            """, tuple(chunk)).fetchall()
        for row in rows:
            item = dict(row)
            item.pop("id", None)
            sources[str(row["id"])] = item

    tags: dict[str, list[dict]] = {recording_id: [] for recording_id in ids}
    for chunk in _chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT rt.recording_id, t.namespace, t.canonical_name, t.path
            FROM recording_tag rt JOIN tag t ON t.id = rt.tag_id
            WHERE rt.recording_id IN ({placeholders})
              AND t.namespace IN ('audience', 'audience-hint')
            ORDER BY rt.recording_id, t.namespace, t.canonical_name
            """, tuple(chunk)).fetchall()
        for row in rows:
            tags.setdefault(str(row["recording_id"]), []).append(
                {
                    "namespace": str(row["namespace"]),
                    "name": str(row["canonical_name"]),
                    "path": str(row["path"]),
                    "key": f"{row['namespace']}/{row['canonical_name']}",
                }
            )

    result: dict[str, dict] = {}
    for recording_id, groups in selection.items():
        entry: dict = {"research": None, "chart": None,
                       "tags": tags.get(recording_id, []),
                       "note": "调研数值是 0–1 推断区间，不是平台报表；榜单证据只代表「进过哪些池子」，不代表受众占比。"}
        for group, profile in groups.items():
            items = []
            group_source_ids: list[str] = []
            for row in observations.get(str(profile["id"]), []):
                items.append(
                    {
                        "dimension": str(row["dimension"]),
                        "label": AUDIENCE_DIMENSION_LABELS.get(
                            str(row["dimension"]), str(row["dimension"])
                        ),
                        "bucket": _audience_bucket_label(str(row["bucket"])),
                        "value_low": row["value_low"],
                        "value_high": row["value_high"],
                        "value_mid": row["value_mid"],
                        "rank": row["rank"],
                        "evidence_level": str(row["evidence_level"]),
                    }
                )
                if row["source_id"] is not None:
                    source_id = str(row["source_id"])
                    if source_id not in group_source_ids:
                        group_source_ids.append(source_id)
            entry[group] = {
                "researched_at": str(profile["researched_at"]),
                "time_window": str(profile["time_window"]),
                "method_id": str(profile["method_id"]),
                "method_version": str(profile["method_version"]),
                "confidence": str(profile["confidence"]),
                "status": str(profile["status"]),
                "basis_note": str(profile["basis_note"] or ""),
                "coverage_note": str(profile["coverage_note"] or ""),
                "items": items,
                "sources": [
                    sources[source_id]
                    for source_id in group_source_ids
                    if source_id in sources
                ],
            }
        result[recording_id] = entry
    return result


def build(database: Path, output_dir: Path, audio_roots: list[Path]) -> dict:
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
        audio_refs: dict[str, str] = {}
        for channel, _, factor in CHANNELS:
            ids, titles, artists, shas, vectors = load_channel(connection, models[channel], factor)
            loaded[channel] = {"ids": ids, "titles": titles, "artists": artists,
                               "shas": shas, "vectors": vectors}
            for recording_id in ids:
                audio_refs.setdefault(recording_id, "")
        needed = {sha for data in loaded.values() for sha in data["shas"] if sha}

    lyrics = load_lyrics(connection, set(audio_refs))
    audience = load_audience(connection, set(audio_refs))
    found = audio_index(needed, audio_roots)
    ordered_ids = sorted(audio_refs)
    sha_by_id = {}
    for data in loaded.values():
        for recording_id, sha in zip(data["ids"], data["shas"]):
            sha_by_id.setdefault(recording_id, sha)
    linked = 0
    for index, recording_id in enumerate(ordered_ids, 1):
        source = found.get(sha_by_id.get(recording_id, ""))
        if source is None:
            audio_refs[recording_id] = ""
            continue
        link_name = audio_target_name(index, source.suffix)
        target = audio_dir / link_name
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(source)
        audio_refs[recording_id] = f"audio/{link_name}"
        linked += 1
    missing = [recording_id for recording_id in ordered_ids if not audio_refs.get(recording_id)]

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
                "full": audio_refs.get(recording_id, ""),
                "lyric": lyrics.get(recording_id, {}).get("text", ""),
                "lyric_status": lyrics.get(recording_id, {}).get("status", "missing"),
                "audience": audience.get(recording_id),
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
    return {"songs": len(ordered_ids), "full_songs": linked, "missing_audio": len(missing),
            "channels": {item["key"]: len(item["songs"]) for item in channels_payload}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, action="append",
                        default=[Path("/Users/wycm/Documents/网易云热榜抓取/music_downloads/KugouMusicClient")])
    args = parser.parse_args()
    print("building library map ...", flush=True)
    summary = build(args.db.expanduser(), args.output_dir.expanduser(), args.audio_root)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
