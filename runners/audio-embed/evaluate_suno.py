"""Publisher-local, read-only three-channel evaluation of an external audio file."""

import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import subprocess
import sys

import numpy as np

from embed_audio import CHANNELS, load_embedder, sha256_file, vector_bytes, write_json

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins/music-kb/src"))
from music_kb.repository import MusicKBRepository


def pairwise_baseline(repository, *, model_id, factor, best_distance):
    model = repository.connection.execute(
        "SELECT dim FROM embedding_model WHERE id=?", (model_id,)
    ).fetchone()
    vectors = []
    for row in repository.connection.execute(
        "SELECT vector FROM recording_embedding WHERE model_id=? AND factor=? ORDER BY recording_id",
        (model_id, factor),
    ):
        norm = repository._embedding_norm(row[0], model["dim"])
        vectors.append(np.frombuffer(row[0], dtype="<f4").astype(np.float64) / norm)
    distances = []
    for index, vector in enumerate(vectors):
        distances.extend(float(np.clip(1 - np.dot(vector, other), 0, 2))
                         for other in vectors[index + 1:])
    values = np.asarray(distances, dtype=np.float64)
    return {
        "recording_count": len(vectors), "pair_count": len(distances),
        "population": "all unordered recording pairs, excluding self, before version collapse",
        "mean": float(values.mean()) if values.size else None,
        "quantiles": {str(quantile): float(np.quantile(values, quantile)) if values.size else None
                      for quantile in (0, 0.25, 0.5, 0.75, 1)},
        "best_distance_unrounded": best_distance,
        "best_distance_percentile": float(100 * np.count_nonzero(values <= best_distance) / values.size)
        if values.size and best_distance is not None else None,
        "percentile_definition": "100 * count(pair distance <= unrounded best distance) / pair_count; lower is closer",
    }


def audio_duration(path):
    import librosa

    return float(librosa.get_duration(path=path))


def analysis_brief(row):
    """One-line brief for the terminal: stored summary, else canonical text excerpt."""

    analysis = row["canonical_analysis"]
    text = analysis.get("summary") or analysis.get("raw_text_excerpt") or ""
    return " ".join(text.split())[:120]


def run(args, *, embedder=None, duration_reader=audio_duration):
    audio = Path(args.audio).resolve(strict=True)
    output_dir = Path(args.output_dir).resolve()
    root = Path(__file__).resolve().parents[2]
    worktrees = subprocess.check_output(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True
    )
    roots = [Path(line.removeprefix("worktree ")).resolve() for line in worktrees.splitlines()
             if line.startswith("worktree ")]
    if any(output_dir.is_relative_to(path) for path in roots):
        raise ValueError("Evaluation output must be outside repository worktrees")
    destination = output_dir / "suno-evaluation.json"
    if destination.exists():
        raise ValueError("Output already exists; choose a fresh output directory")
    duration = duration_reader(audio)
    if not math.isfinite(duration) or duration < 10:
        raise ValueError("MuQ v3 requires at least 10 seconds of audio")
    report = {
        "schema_version": 1, "mode": "simple" if args.simple else "full",
        "query": {"filename": audio.name, "duration_seconds": duration,
                  "sha256": sha256_file(audio),
                  "timestamp": datetime.now(timezone.utc).isoformat(),
                  "channels": CHANNELS},
        "channels": {},
    }
    with MusicKBRepository(args.db, read_only=True) as repository:
        repository.connection.execute("BEGIN")
        for config in CHANNELS.values():
            registered = repository.connection.execute(
                "SELECT * FROM embedding_model WHERE id=?", (config["model_id"],)
            ).fetchone()
            if registered is None or any(registered[key] != config[key]
                                         for key in ("model_name", "recipe", "dim")):
                raise ValueError(f"Registered model identity differs: {config['model_id']}")
        for group, names in (("overall", ("overall",)), ("merit", ("rhythm", "timbre"))):
            active_embedder = embedder if embedder is not None else load_embedder(group, args.device)
            vectors = active_embedder(group, audio) if embedder is not None else active_embedder(audio)
            for name in names:
                config = CHANNELS[name]
                payload = vector_bytes(vectors[config["factor"]], config["dim"])
                result = repository.similar_to_vector(
                    payload, model_id=config["model_id"], factor=config["factor"], limit=10)
                for row in result["results"]:
                    canonical = repository.get_canonical_analysis(
                        row["recording_id"], max_chars=600)["analysis"]
                    row["canonical_analysis"] = {
                        "analysis_id": canonical["id"], "summary": canonical["summary"]}
                    if not canonical["summary"]:
                        # Stored summaries can be empty on campaign imports; the
                        # canonical raw text is the only available brief.
                        row["canonical_analysis"]["raw_text_excerpt"] = canonical["raw_text"]
                if not args.simple:
                    best = None
                    if result["results"]:
                        winner = repository.connection.execute(
                            "SELECT vector FROM recording_embedding WHERE recording_id=? AND model_id=? AND factor=?",
                            (result["results"][0]["recording_id"], config["model_id"], config["factor"]),
                        ).fetchone()[0]
                        query = np.frombuffer(payload, dtype="<f4").astype(np.float64)
                        target = np.frombuffer(winner, dtype="<f4").astype(np.float64)
                        best = float(np.clip(1 - np.dot(query / np.linalg.norm(query),
                                                     target / np.linalg.norm(target)), 0, 2))
                    result["baseline"] = pairwise_baseline(
                        repository, model_id=config["model_id"], factor=config["factor"], best_distance=best)
                report["channels"][name] = result
            del active_embedder
    if not args.simple:
        report["historical_context"] = {
            "channel": "overall", "multi_version_pair_mean": 0.0714,
            "all_pair_mean_approx": 0.3033,
            "note": "Historical MuQ v3 context only, not thresholds or calibrated MERIT baselines",
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(destination, report)
    for name, result in report["channels"].items():
        print(f"{name}: {result['count']} results (cosine distance; lower is closer)")
        for row in result["results"]:
            print(f"  {row['distance']:.3f} {row['title']} [{row['version_label']}] "
                  f"(+{row['more_versions']} versions): {analysis_brief(row)}")
        if "baseline" in result:
            print(f"  Best-distance empirical percentile: {result['baseline']['best_distance_percentile']}")
    print(f"Report: {destination}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--simple", action="store_true")
    parser.add_argument("--device", choices=("mps", "cpu"), default="mps")
    args = parser.parse_args(argv)
    try:
        run(args)
    except Exception as error:
        print(f"Evaluation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
