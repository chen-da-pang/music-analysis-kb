from __future__ import annotations

import importlib.util
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "cnb_campaign_repository.py"
SPEC = importlib.util.spec_from_file_location("cnb_campaign_repository", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


RUNTIME = "docker.cnb.cool/org/runner@sha256:" + "a" * 64
OPERATIONS = Path(__file__).parents[1] / "references" / "validated-operations.json"


def fake_export_runtime(output: Path, commit: str, *, omit: str | None = None) -> dict:
    output.mkdir(parents=True)
    files = []
    for relative in MODULE.REQUIRED_CAMPAIGN_RUNTIME_FILES:
        if relative == omit:
            continue
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"fixture:{relative}\n".encode()
        path.write_bytes(payload)
        files.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": __import__("hashlib").sha256(payload).hexdigest(),
            }
        )
    provenance = {"source_commit": commit, "files": files}
    (output / ".github-source.json").write_text(json.dumps(provenance), encoding="utf-8")
    return provenance


def policy() -> dict:
    return {
        "schema_version": 2,
        "organization_slug": "org",
        "repository_slug": "org/moss",
        "protected_runtime_repository_slug": "org/moss",
        "campaign_repository_prefix": "music-flamingo-campaign-",
        "verified_runtime_image_digest": RUNTIME,
        "required_runtime": {"tag": "runtime"},
        "minimum_group_object_free_bytes": 100,
        "minimum_group_git_free_bytes": 100,
        "campaign_repository": {
            "visibility": "private",
            "branch": "main",
            "event": "api_trigger_music_flamingo_campaign",
            "transport": "git-objects",
            "shard_count": 2,
            "runner_tag": "cnb:arch:amd64:gpu:L40",
            "ledger_branch_template": "campaign-results/{campaign_id}",
            "runtime_image": RUNTIME,
            "max_new_tokens": 2048,
            "audio_clip_seconds": 240,
            "max_git_object_bytes": 5_000,
            "max_git_object_file_bytes": 2_000,
        },
    }


def cnb_runner_factory(*, target_present: bool = False, existing: list[str] | None = None):
    commands: list[list[str]] = []
    state = {"target_present": target_present, "group_object": 1_000}
    existing = existing or []

    def run(command):
        commands.append(list(command))
        joined = " ".join(command)
        if "repositories get-by-id" in joined:
            repo = command[command.index("--repo") + 1]
            if repo == "org/moss" or (repo == "org/music-flamingo-campaign-run-1" and state["target_present"]):
                return {"status": 200, "data": {"path": repo}}
            return {"status": 404, "data": {"errcode": 5}}
        if "get-group-sub-repos" in joined:
            return {"status": 200, "data": {"list": [{"path": value} for value in existing]}}
        if "list-branches" in joined:
            return {"status": 200, "data": [{"name": "main"}]}
        if "get-package-tag-detail" in joined:
            return {"status": 200, "data": {"docker": {"image": {"digest": "sha256:" + "a" * 64}}}}
        if "get-volume" in joined:
            return {"status": 200, "data": {"object_in_byte": state["group_object"], "git_in_byte": 1_000}}
        if "get-quota" in joined:
            return {"status": 200, "data": {"object_in_byte": {"total": 10_000}, "git_in_byte": {"total": 10_000}}}
        if "get-repos-volume" in joined:
            return {"status": 200, "data": [{"slug": "org/music-flamingo-campaign-run-1", "volume": "0"}]}
        if "list-workspaces" in joined:
            return {"status": 200, "data": {"list": []}}
        if "delete-repo" in joined:
            state["target_present"] = False
            return {"status": 200, "data": {"deleted": True}}
        if "create-repo" in joined:
            state["target_present"] = True
            return {"status": 200, "data": {"path": "org/music-flamingo-campaign-run-1"}}
        if "start-build" in joined:
            return {"status": 200, "data": {"sn": "cnb-demo-1"}}
        if "start-workspace" in joined:
            return {"status": 200, "data": {"sn": "cnb-workspace-1"}}
        if "workspace-stop" in joined:
            return {"status": 200, "data": {"stopped": True}}
        if "get-build-status" in joined:
            return {"status": 200, "data": {"status": "success", "pipelinesStatus": {}}}
        raise AssertionError(command)

    return state, commands, run


def test_run_cnb_rejects_zero_exit_api_authorization_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MODULE,
        "_run_json",
        lambda _command: {
            "status": 403,
            "data": {
                "errcode": 10023,
                "errmsg": "The token's authorization scope does not match this request. Missing required scopes: repo-delete:rw",
            },
        },
    )

    with pytest.raises(MODULE.CampaignRepositoryError, match="status 403.*repo-delete:rw"):
        MODULE.run_cnb(["cnb", "repositories", "delete-repo"])


def test_cnb_optional_still_converts_api_404_to_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MODULE,
        "_run_json",
        lambda _command: {"status": 404, "data": {"errcode": 5, "errmsg": "not found"}},
    )

    response, absent = MODULE._cnb_optional(
        ["cnb", "repositories", "get-by-id"], MODULE.run_cnb
    )

    assert response is None
    assert absent is True


def receipt_identity(tmp_path: Path, *, run_id: str = "run-1", count: int = 1) -> dict:
    repository = f"org/music-flamingo-campaign-{run_id}"
    source_manifest = tmp_path / f"{run_id}-source-manifest.jsonl"
    source_manifest.write_text("{}\n" * count, encoding="utf-8")
    return {
        "schema_version": 1,
        "atom": "cnb_campaign_repository",
        "status": "created_and_pushed",
        "run_id": run_id,
        "repository": repository,
        "repository_name": repository.split("/", 1)[1],
        "organization": "org",
        "repository_prefix": "music-flamingo-campaign-",
        "github_repository": "chen-da-pang/music-analysis-kb",
        "operations_sha256": MODULE.sha256_file(OPERATIONS),
        "github_commit": "a" * 40,
        "runtime_image": RUNTIME,
        "runtime_digest": "sha256:" + "a" * 64,
        "transport": "git-objects",
        "manifest": {
            "path": str(source_manifest),
            "sha256": MODULE.sha256_file(source_manifest),
            "item_count": count,
            "source_bytes": count * 4,
            "source_links": count,
            "campaign_id": run_id,
        },
        "runtime_export": {
            "validated": True,
            "required_files": [
                {
                    "path": value,
                    "bytes": len(f"fixture:{value}\n".encode()),
                    "sha256": __import__("hashlib").sha256(f"fixture:{value}\n".encode()).hexdigest(),
                }
                for value in MODULE.REQUIRED_CAMPAIGN_RUNTIME_FILES
            ],
        },
        "campaign_repository_config": str(tmp_path / "repo" / ".cnb.yml"),
        "workspace": str(tmp_path / "workspace"),
        "checkout": str(tmp_path / "repo"),
        "repository_created": True,
        "repository_pushed": True,
        "builds": [],
        "delivery": None,
    }


def completed_receipt(tmp_path: Path, *, count: int = 2) -> dict:
    receipt = receipt_identity(tmp_path, count=count)
    receipt["status"] = "completed"
    receipt["builds"] = [
        {"index": index, "id": f"run-1-s{index}", "sn": f"build-{index}", "status": "success"}
        for index in (1, 2)
    ]
    delivery = tmp_path / "canonical.jsonl"
    delivery.write_text("{}\n" * count, encoding="utf-8")
    receipt["delivery"] = {
        "path": str(delivery),
        "sha256": MODULE.sha256_file(delivery),
        "count": count,
        "ledger_branch": "campaign-results/run-1",
        "state": str(tmp_path / "state.json"),
    }
    return receipt


def external_delivery_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_url: str = "https://example.test/song",
) -> tuple[Path, Path, Path]:
    """Build one legacy receipt, an external delivery, and released provenance."""

    source_sha256 = "b" * 64
    output_text = "A verified Pop recording."
    delivery_row = {
        "schema_version": 1,
        "campaign_id": "run-1",
        "id": "song-1",
        "manifest_index": 1,
        "title": "Song",
        "artist": "Artist",
        "relative_audio_path": "audio/song-1.mp3",
        "source_sha256": source_sha256,
        "source_bytes": 123,
        "source_url": source_url,
        "output_text": output_text,
        "output_text_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        "generated_token_count": 12,
        "max_new_tokens": 2048,
        "contract": "contract-1",
        "attempt_id": "attempt-1",
        "canonical_source": "campaign",
    }
    source_manifest = tmp_path / "source-manifest.jsonl"
    source_manifest.write_text(
        json.dumps(
            {
                "id": delivery_row["id"],
                "relative_audio_path": delivery_row["relative_audio_path"],
                "source_bytes": delivery_row["source_bytes"],
                "sha256": delivery_row["source_sha256"],
                "title": delivery_row["title"],
                "artist": delivery_row["artist"],
                "campaign_id": delivery_row["campaign_id"],
                "source_url": delivery_row["source_url"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = receipt_identity(tmp_path, count=1)
    receipt.update(
        {
            "status": "failed",
            "manifest": {
                "path": str(source_manifest),
                "sha256": MODULE.sha256_file(source_manifest),
                "item_count": 1,
                "source_bytes": 123,
                "source_links": 1,
                "campaign_id": "run-1",
            },
        }
    )
    receipt_path = tmp_path / "legacy-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    delivery_path = tmp_path / "external-delivery.jsonl"
    delivery_path.write_text(
        json.dumps(delivery_row, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    release_manifest = tmp_path / "manifest.json"
    release_manifest.write_text("{}\n", encoding="utf-8")
    database = tmp_path / "release.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE analysis_revision (id TEXT PRIMARY KEY, recording_id TEXT NOT NULL);
            CREATE TABLE source_track (
                recording_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_track_id TEXT NOT NULL,
                source_url TEXT
            );
            CREATE TABLE campaign_delivery_provenance (
                campaign_id TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                analysis_id TEXT NOT NULL,
                manifest_index INTEGER NOT NULL,
                source_title TEXT NOT NULL,
                source_artist TEXT NOT NULL,
                relative_audio_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_bytes INTEGER NOT NULL,
                output_text_sha256 TEXT NOT NULL,
                generated_token_count INTEGER NOT NULL,
                max_new_tokens INTEGER NOT NULL,
                contract TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                canonical_source TEXT NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO analysis_revision VALUES (?, ?)", ("analysis-1", "recording-1"))
        connection.execute(
            "INSERT INTO source_track VALUES (?, ?, ?, ?)",
            ("recording-1", "kugou", delivery_row["id"], delivery_row["source_url"]),
        )
        connection.execute(
            "INSERT INTO campaign_delivery_provenance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                delivery_row["campaign_id"],
                delivery_row["id"],
                "analysis-1",
                delivery_row["manifest_index"],
                delivery_row["title"],
                delivery_row["artist"],
                delivery_row["relative_audio_path"],
                delivery_row["source_sha256"],
                delivery_row["source_bytes"],
                delivery_row["output_text_sha256"],
                delivery_row["generated_token_count"],
                delivery_row["max_new_tokens"],
                delivery_row["contract"],
                delivery_row["attempt_id"],
                delivery_row["canonical_source"],
            ),
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(
        MODULE,
        "verify_snapshot",
        lambda _path: {
            "valid": True,
            "manifest": str(release_manifest),
            "database": str(database),
            "release_name": "fixture-release",
            "sha256": MODULE.sha256_file(database),
        },
    )
    return receipt_path, delivery_path, release_manifest


def test_run_id_and_repository_name_are_strict() -> None:
    value = policy()
    assert MODULE.campaign_repository_name(value, "weekly-20260720") == "music-flamingo-campaign-weekly-20260720"
    for unsafe in ("../escape", "UPPER", "main", "music-flamingo-campaign-old", "with space"):
        with pytest.raises(MODULE.CampaignRepositoryError):
            MODULE.campaign_repository_name(value, unsafe)


def test_policy_and_config_pin_one_immutable_runtime() -> None:
    value = policy()
    loaded = MODULE.load_campaign_policy_from_mapping(value) if hasattr(MODULE, "load_campaign_policy_from_mapping") else None
    assert loaded is None  # mapping validation is intentionally exercised through config below
    text = MODULE.generate_campaign_config(
        value,
        campaign_id="weekly-20260720",
        repository_slug="org/music-flamingo-campaign-weekly-20260720",
        item_count=2,
        source_manifest_sha256="b" * 64,
    )
    assert RUNTIME in text
    assert "api_trigger_music_flamingo_campaign:" in text
    assert "scripts/run_music_flamingo_campaign.sh" in text
    assert "MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT: '2'" in text


def test_manifest_rejects_hash_or_path_mismatch(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    (staging / "audio").mkdir(parents=True)
    (staging / "audio" / "song.flac").write_bytes(b"audio")
    row = {
        "id": "kugou-1",
        "relative_audio_path": "audio/song.flac",
        "source_bytes": 5,
        "sha256": "0" * 64,
        "title": "Song",
        "artist": "Artist",
        "campaign_id": "run-1",
    }
    (staging / "manifest.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(MODULE.CampaignRepositoryError, match="sha256"):
        MODULE._read_manifest(staging, expected_count=1)
    row["sha256"] = MODULE.sha256_file(staging / "audio" / "song.flac")
    row["relative_audio_path"] = "../escape.flac"
    (staging / "manifest.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(MODULE.CampaignRepositoryError, match="unsafe"):
        MODULE._read_manifest(staging, expected_count=1)


def test_preflight_blocks_existing_campaign_repository_and_target() -> None:
    value = policy()
    _, _, runner = cnb_runner_factory(existing=["org/music-flamingo-campaign-old"])
    result = MODULE.campaign_preflight(value, runner=runner, estimated_bytes=10)
    assert result["clean"] is False
    assert result["checks"]["no_existing_campaign_repositories"] is False
    _, _, target_runner = cnb_runner_factory(target_present=True)
    target = MODULE.campaign_preflight(
        value,
        runner=target_runner,
        estimated_bytes=10,
        target_repository="org/music-flamingo-campaign-run-1",
    )
    assert target["checks"]["target_repository_absent"] is False
    with pytest.raises(MODULE.CampaignRepositoryError, match="exact campaign slug"):
        MODULE.campaign_preflight(
            value,
            runner=runner,
            resume_repository="org/music-flamingo-campaign-../escape",
        )


def test_git_objects_preflight_does_not_require_object_headroom() -> None:
    value = policy()
    state, _, runner = cnb_runner_factory()
    state["group_object"] = 10_000  # object quota is intentionally exhausted below

    def quota_runner(command):
        response = runner(command)
        if "get-quota" in " ".join(command):
            response["data"]["object_in_byte"]["total"] = 10_000
            response["data"]["git_in_byte"]["total"] = 20_000
        return response

    result = MODULE.campaign_preflight(value, runner=quota_runner, estimated_bytes=100)
    assert result["transport"] == "git-objects"
    assert result["clean"] is True
    assert "object_headroom" not in result["checks"]
    assert result["checks"]["git_headroom_for_transport"] is True


def test_lfs_preflight_keeps_object_headroom_gate() -> None:
    value = policy()
    value["campaign_repository"]["transport"] = "lfs"
    state, _, runner = cnb_runner_factory()
    state["group_object"] = 10_000

    def quota_runner(command):
        response = runner(command)
        if "get-quota" in " ".join(command):
            response["data"]["object_in_byte"]["total"] = 10_000
        return response

    result = MODULE.campaign_preflight(value, runner=quota_runner, estimated_bytes=100)
    assert result["clean"] is False
    assert result["checks"]["object_headroom"] is False


def test_prepare_dry_run_writes_receipt_without_create_or_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    staging = tmp_path / "staging"
    (staging / "audio").mkdir(parents=True)
    payload = b"audio"
    (staging / "audio" / "song.flac").write_bytes(payload)
    (staging / "manifest.jsonl").write_text(
        json.dumps(
            {
                "id": "kugou-1",
                "relative_audio_path": "audio/song.flac",
                "source_bytes": len(payload),
                "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                "title": "Song",
                "artist": "Artist",
                "campaign_id": "run-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "github"
    (root / "runners" / "cnb-music-flamingo" / "tools").mkdir(parents=True)
    monkeypatch.setattr(MODULE, "_validate_commit", lambda _root, commit, **_kwargs: commit)
    monkeypatch.setattr(
        MODULE,
        "_export_runtime",
        lambda _root, _commit, output, **_kwargs: fake_export_runtime(output, _commit),
    )
    _, commands, runner = cnb_runner_factory()
    receipt = MODULE.prepare_campaign_repository(
        policy_path=policy_path,
        operations_path=operations_path,
        repository_root=root,
        run_id="run-1",
        staging=staging,
        run_dir=tmp_path / "run",
        github_commit="a" * 40,
        expected_count=1,
        execute=False,
        runner=runner,
    )
    assert receipt["status"] == "planned"
    assert receipt["operations_sha256"] == MODULE.sha256_file(operations_path)
    assert receipt["repository_created"] is False
    assert receipt["runtime_export"]["validated"] is True
    assert len(receipt["runtime_export"]["required_files"]) == len(MODULE.REQUIRED_CAMPAIGN_RUNTIME_FILES)
    assert Path(receipt["campaign_repository_config"]).is_file()
    assert Path(tmp_path / "run" / "cnb" / "campaign-receipt.json").is_file()
    assert not any("create-repo" in " ".join(command) for command in commands)


def test_prepare_push_failure_resumes_same_receipt_and_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    staging = tmp_path / "staging"
    (staging / "audio").mkdir(parents=True)
    payload = b"audio"
    (staging / "audio" / "song.flac").write_bytes(payload)
    (staging / "manifest.jsonl").write_text(
        json.dumps(
            {
                "id": "kugou-1",
                "relative_audio_path": "audio/song.flac",
                "source_bytes": len(payload),
                "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                "title": "Song",
                "artist": "Artist",
                "campaign_id": "run-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "github"
    root.mkdir()
    monkeypatch.setattr(MODULE, "_validate_commit", lambda _root, commit, **_kwargs: commit)
    monkeypatch.setattr(
        MODULE,
        "_export_runtime",
        lambda _root, _commit, output, **_kwargs: fake_export_runtime(output, _commit),
    )
    monkeypatch.setattr(MODULE, "_git_push_environment", lambda: ({}, None))
    push_attempts = {"count": 0}

    def fake_push(command, *, cwd, env):
        push_attempts["count"] += 1
        if push_attempts["count"] == 1:
            raise MODULE.CampaignRepositoryError("simulated push failure")

    monkeypatch.setattr(MODULE, "_run_git_authenticated", fake_push)
    state, commands, runner = cnb_runner_factory()
    first = True
    with pytest.raises(MODULE.CampaignRepositoryError, match="simulated push failure"):
        MODULE.prepare_campaign_repository(
            policy_path=policy_path,
            operations_path=operations_path,
            repository_root=root,
            run_id="run-1",
            staging=staging,
            run_dir=tmp_path / "run",
            github_commit="a" * 40,
            expected_count=1,
            execute=True,
            runner=runner,
        )
    saved = json.loads((tmp_path / "run" / "cnb" / "campaign-receipt.json").read_text(encoding="utf-8"))
    assert saved["repository_created"] is True
    assert saved["repository_pushed"] is False
    assert saved["failure"]["phase"] == "create_or_push"
    assert state["target_present"] is True

    resumed = MODULE.prepare_campaign_repository(
        policy_path=policy_path,
        operations_path=operations_path,
        repository_root=root,
        run_id="run-1",
        staging=staging,
        run_dir=tmp_path / "run",
        github_commit="a" * 40,
        expected_count=1,
        execute=True,
        runner=runner,
    )
    assert resumed["status"] == "created_and_pushed"
    assert resumed["repository_created"] is True
    assert resumed["repository_pushed"] is True
    assert push_attempts["count"] == 2
    assert sum("create-repo" in " ".join(command) for command in commands) == 1


def test_allow_unpublished_is_dry_run_only(tmp_path: Path) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    with pytest.raises(MODULE.CampaignRepositoryError, match="only for a non-executing dry-run"):
        MODULE.prepare_campaign_repository(
            policy_path=policy_path,
            operations_path=operations_path,
            repository_root=tmp_path,
            run_id="run-1",
            staging=tmp_path / "missing-staging",
            run_dir=tmp_path / "run",
            github_commit="a" * 40,
            execute=True,
            allow_unpublished=True,
        )


def test_prepare_rejects_export_missing_required_campaign_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    staging = tmp_path / "staging"
    (staging / "audio").mkdir(parents=True)
    payload = b"audio"
    (staging / "audio" / "song.flac").write_bytes(payload)
    (staging / "manifest.jsonl").write_text(
        json.dumps(
            {
                "id": "kugou-1",
                "relative_audio_path": "audio/song.flac",
                "source_bytes": len(payload),
                "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                "title": "Song",
                "artist": "Artist",
                "campaign_id": "run-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "github"
    root.mkdir()
    missing = "scripts/build_kugou_canonical_delivery.py"
    monkeypatch.setattr(MODULE, "_validate_commit", lambda _root, commit, **_kwargs: commit)
    monkeypatch.setattr(
        MODULE,
        "_export_runtime",
        lambda _root, _commit, output, **_kwargs: fake_export_runtime(output, _commit, omit=missing),
    )
    _, _, runner = cnb_runner_factory()
    with pytest.raises(MODULE.CampaignRepositoryError, match="missing required campaign scripts"):
        MODULE.prepare_campaign_repository(
            policy_path=policy_path,
            operations_path=operations_path,
            repository_root=root,
            run_id="run-1",
            staging=staging,
            run_dir=tmp_path / "run",
            github_commit="a" * 40,
            expected_count=1,
            execute=False,
            runner=runner,
        )
    receipt_path = tmp_path / "run" / "cnb" / "campaign-receipt.json"
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["failure"]["phase"] == "export_or_stage"


def test_submit_failure_keeps_same_receipt_and_does_not_delete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
                receipt_identity(tmp_path, count=1)
        ),
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".cnb.yml").write_text(
        MODULE.generate_campaign_config(
            value,
            campaign_id="run-1",
            repository_slug="org/music-flamingo-campaign-run-1",
            item_count=1,
            source_manifest_sha256="b" * 64,
        ),
        encoding="utf-8",
    )
    _, commands, runner = cnb_runner_factory(target_present=True)
    monkeypatch.setattr(MODULE, "_recover_delivery", lambda *args, **kwargs: (_ for _ in ()).throw(MODULE.CampaignRepositoryError("ledger incomplete")))
    with pytest.raises(MODULE.CampaignRepositoryError, match="ledger incomplete"):
        MODULE.submit_campaign(
            policy_path=policy_path,
            operations_path=operations_path,
            receipt_path=receipt_path,
            run_dir=tmp_path,
            execute=True,
            wait=True,
            poll_seconds=0,
            timeout_seconds=2,
            runner=runner,
            transport="git-objects",
        )
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["failure"]["phase"] == "submit_or_recover"
    assert not any("delete-repo" in " ".join(command) for command in commands)


def test_submit_resume_reuses_existing_shard_sn_without_duplicate_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
                {
                    **receipt_identity(tmp_path, count=2),
                    "status": "failed",
                    "builds": [
                    {
                        "index": 1,
                        "id": "run-1-s1",
                        "sn": "cnb-existing-1",
                        "status": "success",
                        "env": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".cnb.yml").write_text(
        MODULE.generate_campaign_config(
            value,
            campaign_id="run-1",
            repository_slug="org/music-flamingo-campaign-run-1",
            item_count=2,
            source_manifest_sha256="b" * 64,
        ),
        encoding="utf-8",
    )
    _, commands, runner = cnb_runner_factory(target_present=True)
    monkeypatch.setattr(
        MODULE,
        "_recover_delivery",
        lambda *args, **kwargs: {
            "path": str(tmp_path / "canonical.jsonl"),
            "count": 2,
            "sha256": "b" * 64,
        },
    )
    result = MODULE.submit_campaign(
        policy_path=policy_path,
        operations_path=operations_path,
        receipt_path=receipt_path,
        run_dir=tmp_path,
        execute=True,
        wait=True,
        poll_seconds=0,
        timeout_seconds=2,
        runner=runner,
        transport="git-objects",
    )
    starts = [command for command in commands if "start-build" in " ".join(command)]
    assert len(starts) == 1
    assert "--config" in starts[0]
    assert "--data" not in starts[0]
    assert result["status"] == "completed"
    assert [item["index"] for item in result["builds"]] == [1, 2]


def test_submit_retries_failed_shard_without_changing_repository_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    receipt = receipt_identity(tmp_path, count=2)
    receipt.update(
        {
            "status": "failed",
            "builds": [
                {"index": 1, "id": "run-1-s1", "sn": "old-1", "status": "success"},
                {"index": 2, "id": "run-1-s2", "sn": "old-2", "status": "failed"},
            ],
        }
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".cnb.yml").write_text(
        MODULE.generate_campaign_config(
            value,
            campaign_id="run-1",
            repository_slug="org/music-flamingo-campaign-run-1",
            item_count=2,
            source_manifest_sha256="b" * 64,
        ),
        encoding="utf-8",
    )
    _, commands, runner = cnb_runner_factory(target_present=True)
    monkeypatch.setattr(
        MODULE,
        "_recover_delivery",
        lambda *args, **kwargs: {"path": str(tmp_path / "canonical.jsonl"), "count": 2, "sha256": "b" * 64},
    )
    result = MODULE.submit_campaign(
        policy_path=policy_path,
        operations_path=operations_path,
        receipt_path=receipt_path,
        run_dir=tmp_path,
        execute=True,
        wait=True,
        poll_seconds=0,
        timeout_seconds=2,
        runner=runner,
        transport="git-objects",
    )
    starts = [command for command in commands if "start-build" in " ".join(command)]
    assert len(starts) == 1
    assert result["status"] == "completed"
    retried = [item for item in result["builds"] if item["index"] == 2][0]
    assert retried["attempt"] == 2
    assert retried["previous_failures"][0]["sn"] == "old-2"


def test_devgpu_recovery_config_runs_all_shards_with_clean_gpu_gates() -> None:
    config = MODULE.generate_campaign_devgpu_config(
        policy(),
        campaign_id="run-1",
        repository_slug="org/music-flamingo-campaign-run-1",
        item_count=48,
        source_manifest_sha256="b" * 64,
    )
    assert "  vscode:\n" in config
    assert "      tags: cnb:arch:amd64:gpu:L40" in config
    assert "    - name: Run receipt-bound Dev GPU full resume" in config
    assert "        sleep 60" in config
    assert "--phase before_hydrate" in config
    assert "--phase stable_before_hydrate" in config
    assert "for shard_index in $(seq 1 2)" in config
    assert "--phase \"pre_model_s${shard_index}\"" in config
    assert "run_music_flamingo_campaign.sh" in config


def test_devgpu_recovery_stage_script_is_valid_bash(tmp_path: Path) -> None:
    config = MODULE.generate_campaign_devgpu_config(
        policy(),
        campaign_id="run-1",
        repository_slug="org/music-flamingo-campaign-run-1",
        item_count=48,
        source_manifest_sha256="b" * 64,
    )
    marker = "    - name: Run receipt-bound Dev GPU full resume\n      timeout: 4h\n      script: |\n"
    stage = config.split(marker, 1)[1].split("\n    lock:\n", 1)[0]
    script = "\n".join(line[8:] for line in stage.splitlines()) + "\n"
    script_path = tmp_path / "devgpu-stage.sh"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(["bash", "-n", str(script_path)], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_git_push_environment_uses_preemptive_basic_header_without_askpass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CNB_TOKEN", "existing-admin-token")
    env, askpass = MODULE._git_push_environment()
    assert askpass is None
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    scheme, encoded = env["GIT_CONFIG_VALUE_0"].split(" ", 2)[1:]
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode("utf-8") == "cnb:existing-admin-token"
    assert "GIT_ASKPASS" not in env or env["GIT_ASKPASS"] == os.environ.get("GIT_ASKPASS")


def test_prepare_devgpu_overlay_reuses_exact_remote_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = policy()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=checkout, check=True)
    for relative in ("scripts/check_manual_gpu_gate.py", "scripts/run_music_flamingo_campaign.sh"):
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "campaign"], cwd=checkout, check=True)
    campaign_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True, capture_output=True, check=True
    ).stdout.strip()
    branch = "codex/devgpu-recovery-run-1"
    config = MODULE.generate_campaign_devgpu_config(
        value,
        campaign_id="run-1",
        repository_slug="org/music-flamingo-campaign-run-1",
        item_count=2,
        source_manifest_sha256="b" * 64,
    )
    subprocess.run(["git", "checkout", "-qb", branch], cwd=checkout, check=True)
    (checkout / ".cnb.yml").write_text(config, encoding="utf-8")
    subprocess.run(["git", "add", ".cnb.yml"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "overlay"], cwd=checkout, check=True)
    overlay_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True, capture_output=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "--detach", campaign_commit], cwd=checkout, check=True)
    monkeypatch.setenv("CNB_TOKEN", "existing-admin-token")

    def fake_authenticated(command, *, cwd, env):
        joined = " ".join(command)
        if "refs/heads/main" in joined:
            return f"{campaign_commit}\trefs/heads/main"
        if f"refs/heads/{branch}" in joined and "ls-remote" in joined:
            return f"{overlay_commit}\trefs/heads/{branch}"
        if " fetch " in f" {joined} ":
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(MODULE, "_authenticated_git_output", fake_authenticated)
    source = {
        "checkout": str(checkout),
        "campaign_commit": campaign_commit,
        "run_id": "run-1",
        "repository": "org/music-flamingo-campaign-run-1",
        "manifest": {"item_count": 2, "sha256": "b" * 64},
    }
    result = MODULE._prepare_devgpu_overlay(
        policy=value,
        source_receipt=source,
        recovery_dir=tmp_path / "recovery",
    )
    assert result["reused"] is True
    assert result["commit"] == overlay_commit
    assert result["parent_campaign_commit"] == campaign_commit
    assert MODULE.sha256_file(Path(result["path"]) / ".cnb.yml") == result["config_sha256"]


def test_devgpu_recovery_dry_run_creates_no_overlay_or_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    source = receipt_identity(tmp_path, count=2)
    source.update({"status": "failed", "campaign_commit": "c" * 40})
    source_path = tmp_path / "source-receipt.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    _, commands, runner = cnb_runner_factory(target_present=True)
    monkeypatch.setattr(
        MODULE,
        "_verify_build_gpu_platform_gate",
        lambda *_args, **_kwargs: {"classification": "cnb_build_gpu_pre_freezing_quota", "builds": []},
    )
    monkeypatch.setattr(
        MODULE,
        "_prepare_devgpu_overlay",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not prepare an overlay")),
    )
    result = MODULE.recover_campaign_with_devgpu(
        policy_path=policy_path,
        operations_path=OPERATIONS,
        source_receipt_path=source_path,
        recovery_receipt_path=tmp_path / "recovery" / "receipt.json",
        run_dir=tmp_path,
        execute=False,
        runner=runner,
        transport="git-objects",
    )
    assert result["status"] == "planned"
    assert not any("start-workspace" in " ".join(command) for command in commands)


def test_devgpu_recovery_stops_workspace_after_stage_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    source = receipt_identity(tmp_path, count=2)
    source.update({"status": "failed", "campaign_commit": "c" * 40})
    source_path = tmp_path / "source-receipt.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    recovery_path = tmp_path / "recovery" / "receipt.json"
    _, commands, base_runner = cnb_runner_factory(target_present=True)
    monkeypatch.setattr(
        MODULE,
        "_verify_build_gpu_platform_gate",
        lambda *_args, **_kwargs: {"classification": "cnb_build_gpu_pre_freezing_quota", "builds": []},
    )

    def runner(command):
        if "get-build-status" in " ".join(command):
            commands.append(list(command))
            return {"status": 200, "data": {"status": "failed", "pipelinesStatus": {}}}
        return base_runner(command)

    monkeypatch.setattr(
        MODULE,
        "_prepare_devgpu_overlay",
        lambda **_kwargs: {
            "branch": "codex/devgpu-recovery-run-1",
            "commit": "d" * 40,
            "parent_campaign_commit": "c" * 40,
            "config_sha256": "e" * 64,
            "path": str(tmp_path / "overlay"),
        },
    )
    with pytest.raises(MODULE.CampaignRepositoryError, match="terminal status failed"):
        MODULE.recover_campaign_with_devgpu(
            policy_path=policy_path,
            operations_path=OPERATIONS,
            source_receipt_path=source_path,
            recovery_receipt_path=recovery_path,
            run_dir=tmp_path,
            execute=True,
            wait=True,
            poll_seconds=0,
            timeout_seconds=2,
            runner=runner,
            transport="git-objects",
        )
    saved = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["workspace"]["stopped_after_failure"] is True
    assert sum("workspace-stop" in " ".join(command) for command in commands) == 1


def test_devgpu_workspace_success_requires_the_full_resume_stage() -> None:
    def runner(_command):
        return {"status": 200, "data": {"status": "success", "pipelinesStatus": {}}}

    with pytest.raises(MODULE.CampaignRepositoryError, match="without the receipt-bound full-resume stage"):
        MODULE._workspace_recovery_stage_status("org/repo", "cnb-workspace", runner)


def test_devgpu_recovery_requires_every_build_to_prove_pre_freezing_quota() -> None:
    source = {
        "repository": "org/music-flamingo-campaign-run-1",
        "failure": {"phase": "submit_or_recover", "message": "shard failed"},
        "builds": [{"sn": "cnb-one"}, {"sn": "cnb-two"}],
    }

    def runner(command):
        sn = command[command.index("--sn") + 1]
        if "get-build-status" in command:
            return {
                "status": 200,
                "data": {
                    "status": "error",
                    "pipelinesStatus": {
                        f"{sn}-001": {
                            "stages": [
                                {"id": "prepare", "name": "Prepare", "status": "error"},
                                {
                                    "id": "stage-0",
                                    "name": "Run disposable Music Flamingo campaign shard",
                                    "status": "skipped",
                                },
                            ]
                        }
                    },
                },
            }
        if "get-build-stage" in command:
            return {
                "status": 200,
                "data": {
                    "status": "error",
                    "error": "events GPU core-hours are insufficient for pre-freezing",
                },
            }
        raise AssertionError(command)

    evidence = MODULE._verify_build_gpu_platform_gate(source, runner)
    assert evidence["classification"] == "cnb_build_gpu_pre_freezing_quota"
    assert [item["sn"] for item in evidence["builds"]] == ["cnb-one", "cnb-two"]

    def wrong_failure_runner(command):
        if "get-build-status" in command:
            return runner(command)
        return {"status": 200, "data": {"status": "error", "error": "model import failed"}}

    with pytest.raises(MODULE.CampaignRepositoryError, match="does not prove"):
        MODULE._verify_build_gpu_platform_gate(source, wrong_failure_runner)


def test_devgpu_recovery_keeps_failed_source_receipt_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    source = receipt_identity(tmp_path, count=2)
    source.update(
        {
            "status": "failed",
            "campaign_commit": "c" * 40,
            "failure": {"phase": "submit_or_recover", "message": "build GPU quota"},
        }
    )
    source_path = tmp_path / "source-receipt.json"
    source_path.write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    source_sha = MODULE.sha256_file(source_path)
    recovery_path = tmp_path / "devgpu-recovery.json"
    _, commands, base_runner = cnb_runner_factory(target_present=True)

    def runner(command):
        if "get-build-status" in " ".join(command):
            commands.append(list(command))
            return {
                "status": 200,
                "data": {
                    "status": "success",
                    "pipelinesStatus": {
                        "cnb-workspace-1-001": {
                            "stages": [
                                {
                                    "id": "stage-0",
                                    "name": "Run receipt-bound Dev GPU full resume",
                                    "status": "success",
                                }
                            ]
                        }
                    },
                },
            }
        return base_runner(command)

    monkeypatch.setattr(
        MODULE,
        "_verify_build_gpu_platform_gate",
        lambda *_args, **_kwargs: {"classification": "cnb_build_gpu_pre_freezing_quota", "builds": []},
    )
    monkeypatch.setattr(
        MODULE,
        "_prepare_devgpu_overlay",
        lambda **_kwargs: {
            "branch": "codex/devgpu-recovery-run-1",
            "commit": "d" * 40,
            "parent_campaign_commit": "c" * 40,
            "config_sha256": "e" * 64,
            "path": str(tmp_path / "overlay"),
        },
    )
    monkeypatch.setattr(
        MODULE,
        "_recover_delivery",
        lambda *_args, **_kwargs: {
            "path": str(tmp_path / "canonical.jsonl"),
            "count": 2,
            "sha256": "f" * 64,
            "ledger_branch": "campaign-results/run-1",
        },
    )
    result = MODULE.recover_campaign_with_devgpu(
        policy_path=policy_path,
        operations_path=OPERATIONS,
        source_receipt_path=source_path,
        recovery_receipt_path=recovery_path,
        run_dir=tmp_path,
        execute=True,
        wait=True,
        poll_seconds=0,
        timeout_seconds=2,
        runner=runner,
        transport="git-objects",
    )
    assert result["status"] == "completed"
    assert result["workspace"]["stopped"] is True
    assert [item["index"] for item in result["logical_shards"]] == [1, 2]
    assert MODULE.sha256_file(source_path) == source_sha
    assert sum("start-workspace" in " ".join(command) for command in commands) == 1
    assert sum("workspace-stop" in " ".join(command) for command in commands) == 1


def test_recover_delivery_reuses_receipt_bound_ledger_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    ledger_dir = run_dir / "cnb" / "ledger-recovery"
    (ledger_dir / ".git").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=ledger_dir, check=True)
    subprocess.run(
        ["git", "config", "remote.origin.url", "https://cnb.cool/org/music-flamingo-campaign-run-1.git"],
        cwd=ledger_dir,
        check=True,
    )
    (ledger_dir / "campaign_ledger.jsonl").write_text("ledger\n", encoding="utf-8")
    checkout = tmp_path / "repo"
    builder = checkout / "scripts" / "build_kugou_canonical_delivery.py"
    builder.parent.mkdir(parents=True)
    builder.write_text("# fixture\n", encoding="utf-8")
    receipt = {
        "run_id": "run-1",
        "repository": "org/music-flamingo-campaign-run-1",
        "ledger_branch": "campaign-results/run-1",
        "checkout": str(checkout),
        "manifest": {"item_count": 2},
    }
    monkeypatch.setattr(MODULE, "_authenticated_clone", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("clone should not repeat")))

    def fake_run(command, *, cwd=None, timeout=None):
        output = Path(command[command.index("--output-manifest") + 1])
        output.write_text("{}\n{}\n", encoding="utf-8")
        Path(command[command.index("--output-state") + 1]).write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(MODULE, "_run", fake_run)
    result = MODULE._recover_delivery(receipt, run_dir=run_dir)
    assert result["count"] == 2
    assert Path(result["ledger"]).resolve() == (ledger_dir / "campaign_ledger.jsonl").resolve()


def test_cleanup_blocks_without_release_or_peer_gate(tmp_path: Path) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "repository": "org/music-flamingo-campaign-run-1",
                "runtime_image": RUNTIME,
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    _, commands, runner = cnb_runner_factory()
    result = MODULE.cleanup_campaign_repository(
        policy_path=policy_path,
        operations_path=operations_path,
        receipt_path=receipt_path,
        confirm=True,
        release_verified=False,
        peer_gate=False,
        runner=runner,
    )
    assert result["status"] == "blocked"
    assert {item["kind"] for item in result["failures"]} >= {"release-gate", "peer-gate"}
    assert not any("delete-repo" in " ".join(command) for command in commands)
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert saved["cleanup"]["status"] == "blocked"
    assert saved["cleanup"]["failures"] == result["failures"]


def test_cleanup_dry_run_records_receipt_without_delete(tmp_path: Path) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "repository": "org/music-flamingo-campaign-run-1",
                "runtime_image": RUNTIME,
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    _, commands, runner = cnb_runner_factory()
    result = MODULE.cleanup_campaign_repository(
        policy_path=policy_path,
        operations_path=operations_path,
        receipt_path=receipt_path,
        confirm=False,
        release_verified=False,
        peer_gate=False,
        runner=runner,
    )
    assert result["status"] == "dry_run"
    assert result["receipt"] == str(receipt_path.resolve())
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert saved["cleanup"]["status"] == "dry_run"
    assert not any("delete-repo" in " ".join(command) for command in commands)


def test_cleanup_blocks_completed_status_without_complete_receipt_proof(tmp_path: Path) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    receipt_path = tmp_path / "receipt.json"
    malformed = receipt_identity(tmp_path, count=1)
    malformed.update({"status": "completed", "builds": [], "delivery": None})
    receipt_path.write_text(json.dumps(malformed), encoding="utf-8")
    _, commands, runner = cnb_runner_factory(target_present=True)
    result = MODULE.cleanup_campaign_repository(
        policy_path=policy_path,
        operations_path=operations_path,
        receipt_path=receipt_path,
        confirm=True,
        release_verified=True,
        peer_gate=True,
        runner=runner,
        transport="git-objects",
    )
    assert result["status"] == "blocked"
    assert result["clean"] is False
    assert any("build shard" in item["error"] for item in result["failures"])
    assert any("canonical delivery" in item["error"] for item in result["failures"])
    assert not any("delete-repo" in " ".join(command) for command in commands)
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert saved["cleanup"]["status"] == "blocked"


def test_cleanup_deletes_only_after_complete_receipt_proof(tmp_path: Path) -> None:
    value = policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    operations_path = Path(__file__).parents[1] / "references" / "validated-operations.json"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(completed_receipt(tmp_path)), encoding="utf-8")
    state, commands, base_runner = cnb_runner_factory(target_present=True)

    def runner(command):
        if "delete-repo" in " ".join(command):
            state["target_present"] = False
            state["group_object"] -= 100
        return base_runner(command)

    result = MODULE.cleanup_campaign_repository(
        policy_path=policy_path,
        operations_path=operations_path,
        receipt_path=receipt_path,
        confirm=True,
        release_verified=True,
        peer_gate=True,
        runner=runner,
        transport="git-objects",
    )
    assert result["status"] == "succeeded"
    assert result["clean"] is True
    assert result["deleted"] is True
    assert any("delete-repo" in " ".join(command) for command in commands)


def test_reconciled_external_delivery_deletes_legacy_repository_without_mutating_source_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy()), encoding="utf-8")
    receipt_path, delivery_path, release_manifest = external_delivery_evidence(tmp_path, monkeypatch)
    source_before = receipt_path.read_bytes()
    reconciliation_path = tmp_path / "external-reconciliation.json"

    reconciliation = MODULE.reconcile_external_delivery(
        policy_path=policy_path,
        operations_path=OPERATIONS,
        source_receipt_path=receipt_path,
        delivery_path=delivery_path,
        release_manifest_path=release_manifest,
        reconciliation_receipt_path=reconciliation_path,
        transport="git-objects",
    )

    assert reconciliation["status"] == "succeeded"
    assert reconciliation["proof"]["identity"]["count"] == 1
    assert receipt_path.read_bytes() == source_before

    state, commands, base_runner = cnb_runner_factory(target_present=True)

    def runner(command):
        if "delete-repo" in " ".join(command):
            state["target_present"] = False
            state["group_object"] -= 100
        return base_runner(command)

    cleanup = MODULE.cleanup_reconciled_external_delivery_campaign(
        policy_path=policy_path,
        operations_path=OPERATIONS,
        reconciliation_receipt_path=reconciliation_path,
        confirm=True,
        release_verified=True,
        peer_gate=True,
        runner=runner,
        transport="git-objects",
    )

    assert cleanup["status"] == "succeeded"
    assert cleanup["clean"] is True
    assert cleanup["deleted"] is True
    assert receipt_path.read_bytes() == source_before
    assert any("delete-repo" in " ".join(command) for command in commands)
    saved = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    assert saved["cleanup"]["status"] == "succeeded"


def test_external_delivery_reconciliation_fails_closed_on_manifest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy()), encoding="utf-8")
    receipt_path, delivery_path, release_manifest = external_delivery_evidence(tmp_path, monkeypatch)
    source_before = receipt_path.read_bytes()
    row = json.loads(delivery_path.read_text(encoding="utf-8"))
    row["source_url"] = "https://example.test/not-the-manifest-url"
    delivery_path.write_text(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    reconciliation_path = tmp_path / "external-reconciliation.json"

    reconciliation = MODULE.reconcile_external_delivery(
        policy_path=policy_path,
        operations_path=OPERATIONS,
        source_receipt_path=receipt_path,
        delivery_path=delivery_path,
        release_manifest_path=release_manifest,
        reconciliation_receipt_path=reconciliation_path,
        transport="git-objects",
    )

    assert reconciliation["status"] == "blocked"
    assert any("source manifest/external delivery identity mismatch" in item["error"] for item in reconciliation["failures"])
    assert receipt_path.read_bytes() == source_before
