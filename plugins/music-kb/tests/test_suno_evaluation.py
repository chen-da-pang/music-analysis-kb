import builtins
from argparse import Namespace
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from music_kb.repository import MusicKBRepository


@pytest.fixture()
def evaluator(monkeypatch):
    original_import = builtins.__import__

    def model_free_import(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "muq", "transformers", "torchaudio", "librosa"}:
            pytest.fail(f"Unexpected model/audio import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", model_free_import)
    directory = Path(__file__).resolve().parents[3] / "runners/audio-embed"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("evaluate_suno", directory / "evaluate_suno.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def evaluation_library(evaluator, master_database, fixture_payload, tmp_path):
    with MusicKBRepository(master_database) as repository:
        identities = [(fixture_payload["recording"]["id"], "a", (1, 0))]
        for recording_id, digit, vector in [("other", "b", (0, 1)), ("opposite", "c", (-1, 0))]:
            payload = deepcopy(fixture_payload)
            payload["recording"].update(id=recording_id, title=recording_id, audio_sha256=digit * 64)
            payload["source_tracks"] = []
            repository.import_analysis(payload)
            identities.append((recording_id, digit, vector))
        for config in evaluator.CHANNELS.values():
            entries = [dict(recording_id=recording_id, audio_sha256=digit * 64,
                            vector=evaluator.vector_bytes(list(vector) + [0] * (config["dim"] - 2), config["dim"]))
                       for recording_id, digit, vector in identities]
            repository.import_embeddings(entries, **{key: config[key] for key in
                ("model_id", "model_name", "recipe", "dim", "factor")}, weight_license="test-only")
    audio = tmp_path / "query.wav"
    audio.write_bytes(b"fake audio; never decoded")
    return Namespace(audio=audio, db=master_database, output_dir=tmp_path / "output", simple=False, device="cpu")


@pytest.mark.parametrize("simple", [False, True])
def test_report_contract_readonly_and_modes(evaluator, evaluation_library, simple, capsys, monkeypatch):
    args = evaluation_library
    args.simple = simple
    before = Path(args.db).read_bytes()
    calls = []
    if simple:
        monkeypatch.setattr(evaluator, "pairwise_baseline", lambda *args, **kwargs: pytest.fail("simple must skip baseline"))

    def embedder(group, path):
        calls.append((group, path))
        names = ("overall",) if group == "overall" else ("rhythm", "timbre")
        return {evaluator.CHANNELS[name]["factor"]: [1, 0] + [0] * (evaluator.CHANNELS[name]["dim"] - 2)
                for name in names}

    report = evaluator.run(args, embedder=embedder, duration_reader=lambda path: 20)
    assert json.loads((args.output_dir / "suno-evaluation.json").read_text()) == report
    assert Path(args.db).read_bytes() == before
    assert report["schema_version"] == 1
    assert report["mode"] == ("simple" if simple else "full")
    assert report["query"]["sha256"] == hashlib.sha256(args.audio.read_bytes()).hexdigest()
    assert report["query"]["duration_seconds"] == 20
    assert report["query"]["filename"] == "query.wav"
    assert report["query"]["timestamp"].endswith("+00:00")
    assert report["query"]["channels"] == evaluator.CHANNELS
    assert [group for group, path in calls] == ["overall", "merit"]
    assert set(report["channels"]) == {"overall", "rhythm", "timbre"}
    for result in report["channels"].values():
        assert result["count"] == 3
        assert [row["distance"] for row in result["results"]] == [0, 1, 2]
        first = result["results"][0]
        with MusicKBRepository(args.db, read_only=True) as repository:
            canonical = repository.get_canonical_analysis(first["recording_id"])["analysis"]
        assert first["canonical_analysis"] == {"analysis_id": canonical["id"], "summary": canonical["summary"]}
        assert {"title", "version_label", "artists", "more_versions"} <= first.keys()
        assert ("baseline" in result) == (not simple)
        if not simple:
            baseline = result["baseline"]
            assert baseline["pair_count"] == 3
            assert baseline["mean"] == pytest.approx(4 / 3)
            assert baseline["best_distance_percentile"] == 0
    assert ("historical_context" in report) == (not simple)
    assert "0.000" in capsys.readouterr().out


def test_percentile_known_population_and_empty(evaluator, evaluation_library):
    with MusicKBRepository(evaluation_library.db, read_only=True) as repository:
        options = dict(model_id="muq-mulan-v3", factor="", best_distance=1)
        baseline = evaluator.pairwise_baseline(repository, **options)
        assert baseline["best_distance_percentile"] == pytest.approx(200 / 3)
        assert baseline["quantiles"]["0.5"] == 1
        options["factor"] = "rhythm"
        empty = evaluator.pairwise_baseline(repository, **options)
        assert empty["pair_count"] == 0
        assert empty["mean"] is None
        assert empty["best_distance_percentile"] is None


def test_percentile_uses_unrounded_best_distance(evaluator, evaluation_library):
    report = evaluator.run(evaluation_library, duration_reader=lambda path: 20,
                           embedder=lambda group, path: {
        config["factor"]: [-0.0004, -1] + [0] * (config["dim"] - 2)
        for config in evaluator.CHANNELS.values()
    })
    for channel in report["channels"].values():
        assert channel["results"][0]["distance"] == 1.0
        assert channel["baseline"]["best_distance_unrounded"] < 1
        assert channel["baseline"]["best_distance_percentile"] == 0


def test_reject_output_inside_worktree_before_inference(evaluator, evaluation_library):
    evaluation_library.output_dir = Path(__file__).resolve().parents[3] / "evaluation-test-output"
    with pytest.raises(ValueError, match="outside"):
        evaluator.run(evaluation_library, embedder=lambda *args: pytest.fail("must not infer"))


def test_reject_short_audio(evaluator, evaluation_library):
    with pytest.raises(ValueError, match="10 seconds"):
        evaluator.run(evaluation_library, duration_reader=lambda path: 9)


def test_cli_failure_is_nonzero(evaluator, tmp_path, capsys):
    assert evaluator.main(["--audio", str(tmp_path / "missing"), "--db", "missing",
                           "--output-dir", str(tmp_path)]) == 1
    assert "Evaluation failed" in capsys.readouterr().err


def test_single_recording_baseline_and_no_results(evaluator, evaluation_library):
    args = evaluation_library
    with MusicKBRepository(args.db) as repository:
        with repository.connection:
            repository.connection.execute("DELETE FROM recording_embedding WHERE recording_id IN ('other', 'opposite')")
    with MusicKBRepository(args.db, read_only=True) as repository:
        baseline = evaluator.pairwise_baseline(repository, model_id="muq-mulan-v3", factor="", best_distance=0)
        assert baseline["recording_count"] == 1
        assert baseline["pair_count"] == 0
        assert baseline["best_distance_percentile"] is None
    with MusicKBRepository(args.db) as repository:
        with repository.connection:
            repository.connection.execute("DELETE FROM recording_embedding")
    report = evaluator.run(args, duration_reader=lambda path: 15, embedder=lambda group, path: {
        config["factor"]: [1] + [0] * (config["dim"] - 1) for config in evaluator.CHANNELS.values()
    })
    for channel in report["channels"].values():
        assert channel["results"] == []
        assert channel["baseline"]["best_distance_unrounded"] is None


def test_mismatched_recipe_and_existing_output_fail_before_inference(evaluator, evaluation_library):
    args = evaluation_library
    args.output_dir.mkdir()
    destination = args.output_dir / "suno-evaluation.json"
    destination.write_text("keep")
    with pytest.raises(ValueError, match="already exists"):
        evaluator.run(args)
    assert destination.read_text() == "keep"
    destination.unlink()
    with MusicKBRepository(args.db) as repository:
        with repository.connection:
            repository.connection.execute("UPDATE embedding_model SET recipe='different'")
    with pytest.raises(ValueError, match="identity differs"):
        evaluator.run(args, duration_reader=lambda path: 20,
                      embedder=lambda *args: pytest.fail("must not infer"))


def test_empty_summary_falls_back_to_canonical_raw_text(evaluator, evaluation_library):
    args = evaluation_library
    with MusicKBRepository(args.db) as repository:
        with repository.connection:
            repository.connection.execute("UPDATE analysis_revision SET summary=''")

    def embedder(group, path):
        names = ("overall",) if group == "overall" else ("rhythm", "timbre")
        return {evaluator.CHANNELS[name]["factor"]: [1, 0] + [0] * (evaluator.CHANNELS[name]["dim"] - 2)
                for name in names}

    report = evaluator.run(args, embedder=embedder, duration_reader=lambda path: 20)
    for channel in report["channels"].values():
        analysis = channel["results"][0]["canonical_analysis"]
        assert analysis["summary"] == ""
        assert analysis["raw_text_excerpt"].strip()
