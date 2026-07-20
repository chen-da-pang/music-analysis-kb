"""End-to-end publisher orchestration for one resumable weekly run."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .campaign_delivery import load_campaign_delivery_file
from .operation_context import RunContext, atom, atomic_write_json
from .preflight import run_preflight
from .repository import MusicKBRepository
from .snapshot import create_snapshot, current_snapshot_target, install_snapshot, verify_snapshot


DEFAULT_CHART_PROFILE = Path(__file__).resolve().parents[2] / "references" / "kugou-chart-profile.json"
DEFAULT_CNB_STORAGE_POLICY = Path(__file__).resolve().parents[2] / "references" / "cnb-storage-policy.json"


def _json_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout_seconds}s: {shlex.join(command)}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed with exit={completed.returncode}: {shlex.join(command)}\n"
            f"{completed.stderr.strip()}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"command returned no JSON output: {shlex.join(command)}")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command returned invalid JSON: {shlex.join(command)}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"command JSON result must be an object: {shlex.join(command)}")
    return value, completed


def _safe_run_id(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if not value or any(character not in allowed for character in value):
        raise ValueError("run_id contains unsafe characters")
    return value


def _resolve_workspace_path(value: str | Path, workspace: Path) -> Path:
    path = Path(value).expanduser()
    return (workspace / path).resolve() if not path.is_absolute() else path.resolve()


def _load_chart_profile(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path).expanduser().resolve()
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Kugou chart profile is unreadable: {profile_path}: {exc}") from exc
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise ValueError(f"Kugou chart profile schema must be 1: {profile_path}")
    charts = profile.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ValueError(f"Kugou chart profile has no charts: {profile_path}")
    rank_ids: set[str] = set()
    for chart in charts:
        if not isinstance(chart, dict):
            raise ValueError(f"Kugou chart profile contains a non-object chart: {profile_path}")
        rank_id = str(chart.get("rank_id", "")).strip()
        if not rank_id.isdigit() or int(rank_id) <= 0 or rank_id in rank_ids:
            raise ValueError(f"Kugou chart profile has an invalid/duplicate rank id: {rank_id}")
        rank_ids.add(rank_id)
    page_size = profile.get("page_size", 100)
    if not isinstance(page_size, int) or page_size < 1 or page_size > 500:
        raise ValueError(f"Kugou chart profile page_size must be between 1 and 500: {profile_path}")
    max_pages = profile.get("max_pages", 100)
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError(f"Kugou chart profile max_pages must be positive: {profile_path}")
    minimum_total = profile.get("minimum_total_source_records", 1)
    if not isinstance(minimum_total, int) or minimum_total < 1:
        raise ValueError(
            f"Kugou chart profile minimum_total_source_records must be positive: {profile_path}"
        )
    return {
        **profile,
        "profile_path": str(profile_path),
        "page_size": page_size,
        "max_pages": max_pages,
        "minimum_total_source_records": minimum_total,
    }


def _merge_chart_exports(paths: Sequence[Path], output: Path, *, run_id: str) -> dict[str, Any]:
    unique: dict[str, dict[str, Any]] = {}
    source_records = 0
    duplicate_records = 0
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("songs", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError(f"chart songs payload is not an array: {path}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_records += 1
            key = row.get("identity_key") or row.get("title_artist_key")
            if not key:
                continue
            if key in unique:
                duplicate_records += 1
                old_appearances = unique[key].setdefault("chart_appearances", [])
                old_appearances.extend(row.get("chart_appearances", []))
                if not unique[key].get("play_link") and row.get("play_link"):
                    unique[key]["play_link"] = row["play_link"]
                continue
            unique[key] = dict(row)

    payload = {
        "schema_version": 1,
        "platform": "kugou",
        "run_id": run_id,
        "summary": {
            "run_id": run_id,
            "source_records": source_records,
            "source_unique_records": len(unique),
            "duplicate_source_records": duplicate_records,
        },
        "songs": list(unique.values()),
    }
    atomic_write_json(output, payload)
    return payload["summary"]


def _inventory_song_count(path: Path) -> int:
    value = json.loads(path.read_text(encoding="utf-8"))
    songs = value.get("songs") if isinstance(value, dict) else None
    if not isinstance(songs, list) or not songs:
        raise ValueError(f"inventory has no songs: {path}")
    return len(songs)


def _cleanup_gate_satisfied(
    *,
    publish: bool,
    release_result: dict[str, Any] | None,
    skip_peers: bool,
    publish_result: dict[str, Any],
) -> bool:
    """Allow destructive cleanup only after local publication is explicit and safe."""

    if not publish or release_result is None:
        return False
    if skip_peers:
        return True
    return (
        int(publish_result.get("peer_count", 0)) > 0
        and int(publish_result.get("failed_count", 0)) == 0
    )


def _cnb_cleanup_receipt_is_acceptable(result: dict[str, Any]) -> bool:
    """Treat visible cleanup plus pending server GC as a completed cleanup atom."""

    return (
        bool(result.get("visible_cleanup_complete"))
        and not result.get("failures")
        and (bool(result.get("clean")) or bool(result.get("server_gc_pending")))
    )


def run_weekly_run(
    *,
    workspace: str | Path,
    run_id: str,
    rank_ids: Sequence[str],
    chart_page: int,
    chart_size: int,
    chart_profile: str | Path | None,
    database: str | Path,
    inventory: str | Path,
    audio_root: str | Path,
    legacy_progress: str | Path,
    operations_file: str | Path,
    output_dir: str | Path,
    release_name: str | None,
    local_snapshot_dir: str | Path | None = None,
    install_local: bool | None = None,
    peers_file: str | Path | None,
    peer_names: Sequence[str],
    publish: bool,
    delivery: str | Path | None,
    cnb_command: str | None,
    chart_database: str | Path | None,
    state_file: str | Path,
    proxy: str | None = None,
    download_dry_run: bool = False,
    download_max_items: int | None = None,
    confirm_delete_audio: bool = False,
    expected_count: int | None = None,
    timeout_seconds: int = 86_400,
    skip_peers: bool = False,
    cnb_storage_policy: str | Path = DEFAULT_CNB_STORAGE_POLICY,
    confirm_delete_cnb_storage: bool = False,
    cnb_transport: str = "lfs",
) -> dict[str, Any]:
    """Run all safe publisher stages and stop at the first failed atom."""

    root = Path(workspace).expanduser().resolve()
    run_id = _safe_run_id(run_id)
    run_dir = root / "data" / "weekly_runs" / run_id
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    database_path = _resolve_workspace_path(database, root)
    inventory_path = _resolve_workspace_path(inventory, root)
    audio_path = _resolve_workspace_path(audio_root, root)
    progress_path = _resolve_workspace_path(legacy_progress, root)
    operations_path = _resolve_workspace_path(operations_file, root)
    output_dir_path = _resolve_workspace_path(output_dir, root)
    local_snapshot_path = (
        _resolve_workspace_path(local_snapshot_dir, root)
        if local_snapshot_dir is not None
        else database_path.parent
    )
    if publish and install_local is False:
        raise ValueError("--no-install-local cannot be combined with --publish")
    install_local_enabled = publish if install_local is None else bool(install_local)
    peers_path = _resolve_workspace_path(peers_file, root) if peers_file else None
    state_path = _resolve_workspace_path(state_file, root)
    chart_db_path = _resolve_workspace_path(chart_database, root) if chart_database else None
    cnb_storage_policy_path = Path(cnb_storage_policy).expanduser().resolve()
    if cnb_transport not in {"lfs", "git-objects"}:
        raise ValueError("cnb_transport must be lfs or git-objects")
    delivery_path: Path | None = _resolve_workspace_path(delivery, root) if delivery else None
    delivery_supplied = delivery is not None
    resume_reason = "verified supplied delivery resumes after Music Flamingo analysis"
    required_commands: tuple[str, ...] = () if delivery_supplied else ("kugou-cli", "claude")
    if publish and not skip_peers:
        required_commands += ("rsync",)
    explicit_ranks = list(rank_ids)
    profile_path = Path(chart_profile).expanduser().resolve() if chart_profile else DEFAULT_CHART_PROFILE
    profile = None if explicit_ranks else _load_chart_profile(profile_path)
    capture_mode = "explicit_page" if explicit_ranks else "full_profile"
    capture_size = chart_size if explicit_ranks else int(profile["page_size"])

    with RunContext(run_id=run_id, run_dir=run_dir, operations_file=operations_path) as context:
        with atom(context, "preflight", inputs={"workspace": str(root), "publish": publish} ) as outputs:
            preflight = run_preflight(
                workspace=root,
                operations_file=operations_path,
                database=database_path,
                inventory=inventory_path,
                audio_root=audio_path,
                peers_file=None if skip_peers else peers_path,
                publish=publish and not skip_peers,
                required_commands=required_commands,
            )
            outputs.update(preflight)
            if not preflight["valid"]:
                raise RuntimeError(f"weekly preflight failed: {preflight['failed_required']}")
            if publish:
                missing_cleanup_confirmations = []
                if not confirm_delete_audio:
                    missing_cleanup_confirmations.append("--confirm-delete-audio")
                if not confirm_delete_cnb_storage:
                    missing_cleanup_confirmations.append("--confirm-delete-cnb-storage")
                if missing_cleanup_confirmations:
                    outputs["missing_cleanup_confirmations"] = missing_cleanup_confirmations
                    raise RuntimeError(
                        "this publishing weekly-run invocation must guarantee post-run cleanup; missing: "
                        + ", ".join(missing_cleanup_confirmations)
                    )

        with atom(
            context,
            "cnb_storage_preflight",
            inputs={"policy": str(cnb_storage_policy_path), "transport": cnb_transport},
        ) as outputs:
            if delivery_supplied:
                if delivery_path is None or not delivery_path.is_file():
                    raise RuntimeError(f"supplied canonical delivery does not exist: {delivery_path}")
                entries = load_campaign_delivery_file(delivery_path, expected_count=expected_count)
                outputs.update(
                    {
                        "status": "skipped",
                        "reason": resume_reason,
                        "delivery": str(delivery_path),
                        "delivery_count": len(entries),
                    }
                )
            else:
                command = [
                    sys.executable,
                    str(scripts_dir / "cnb_storage_lifecycle.py"),
                    "inspect",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--transport",
                    cnb_transport,
                ]
                completed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
                lines = [line for line in completed.stdout.splitlines() if line.strip()]
                if lines:
                    try:
                        outputs.update(json.loads(lines[-1]))
                    except json.JSONDecodeError:
                        outputs["stdout"] = completed.stdout[-2000:]
                if completed.returncode != 0:
                    raise RuntimeError(
                        "CNB object storage is not clean before this weekly-run invocation: "
                        f"{completed.stderr.strip() or outputs}"
                    )

        chart_dir = run_dir / "charts"
        capture_results: list[dict[str, Any]] = []
        with atom(
            context,
            "chart_capture",
            inputs={
                "mode": capture_mode,
                "rank_ids": explicit_ranks if explicit_ranks else [chart["rank_id"] for chart in profile["charts"]],
                "profile": str(profile_path) if profile else None,
                "page": chart_page,
                "size": capture_size,
                "termination": profile.get("termination") if profile else "single_page",
            },
        ) as outputs:
            if delivery_supplied:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                charts = (
                    [{"rank_id": rank_id} for rank_id in explicit_ranks]
                    if explicit_ranks
                    else profile["charts"]
                )
                for chart in charts:
                    rank_id = str(chart["rank_id"])
                    page = chart_page
                    while True:
                        command = [
                            sys.executable,
                            str(scripts_dir / "capture_kugou_chart.py"),
                            "--run-id",
                            run_id,
                            "--rank-id",
                            rank_id,
                            "--page",
                            str(page),
                            "--size",
                            str(capture_size),
                            "--output-dir",
                            str(chart_dir),
                            "--operations-file",
                            str(operations_path),
                        ]
                        if proxy:
                            command.extend(["--proxy", proxy])
                        result, _ = _json_command(command, cwd=root, timeout_seconds=min(timeout_seconds, 300))
                        capture_results.append(result)
                        if capture_mode == "explicit_page":
                            break
                        source_records = int(result.get("source_records", 0))
                        if source_records == 0 or source_records < capture_size:
                            break
                        page += 1
                        if page - chart_page >= int(profile["max_pages"]):
                            raise RuntimeError(
                                f"Kugou chart pagination exceeded max_pages={profile['max_pages']}: rank_id={rank_id}"
                            )
                total_source_records = sum(int(item.get("source_records", 0)) for item in capture_results)
                if capture_mode == "full_profile" and total_source_records < int(profile["minimum_total_source_records"]):
                    raise RuntimeError(
                        "full Kugou chart profile captured too few records: "
                        f"{total_source_records} < {profile['minimum_total_source_records']}"
                    )
                outputs.update(
                    {
                        "mode": capture_mode,
                        "profile": str(profile_path) if profile else None,
                        "captures": capture_results,
                        "count": len(capture_results),
                        "source_records": total_source_records,
                    }
                )

        merged_chart = run_dir / "chart-songs.json"
        with atom(context, "chart_dedupe", inputs={"captures": [item["songs"] for item in capture_results]}) as outputs:
            if delivery_supplied:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                summary = _merge_chart_exports(
                    [Path(item["songs"]) for item in capture_results], merged_chart, run_id=run_id
                )
                outputs.update({**summary, "songs": str(merged_chart)})

        queue_path = run_dir / "download_queue.jsonl"
        queue_manifest: dict[str, Any] = {}
        with atom(context, "historical_dedupe", inputs={"inventory": str(inventory_path), "source": str(merged_chart)}) as outputs:
            if delivery_supplied:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                inventory_command = [
                    sys.executable,
                    str(scripts_dir / "build_song_inventory.py"),
                    "--db",
                    str(root / "data" / "music_trends.sqlite"),
                    "--progress",
                    str(progress_path),
                    "--inventory",
                    str(inventory_path),
                    "--audio-root",
                    str(audio_path),
                ]
                inventory_result, _ = _json_command(inventory_command, cwd=root, timeout_seconds=min(timeout_seconds, 600))
                queue_command = [
                    sys.executable,
                    str(scripts_dir / "prepare_download_queue.py"),
                    "--source",
                    str(merged_chart),
                    "--inventory",
                    str(inventory_path),
                    "--output",
                    str(queue_path),
                    "--audio-root",
                    str(audio_path),
                ]
                queue_manifest, _ = _json_command(queue_command, cwd=root, timeout_seconds=min(timeout_seconds, 600))
                outputs.update({"inventory": inventory_result, "queue": queue_manifest})

        with atom(context, "claude_download", inputs={"queue": str(queue_path), "dry_run": download_dry_run}) as outputs:
            if delivery_supplied:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                command = [
                    sys.executable,
                    str(scripts_dir / "run_claude_download.py"),
                    "--workspace",
                    str(root),
                    "--source",
                    str(merged_chart),
                    "--run-id",
                    run_id,
                    "--operations-file",
                    str(operations_path),
                    "--timeout-seconds",
                    str(timeout_seconds),
                ]
                if proxy:
                    command.extend(["--proxy", proxy])
                if download_dry_run:
                    command.append("--dry-run")
                if download_max_items is not None:
                    command.extend(["--max-items", str(download_max_items)])
                download_result, _ = _json_command(command, cwd=root, timeout_seconds=timeout_seconds + 30)
                outputs.update(download_result)

        fallback_run_id = f"{run_id}-fallback"
        with atom(
            context,
            "fallback_download",
            inputs={"run_id": fallback_run_id, "dry_run": download_dry_run},
        ) as outputs:
            if delivery_supplied:
                outputs.update({"status": "skipped", "reason": resume_reason})
            else:
                fallback_command = [
                    sys.executable,
                    str(scripts_dir / "run_claude_fallback.py"),
                    "--workspace",
                    str(root),
                    "--run-id",
                    fallback_run_id,
                    "--operations-file",
                    str(operations_path),
                    "--timeout-seconds",
                    str(timeout_seconds),
                ]
                if proxy:
                    fallback_command.extend(["--proxy", proxy])
                if download_dry_run:
                    fallback_command.append("--dry-run")
                fallback_result, _ = _json_command(fallback_command, cwd=root, timeout_seconds=timeout_seconds + 30)
                outputs.update(fallback_result)

        if delivery_supplied:
            with atom(
                context,
                "cnb_input_materialization",
                inputs={"delivery": str(delivery_path) if delivery_path else None},
            ) as outputs:
                outputs.update({"status": "skipped", "reason": resume_reason})

        if delivery_path is None and cnb_command:
            delivery_path = run_dir / "cnb" / "canonical_delivery.jsonl"
        with atom(context, "cnb_analysis", inputs={"delivery": str(delivery_path) if delivery_path else None, "command": cnb_command}) as outputs:
            if delivery_supplied and delivery_path is not None and delivery_path.is_file():
                entries = load_campaign_delivery_file(delivery_path, expected_count=expected_count)
                outputs.update(
                    {
                        "status": "supplied_delivery_validated",
                        "delivery": str(delivery_path),
                        "count": len(entries),
                    }
                )
            elif cnb_command and delivery_path is not None:
                delivery_path.parent.mkdir(parents=True, exist_ok=True)
                env = os.environ.copy()
                env.update(
                    {
                        "MUSIC_KB_WEEKLY_RUN_ID": run_id,
                        "MUSIC_KB_CNB_OUTPUT": str(delivery_path),
                        "MUSIC_KB_CNB_INPUT": str(merged_chart),
                        "MUSIC_KB_AUDIO_ROOT": str(audio_path),
                    }
                )
                command = shlex.split(cnb_command)
                completed = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=timeout_seconds, check=False)
                outputs.update({"command": command, "returncode": completed.returncode, "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:]})
                if completed.returncode != 0 or not delivery_path.is_file():
                    raise RuntimeError("CNB command did not produce a canonical delivery")
                outputs["count"] = len(load_campaign_delivery_file(delivery_path))
            elif download_dry_run:
                outputs.update({"status": "skipped", "reason": "dry-run has no CNB delivery"})
            else:
                raise RuntimeError("CNB stage requires --cnb-delivery or --cnb-command")

        import_result: dict[str, Any]
        with atom(context, "knowledge_import", inputs={"delivery": str(delivery_path) if delivery_path else None}) as outputs:
            if delivery_path is None or not delivery_path.is_file():
                outputs.update({"status": "skipped", "reason": "no delivery in dry-run"})
            else:
                entries = load_campaign_delivery_file(delivery_path, expected_count=expected_count)
                with MusicKBRepository(database_path, read_only=False) as repository:
                    import_result = repository.import_campaign_delivery(entries)
                    if chart_db_path is not None and chart_db_path.is_file():
                        link_result = repository.backfill_source_links(chart_db_path)
                    else:
                        link_result = {"skipped": True, "reason": "chart database not supplied"}
                    tag_result = repository.enrich_campaign_tags(dry_run=False, batch_size=500)
                    validation = repository.validate()
                    status = repository.status()
                if not validation["valid"]:
                    raise RuntimeError(f"master validation failed: {validation}")
                counts = status["counts"]
                if counts["source_tracks"] <= 0 or counts["source_links"] != counts["source_tracks"]:
                    raise RuntimeError(
                        "source link completeness gate failed: "
                        f"source_tracks={counts['source_tracks']} source_links={counts['source_links']}"
                    )
                import_result = {"import": import_result, "links": link_result, "tags": tag_result, "validation": validation, "status": status}
                outputs.update(import_result)

        release_result: dict[str, Any] | None = None
        release_verification: dict[str, Any] | None = None
        with atom(context, "snapshot", inputs={"database": str(database_path), "output_dir": str(output_dir_path)}) as outputs:
            if delivery_path is None or not delivery_path.is_file():
                outputs.update({"status": "skipped", "reason": "no delivery in dry-run"})
            else:
                release_result = create_snapshot(database_path, output_dir_path, release_name=release_name)
                release_verification = verify_snapshot(Path(release_result["manifest"]))
                outputs.update({"release": release_result, "verification": release_verification})

        local_install_result: dict[str, Any] | None = None
        with atom(
            context,
            "local_snapshot_install",
            inputs={
                "release_dir": str(release_result["release_dir"]) if release_result else None,
                "release_sha256": str((release_verification or {}).get("sha256") or ""),
                "target_dir": str(local_snapshot_path),
                "enabled": install_local_enabled,
            },
        ) as outputs:
            verification_receipt = None
            release_sha256 = ""
            release_name = None
            previous_current = current_snapshot_target(local_snapshot_path)
            if release_verification is not None:
                release_sha256 = str(release_verification.get("sha256") or "")
                release_name = release_verification.get("release_name")
                verification_receipt = {
                    "valid": bool(release_verification.get("valid")),
                    "manifest": str(release_verification.get("manifest") or ""),
                    "sha256": release_sha256,
                }
            outputs.update(
                {
                    "release_name": release_name,
                    "release_sha256": release_sha256,
                    "verification": verification_receipt,
                    "target_dir": str(local_snapshot_path),
                    "previous_current": previous_current,
                }
            )
            if release_result is None:
                outputs.update({"status": "skipped", "reason": "no release in dry-run"})
            elif not install_local_enabled:
                outputs.update(
                    {
                        "status": "skipped",
                        "reason": "publisher-local install disabled",
                    }
                )
            else:
                try:
                    local_install_result = install_snapshot(release_result["release_dir"], local_snapshot_path)
                except Exception:
                    outputs["current_target"] = current_snapshot_target(local_snapshot_path)
                    raise
                outputs.update({"status": "succeeded", **local_install_result})

        publish_result: dict[str, Any] = {"status": "skipped", "reason": "no release in dry-run"}
        with atom(context, "peer_publish", inputs={"publish": publish, "peers_file": str(peers_path) if peers_path else None}) as outputs:
            if release_result is None:
                outputs.update(publish_result)
            elif skip_peers:
                publish_result = {
                    "status": "skipped",
                    "reason": "peer sync explicitly skipped",
                    "peer_count": 0,
                    "succeeded_count": 0,
                    "failed_count": 0,
                    "dry_run": not publish,
                }
                outputs.update(publish_result)
            else:
                from .distribution import publish_snapshot

                publish_result = publish_snapshot(
                    release_result["release_dir"],
                    peers_path,
                    peer_names=peer_names,
                    dry_run=not publish,
                )
                outputs.update(publish_result)
                if publish and publish_result["failed_count"]:
                    raise RuntimeError(f"peer publish failed: {publish_result}")
                if publish:
                    from .publish_state import record_publish_result

                    record_publish_result(
                        state_path,
                        publish_result,
                        release_sha256=str((release_verification or {}).get("sha256") or ""),
                    )
                    outputs["state_file"] = str(state_path)

        cleanup_gate = _cleanup_gate_satisfied(
            publish=publish,
            release_result=release_result,
            skip_peers=skip_peers,
            publish_result=publish_result,
        )
        with atom(
            context,
            "audio_cleanup",
            inputs={"confirm": confirm_delete_audio, "cleanup_gate": cleanup_gate},
        ) as outputs:
            if not confirm_delete_audio:
                outputs.update({"status": "skipped", "reason": "confirmation flag not supplied"})
            elif not cleanup_gate:
                raise RuntimeError(
                    "audio cleanup requires a verified release and either explicit peer skip or successful peer publication"
                )
            else:
                expected_inventory_count = _inventory_song_count(inventory_path)
                cleanup_command = [
                    sys.executable,
                    str(scripts_dir / "prune_audio_library.py"),
                    "--inventory",
                    str(inventory_path),
                    "--audio-root",
                    str(audio_path),
                    "--knowledge-db",
                    str(database_path),
                    "--expected-count",
                    str(expected_inventory_count),
                    "--confirm-delete-audio",
                ]
                cleanup_result, _ = _json_command(cleanup_command, cwd=root, timeout_seconds=min(timeout_seconds, 600))
                outputs.update(cleanup_result)

        with atom(
            context,
            "cnb_storage_cleanup",
            inputs={
                "policy": str(cnb_storage_policy_path),
                "confirm": confirm_delete_cnb_storage,
                "transport": cnb_transport,
            },
        ) as outputs:
            if not confirm_delete_cnb_storage:
                outputs.update({"status": "skipped", "reason": "confirmation flag not supplied"})
            elif not cleanup_gate:
                raise RuntimeError(
                    "CNB storage cleanup requires a verified release and either explicit peer skip or successful peer publication"
                )
            else:
                command = [
                    sys.executable,
                    str(scripts_dir / "cnb_storage_lifecycle.py"),
                    "cleanup",
                    "--policy",
                    str(cnb_storage_policy_path),
                    "--transport",
                    cnb_transport,
                    "--confirm-cleanup",
                ]
                completed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
                lines = [line for line in completed.stdout.splitlines() if line.strip()]
                if lines:
                    try:
                        outputs.update(json.loads(lines[-1]))
                    except json.JSONDecodeError:
                        outputs["stdout"] = completed.stdout[-2000:]
                if completed.returncode != 0:
                    if _cnb_cleanup_receipt_is_acceptable(outputs):
                        outputs["status"] = "succeeded_with_server_gc_pending"
                    else:
                        raise RuntimeError(
                            "CNB object storage cleanup did not verify: "
                            f"{completed.stderr.strip() or outputs}"
                        )

    return {
        "workflow": "weekly-run",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "state": str(run_dir / "run-state.json"),
        "local_install": local_install_result,
        "publish": publish_result,
    }
