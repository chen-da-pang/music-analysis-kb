from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "cnb_campaign_repository.py"
SPEC = importlib.util.spec_from_file_location("cnb_campaign_repository", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


RUNTIME = "docker.cnb.cool/org/runner@sha256:" + "a" * 64


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
        if "start-build" in joined:
            return {"status": 200, "data": {"sn": "cnb-demo-1"}}
        if "get-build-status" in joined:
            return {"status": 200, "data": {"status": "success", "pipelinesStatus": {}}}
        raise AssertionError(command)

    return state, commands, run


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
    assert receipt["repository_created"] is False
    assert receipt["runtime_export"]["validated"] is True
    assert len(receipt["runtime_export"]["required_files"]) == len(MODULE.REQUIRED_CAMPAIGN_RUNTIME_FILES)
    assert Path(receipt["campaign_repository_config"]).is_file()
    assert Path(tmp_path / "run" / "cnb" / "campaign-receipt.json").is_file()
    assert not any("create-repo" in " ".join(command) for command in commands)


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
            {
                "schema_version": 1,
                "atom": "cnb_campaign_repository",
                "status": "created_and_pushed",
                "run_id": "run-1",
                "repository": "org/music-flamingo-campaign-run-1",
                "runtime_image": RUNTIME,
                "manifest": {"item_count": 1, "source_bytes": 4},
                "checkout": str(tmp_path / "repo"),
                "repository_created": True,
                "repository_pushed": True,
                "builds": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
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
                "schema_version": 1,
                "atom": "cnb_campaign_repository",
                "status": "failed",
                "run_id": "run-1",
                "repository": "org/music-flamingo-campaign-run-1",
                "runtime_image": RUNTIME,
                "manifest": {"item_count": 2, "source_bytes": 8},
                "checkout": str(tmp_path / "repo"),
                "repository_created": True,
                "repository_pushed": True,
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
    )
    starts = [command for command in commands if "start-build" in " ".join(command)]
    assert len(starts) == 1
    assert result["status"] == "completed"
    assert [item["index"] for item in result["builds"]] == [1, 2]


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
