from __future__ import annotations

import base64
import builtins
import hashlib
import importlib.util
import json
import sqlite3
import struct
import sys
from argparse import Namespace
from contextlib import closing
from pathlib import Path

import pytest
import numpy as np

from music_kb.repository import MusicKBRepository
from music_kb.schema import initialize_database


SCRIPT = Path(__file__).resolve().parents[3] / "runners/audio-embed/embed_audio.py"


@pytest.fixture()
def runner(monkeypatch):
    original_import = builtins.__import__
    forbidden = {"torch", "torchaudio", "transformers", "muq", "merit", "mlx"}
    attempted_model_imports = []

    def model_free_import(name, *args, **kwargs):
        if name.split(".")[0] in forbidden:
            attempted_model_imports.append(name)
            raise AssertionError(f"Pure-logic test attempted a model import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", model_free_import)
    spec = importlib.util.spec_from_file_location("audio_embed_runner_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    yield module
    assert attempted_model_imports == []


@pytest.fixture()
def library(tmp_path):
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    args = Namespace(
        audio_root=audio_root,
        db=database,
        output_dir=tmp_path / "output",
        channel="overall",
        resume=False,
        device="mps",
    )

    def add(recording_id, *, filename=None, content=None):
        content = content if content is not None else (recording_id.encode() + b"\0") * 1_048_576
        path = audio_root / (filename or f"{recording_id}.mp3")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "INSERT INTO recording(id, canonical_title, normalized_title, audio_sha256) "
                "VALUES (?, ?, ?, ?)",
                (recording_id, recording_id, recording_id, digest),
            )
        return path, digest

    return args, add


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def duplicate_library(library):
    args, add = library
    args.db.unlink()
    with sqlite3.connect(args.db) as connection:
        connection.executescript(
            "CREATE TABLE recording(id TEXT PRIMARY KEY, canonical_title TEXT, "
            "normalized_title TEXT, audio_sha256 TEXT);"
            "CREATE TABLE recording_embedding(recording_id TEXT, model_id TEXT, factor TEXT);"
            "CREATE TABLE embedding_model(id TEXT, model_name TEXT, recipe TEXT, "
            "dim INTEGER, modality TEXT, metric TEXT);"
        )
    return args, add


def receipt_file(args):
    return args.output_dir / f"receipt-{args.channel}.json"


def fake_vector(dim):
    return [0.6, -0.8] + [0.0] * (dim - 2)


def test_sha256_known_bytes_and_content_not_filename(runner, tmp_path):
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.wav"
    first.write_bytes(b"abc")
    second.write_bytes(b"abc")
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert runner.sha256_file(first) == expected
    assert runner.sha256_file(second) == expected
    first.write_bytes(b"")
    assert runner.sha256_file(first) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_missing_file_fails(runner, tmp_path):
    with pytest.raises(OSError):
        runner.sha256_file(tmp_path / "missing.mp3")


def test_scan_matches_hash_recursively_and_leaves_database_unchanged(runner, library):
    args, add = library
    path, digest = add("recording", filename="nested/unrelated-name.mp3")
    before = args.db.read_bytes()
    candidates, rejected = runner.scan_audio(args.audio_root, args.db)
    assert [(row["recording_id"], row["audio_sha256"], Path(row["path"])) for row in candidates] == [
        ("recording", digest, path.resolve())
    ]
    assert rejected == []
    assert args.db.read_bytes() == before


def test_scan_excludes_unsupported_empty_and_unmatched_audio(runner, library):
    args, add = library
    good, _ = add("good")
    add("unsupported", filename="notes.txt")
    add("empty", content=b"")
    (args.audio_root / "unknown.mp3").write_bytes(b"unknown" * 262144)
    candidates, rejected = runner.scan_audio(args.audio_root, args.db)
    assert {Path(row["path"]) for row in candidates} == {good}
    assert all(isinstance(row, dict) for row in rejected)
    assert rejected == []


def test_scan_shared_hash_keeps_each_recording_once(runner, duplicate_library):
    args, add = duplicate_library
    content = b"shared audio" * 131072
    _, digest = add("first", content=content)
    add("second", filename="nested/copy.mp3", content=content)
    candidates, _ = runner.scan_audio(args.audio_root, args.db)
    assert sorted(row["recording_id"] for row in candidates) == ["first", "second"]
    assert {row["audio_sha256"] for row in candidates} == {digest}


@pytest.mark.parametrize("channel,factors,dim", [("overall", ("",), 512), ("merit", ("rhythm", "timbre"), 128)])
def test_run_exports_little_endian_float32_without_db_writes(
    runner, library, channel, factors, dim
):
    args, add = library
    path, digest = add("recording")
    args.channel = channel
    calls = []

    def embedder(audio_path):
        assert isinstance(audio_path, Path)
        calls.append(audio_path)
        return {factor: fake_vector(dim) for factor in factors}

    before = args.db.read_bytes()
    receipt = runner.run(args, embedder=embedder)
    assert calls == [path.resolve()]
    assert receipt["status"] == "complete"
    assert receipt["failures"] == []
    assert args.db.read_bytes() == before
    assert json.loads(receipt_file(args).read_text()) == receipt
    for factor in factors:
        rows = read_rows(args.output_dir / f"{factor or 'overall'}.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["recording_id"] == "recording"
        assert row["audio_sha256"] == digest
        vector = base64.b64decode(row["vector_b64"], validate=True)
        assert len(vector) == dim * 4
        assert struct.unpack(f"<{dim}f", vector) == pytest.approx(fake_vector(dim))


def test_resume_does_not_reembed_or_duplicate_completed_rows(runner, library):
    args, add = library
    add("first")
    calls = []

    def embedder(path):
        calls.append(path)
        return {"": fake_vector(512)}

    runner.run(args, embedder=embedder)
    args.resume = True
    runner.run(args, embedder=embedder)
    assert len(calls) == 1
    assert len(read_rows(args.output_dir / "overall.jsonl")) == 1
    second, _ = add("second")
    runner.run(args, embedder=embedder)
    assert calls[-1] == second
    assert len(calls) == 2
    assert {row["recording_id"] for row in read_rows(args.output_dir / "overall.jsonl")} == {"first", "second"}


def test_run_shared_audio_exports_both_recording_identities(runner, duplicate_library):
    args, add = duplicate_library
    content = b"shared audio" * 131072
    _, digest = add("first", content=content)
    add("second", content=content)
    runner.run(args, embedder=lambda path: {"": fake_vector(512)})
    rows = read_rows(args.output_dir / "overall.jsonl")
    assert sorted(row["recording_id"] for row in rows) == ["first", "second"]
    assert {row["audio_sha256"] for row in rows} == {digest}


def test_existing_database_embedding_skips_inference(runner, library):
    args, add = library
    _, digest = add("recording")
    channel = runner.CHANNELS["overall"]
    with MusicKBRepository(args.db) as repository:
        repository.import_embeddings(
            [{"recording_id": "recording", "audio_sha256": digest,
              "vector": struct.pack("<512f", *fake_vector(512))}],
            model_id=channel["model_id"], model_name=channel["model_name"], recipe=channel["recipe"],
            dim=512, weight_license="test-only",
        )

    def unexpected_embedder(path):
        pytest.fail(f"Already embedded recording was processed: {path}")

    before = args.db.read_bytes()
    runner.run(args, embedder=unexpected_embedder)
    args.resume = True
    runner.run(args)
    assert args.db.read_bytes() == before


def test_empty_library_never_loads_model(runner, library):
    args, _ = library
    receipt = runner.run(args)
    assert json.loads(receipt_file(args).read_text()) == receipt


def test_scan_missing_database_does_not_create_it(runner, library):
    args, _ = library
    missing = args.db.with_name("missing.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        runner.scan_audio(args.audio_root, missing)
    assert not missing.exists()


def test_inference_failure_is_recorded_and_other_audio_continues(runner, library):
    args, add = library
    bad, _ = add("bad")
    good, _ = add("good")
    calls = []

    def embedder(path):
        calls.append(path)
        if path == bad:
            raise RuntimeError("synthetic decoder failure")
        return {"": fake_vector(512)}

    receipt = runner.run(args, embedder=embedder)
    assert receipt["status"] == "failed"
    assert len(receipt["failures"]) == 1
    assert set(calls) == {bad, good}
    assert [row["recording_id"] for row in read_rows(args.output_dir / "overall.jsonl")] == ["good"]
    assert "synthetic decoder failure" in json.dumps(receipt)
    assert json.loads(receipt_file(args).read_text()) == receipt
    args.resume = True
    retried = []

    def recovered_embedder(path):
        retried.append(path)
        return {"": fake_vector(512)}

    runner.run(args, embedder=recovered_embedder)
    assert retried == [bad]
    assert sorted(row["recording_id"] for row in read_rows(args.output_dir / "overall.jsonl")) == ["bad", "good"]


@pytest.mark.parametrize("vector", [[1.0], [0.0] * 512, [float("nan")] * 512, [float("inf")] * 512])
def test_invalid_vectors_are_not_exported(runner, library, vector):
    args, add = library
    add("invalid")
    receipt = runner.run(args, embedder=lambda path: {"": vector})
    assert receipt["status"] == "failed"
    assert len(receipt["failures"]) == 1
    output = args.output_dir / "overall.jsonl"
    assert not output.exists() or read_rows(output) == []
    assert receipt_file(args).exists()


def test_all_clips_wraps_tail_from_start_without_dropping_samples(runner):
    audio = np.arange(240_003, dtype=np.float32)
    clips = runner.all_clips(audio)
    assert len(clips) == 2
    np.testing.assert_array_equal(clips[0], audio[:240_000])
    assert len(clips[1]) == 240_000
    np.testing.assert_array_equal(clips[1][:3], audio[-3:])
    np.testing.assert_array_equal(clips[1][3:], audio[:239_997])


def test_all_clips_exact_multiple_has_no_extra_tail(runner):
    audio = np.arange(480_000, dtype=np.float32)
    clips = runner.all_clips(audio)
    assert len(clips) == 2
    np.testing.assert_array_equal(np.concatenate(clips), audio)


def test_all_clips_rejects_audio_shorter_than_ten_seconds(runner):
    with pytest.raises(ValueError):
        runner.all_clips(np.zeros(239_999, dtype=np.float32))


def resume_row():
    return dict(
        recording_id="recording", audio_sha256="a" * 64,
        vector_b64=base64.b64encode(struct.pack("<2f", 0.6, -0.8)).decode("ascii"),
    )


def test_read_resume_truncates_only_unterminated_tail(runner, tmp_path):
    path = tmp_path / "overall.jsonl"
    complete = (json.dumps(resume_row()) + "\n").encode()
    path.write_bytes(complete + b'{"recording_id":')
    assert runner.read_resume(path, 2, {"recording": "a" * 64}) == {"recording"}
    assert path.read_bytes() == complete


@pytest.mark.parametrize("change", [
    {"audio_sha256": "b" * 64},
    {"recording_id": "unknown"},
    {"vector_b64": "not base64!"},
    {"vector_b64": base64.b64encode(struct.pack("<f", 1.0)).decode("ascii")},
    {"vector_b64": base64.b64encode(struct.pack("<2f", 0.0, 0.0)).decode("ascii")},
    {"vector_b64": base64.b64encode(struct.pack("<2f", float("nan"), 1.0)).decode("ascii")},
])
def test_read_resume_rejects_invalid_complete_rows(runner, tmp_path, change):
    path = tmp_path / "overall.jsonl"
    row = resume_row()
    row.update(change)
    original = (json.dumps(row) + "\n").encode()
    path.write_bytes(original)
    with pytest.raises(ValueError):
        runner.read_resume(path, 2, {"recording": "a" * 64})
    assert path.read_bytes() == original


@pytest.mark.parametrize("contents", ["{not-json}\n", "{}\n"])
def test_read_resume_rejects_malformed_complete_rows(runner, tmp_path, contents):
    path = tmp_path / "overall.jsonl"
    path.write_text(contents)
    with pytest.raises((ValueError, KeyError)):
        runner.read_resume(path, 2, {"recording": "a" * 64})
    assert path.read_text() == contents


def test_read_resume_rejects_duplicate_recordings(runner, tmp_path):
    path = tmp_path / "overall.jsonl"
    path.write_text((json.dumps(resume_row()) + "\n") * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        runner.read_resume(path, 2, {"recording": "a" * 64})


def test_model_identity_collision_is_not_silently_skipped(runner, library):
    args, add = library
    add("recording")
    channel = runner.CHANNELS["overall"]
    with MusicKBRepository(args.db) as repository:
        repository.import_embeddings(
            [], model_id=channel["model_id"], model_name="different-model", recipe="different",
            dim=512, weight_license="test-only",
        )
    with pytest.raises(ValueError, match="Registered model identity differs"):
        runner.run(args, embedder=lambda path: pytest.fail("Should not infer"))


def test_resume_rejects_changed_configuration_and_unowned_output(runner, library):
    args, add = library
    add("recording")
    runner.run(args, embedder=lambda path: {"": fake_vector(512)})
    with pytest.raises(ValueError, match="already exists"):
        runner.run(args)
    args.resume = True
    args.device = "cpu"
    with pytest.raises(ValueError, match="configuration differs"):
        runner.run(args)
    receipt_file(args).unlink()
    with pytest.raises(ValueError, match="without its recipe receipt"):
        runner.run(args)


def test_partial_merit_resume_preserves_successful_other_factor(runner, library):
    args, add = library
    add("recording")
    args.channel = "merit"
    runner.run(args, embedder=lambda path: {
        "rhythm": fake_vector(128), "timbre": fake_vector(128),
    })
    rhythm = (args.output_dir / "rhythm.jsonl").read_bytes()
    (args.output_dir / "timbre.jsonl").write_bytes(b'{"torn":')
    args.resume = True
    receipt = runner.run(args, embedder=lambda path: {
        "rhythm": fake_vector(128), "timbre": fake_vector(128),
    })
    assert receipt["written"] == {"rhythm": 0, "timbre": 1}
    assert (args.output_dir / "rhythm.jsonl").read_bytes() == rhythm
    assert len(read_rows(args.output_dir / "timbre.jsonl")) == 1


def test_model_load_failure_receipt_and_nonzero_cli(runner, library, monkeypatch, capsys):
    args, add = library
    add("recording")

    def unavailable(*unused):
        raise RuntimeError("missing offline weights")

    monkeypatch.setattr(runner, "load_embedder", unavailable)
    result = runner.main([
        "--audio-root", str(args.audio_root), "--db", str(args.db),
        "--output-dir", str(args.output_dir), "--channel", "overall",
    ])
    assert result == 1
    receipt = json.loads(receipt_file(args).read_text())
    assert receipt["status"] == "failed"
    assert receipt["failures"][0]["stage"] == "model_load"
    assert "missing offline weights" in capsys.readouterr().out


def test_scan_hash_failure_is_reported(runner, library, monkeypatch):
    args, add = library
    path, _ = add("recording")

    def unreadable(unused):
        raise PermissionError("synthetic unreadable audio")

    monkeypatch.setattr(runner, "sha256_file", unreadable)
    candidates, failures = runner.scan_audio(args.audio_root, args.db)
    assert candidates == []
    assert failures == [{"path": str(path), "error": "synthetic unreadable audio"}]


def test_small_supported_audio_is_embedded(runner, library):
    args, add = library
    path, digest = add("small", filename="small.ogg", content=b"OggS tiny stub")
    candidates, failures = runner.scan_audio(args.audio_root, args.db)
    assert failures == []
    assert candidates == [{"recording_id": "small", "audio_sha256": digest, "path": str(path.resolve())}]
    receipt = runner.run(args, embedder=lambda path: {"": fake_vector(512)})
    assert receipt["written"] == {"overall": 1}


def test_scan_stat_error_is_reported(runner, library, monkeypatch):
    args, add = library
    path, _ = add("unreadable", content=b"small audio")
    original_stat = Path.stat

    def unreadable(candidate, *arguments, **options):
        if candidate == path:
            raise PermissionError("cannot stat audio")
        return original_stat(candidate, *arguments, **options)

    monkeypatch.setattr(Path, "stat", unreadable)
    candidates, failures = runner.scan_audio(args.audio_root, args.db)
    assert candidates == []
    assert failures == [{"path": str(path), "error": "cannot stat audio"}]


def test_scan_directory_error_is_reported(runner, library, monkeypatch):
    args, add = library
    nested = args.audio_root / "denied"
    nested.mkdir()
    original_scandir = runner.os.scandir

    def denied(path):
        if Path(path) == nested:
            raise PermissionError(13, "cannot scan directory", str(path))
        return original_scandir(path)

    monkeypatch.setattr(runner.os, "scandir", denied)
    candidates, failures = runner.scan_audio(args.audio_root, args.db)
    assert candidates == []
    assert len(failures) == 1
    assert failures[0]["path"] == str(nested)
    assert "cannot scan directory" in failures[0]["error"]


def test_resume_null_unknown_identity_rejected(runner, tmp_path):
    path = tmp_path / "overall.jsonl"
    row = resume_row()
    row["audio_sha256"] = None
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError):
        runner.read_resume(path, 2, {})


def test_merit_window_starts_cover_start_middle_end(runner):
    assert runner.merit_window_starts(20) == [0.0]
    assert runner.merit_window_starts(30) == [0.0]
    assert runner.merit_window_starts(90) == [0.0, 30.0, 60.0]
    assert runner.merit_window_starts(300) == [0.0, 135.0, 270.0]


def test_merit_normalized_average_is_unit_vector(runner):
    first = np.array([1.0, 0.0], dtype=np.float32)
    second = np.array([0.0, 1.0], dtype=np.float32)
    averaged = runner._normalized_average([first, second])
    assert abs(float(np.linalg.norm(averaged)) - 1.0) < 1e-6
    assert np.allclose(averaged, [2 ** -0.5, 2 ** -0.5])
