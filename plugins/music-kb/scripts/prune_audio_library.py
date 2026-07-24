#!/usr/bin/env python3
"""Delete only locally present audio proven to be in the verified knowledge base.

The inventory remains the deduplication authority. Purged records keep their
identity and historical path but are marked ``purged_after_analysis``; future
queue preparation therefore skips them without requiring the audio file.
Downloaded tracks without a matching source-track entry remain untouched for
future Music Flamingo analysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_strict_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    """Read a physical-LF JSONL evidence file without accepting blank rows."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"无法读取{label}: {path}: {exc}") from exc
    if not raw or not raw.endswith(b"\n") or b"\r" in raw:
        raise RuntimeError(f"{label}必须是非空的 physical-LF JSONL: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise RuntimeError(f"{label}包含空行: {path}:{line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label}不是有效 JSON: {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{label}行必须是对象: {path}:{line_number}")
        rows.append(value)
    return rows


def _required_text(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"缺少{label}")
    return text


def _required_sha256(value: object, *, label: str) -> str:
    digest = _required_text(value, label=label).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RuntimeError(f"{label}必须是 SHA-256")
    return digest


def _required_positive_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label}必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}必须是正整数") from exc
    if number <= 0:
        raise RuntimeError(f"{label}必须是正整数")
    return number


def _safe_audio_relative_path(value: object, *, label: str) -> str:
    relative = _required_text(value, label=label)
    if "\\" in relative:
        raise RuntimeError(f"{label}必须使用 POSIX 路径")
    parsed = PurePosixPath(relative)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or parsed.parts[0] != "audio"
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise RuntimeError(f"{label}必须是 audio/ 下的安全相对路径")
    return relative


def _delivery_binding(row: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    return {
        "campaign_id": _required_text(row.get("campaign_id"), label=f"{label}.campaign_id"),
        "id": _required_text(row.get("id"), label=f"{label}.id"),
        "relative_audio_path": _safe_audio_relative_path(
            row.get("relative_audio_path"), label=f"{label}.relative_audio_path"
        ),
        "sha256": _required_sha256(
            row.get("source_sha256", row.get("sha256")), label=f"{label}.source_sha256"
        ),
        "source_bytes": _required_positive_integer(row.get("source_bytes"), label=f"{label}.source_bytes"),
        "source_url": _required_text(row.get("source_url"), label=f"{label}.source_url"),
    }


def load_delivery_bindings(path: Path) -> dict[str, dict[str, Any]]:
    rows = _load_strict_jsonl(path, label="canonical delivery")
    bindings: dict[str, dict[str, Any]] = {}
    for line_number, row in enumerate(rows, start=1):
        binding = _delivery_binding(row, label=f"canonical delivery line {line_number}")
        delivery_id = str(binding["id"])
        if delivery_id in bindings:
            raise RuntimeError(f"canonical delivery包含重复 id: {delivery_id}")
        if any(
            existing["relative_audio_path"] == binding["relative_audio_path"]
            for existing in bindings.values()
        ):
            raise RuntimeError(
                "canonical delivery包含重复 relative_audio_path: "
                f"{binding['relative_audio_path']}"
            )
        bindings[delivery_id] = binding
    return bindings


def _staging_cleanup_plan(staging: Path, delivery: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    root = staging.expanduser().resolve()
    manifest_path = root / "manifest.jsonl"
    if not root.is_dir() or not manifest_path.is_file():
        raise RuntimeError(f"delivery-bound staging缺少 manifest.jsonl: {root}")
    manifest_rows = _load_strict_jsonl(manifest_path, label="campaign staging manifest")
    manifest: dict[str, dict[str, Any]] = {}
    for line_number, row in enumerate(manifest_rows, start=1):
        binding = _delivery_binding(row, label=f"campaign staging manifest line {line_number}")
        delivery_id = str(binding["id"])
        if delivery_id in manifest:
            raise RuntimeError(f"campaign staging manifest包含重复 id: {delivery_id}")
        manifest[delivery_id] = binding
    if set(manifest) != set(delivery):
        missing = sorted(set(delivery) - set(manifest))
        unexpected = sorted(set(manifest) - set(delivery))
        raise RuntimeError(
            "delivery-bound staging manifest与canonical delivery的来源集合不一致: "
            f"missing={missing[:5]} unexpected={unexpected[:5]}"
        )
    for delivery_id, expected in delivery.items():
        observed = manifest[delivery_id]
        compared_fields = ("campaign_id", "relative_audio_path", "sha256", "source_bytes", "source_url")
        if any(observed[field] != expected[field] for field in compared_fields):
            raise RuntimeError(
                "delivery-bound staging manifest与canonical delivery不一致: "
                f"{delivery_id}"
            )

    audio_root = root / "audio"
    if not audio_root.exists():
        return {
            "path": str(root),
            "manifest": str(manifest_path),
            "status": "already_clean",
            "file_count": 0,
            "logical_bytes": 0,
            "paths": [],
        }
    if not audio_root.is_dir():
        raise RuntimeError(f"delivery-bound staging audio不是目录: {audio_root}")
    expected_paths = {root / Path(*PurePosixPath(binding["relative_audio_path"]).parts) for binding in delivery.values()}
    actual_paths = {path for path in audio_root.rglob("*") if path.is_file()}
    if actual_paths != expected_paths:
        missing = sorted(str(path.relative_to(root)) for path in expected_paths - actual_paths)
        unexpected = sorted(str(path.relative_to(root)) for path in actual_paths - expected_paths)
        raise RuntimeError(
            "delivery-bound staging audio与manifest不一致: "
            f"missing={missing[:5]} unexpected={unexpected[:5]}"
        )
    paths_by_relative = {
        binding["relative_audio_path"]: (delivery_id, binding)
        for delivery_id, binding in delivery.items()
    }
    logical_bytes = 0
    for path in sorted(actual_paths):
        relative = path.relative_to(root).as_posix()
        _delivery_id, binding = paths_by_relative[relative]
        if path.stat().st_size != int(binding["source_bytes"]):
            raise RuntimeError(f"delivery-bound staging audio字节数不匹配: {relative}")
        if sha256_file(path) != str(binding["sha256"]):
            raise RuntimeError(f"delivery-bound staging audio哈希不匹配: {relative}")
        logical_bytes += path.stat().st_size
    return {
        "path": str(root),
        "manifest": str(manifest_path),
        "status": "ready",
        "file_count": len(actual_paths),
        "logical_bytes": logical_bytes,
        "paths": [str(path) for path in sorted(actual_paths)],
    }


def _delivery_staging_cleanup_plan(
    delivery_path: Path | None, campaign_staging_paths: Sequence[Path],
) -> dict[str, Any]:
    if delivery_path is None:
        if campaign_staging_paths:
            raise RuntimeError("campaign staging cleanup requires --delivery")
        return {
            "status": "skipped",
            "reason": "no canonical delivery supplied",
            "staging_directory_count": 0,
            "ready_directory_count": 0,
            "already_clean_directory_count": 0,
            "file_count": 0,
            "logical_bytes": 0,
            "staging_paths": [],
            "plans": [],
        }
    delivery = load_delivery_bindings(delivery_path.expanduser().resolve())
    if not campaign_staging_paths:
        return {
            "status": "skipped",
            "reason": "no delivery-bound local staging directory exists",
            "delivery": str(delivery_path.expanduser().resolve()),
            "delivery_count": len(delivery),
            "staging_directory_count": 0,
            "ready_directory_count": 0,
            "already_clean_directory_count": 0,
            "file_count": 0,
            "logical_bytes": 0,
            "staging_paths": [],
            "plans": [],
        }
    plans = [_staging_cleanup_plan(path, delivery) for path in campaign_staging_paths]
    ready = [plan for plan in plans if plan["status"] == "ready"]
    return {
        "status": "ready" if ready else "already_clean",
        "delivery": str(delivery_path.expanduser().resolve()),
        "delivery_count": len(delivery),
        "staging_directory_count": len(plans),
        "ready_directory_count": len(ready),
        "already_clean_directory_count": len(plans) - len(ready),
        "file_count": sum(int(plan["file_count"]) for plan in ready),
        "logical_bytes": sum(int(plan["logical_bytes"]) for plan in ready),
        "staging_paths": [str(plan["path"]) for plan in plans],
        "plans": plans,
    }


def _remove_delivery_staging_audio(plan: Mapping[str, Any]) -> dict[str, Any]:
    removed_files = 0
    for staging in plan.get("plans", []):
        if not isinstance(staging, Mapping) or staging.get("status") != "ready":
            continue
        paths = [Path(str(path)) for path in staging.get("paths", [])]
        root = Path(str(staging["path"]))
        audio_root = root / "audio"
        for path in paths:
            path.unlink(missing_ok=True)
            removed_files += 1
        for directory in sorted(
            (path for path in audio_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            audio_root.rmdir()
        except OSError:
            pass
    return {
        key: value
        for key, value in {
            **dict(plan),
            "status": "removed" if removed_files else str(plan.get("status", "already_clean")),
            "removed_file_count": removed_files,
        }.items()
        if key != "plans"
    }


def validate_release(knowledge_db: Path) -> tuple[dict[str, int], dict[str, set[str]]]:
    connection = sqlite3.connect(f"{knowledge_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        counts = {
            "campaign_delivery_provenance": connection.execute(
                "SELECT COUNT(*) FROM campaign_delivery_provenance"
            ).fetchone()[0],
            "source_tracks": connection.execute("SELECT COUNT(*) FROM source_track").fetchone()[0],
            "source_links": connection.execute(
                "SELECT COUNT(*) FROM source_track WHERE source_url IS NOT NULL AND trim(source_url) <> ''"
            ).fetchone()[0],
        }
        lyric_counts = connection.execute(
            """
            SELECT COUNT(*) AS canonical_recordings,
                   COALESCE(SUM(CASE WHEN rl.status = 'available' THEN 1 ELSE 0 END), 0) AS lyrics_available,
                   COALESCE(SUM(CASE WHEN rl.status = 'instrumental' THEN 1 ELSE 0 END), 0) AS lyrics_instrumental,
                   COALESCE(SUM(CASE WHEN rl.status = 'platform_unavailable' THEN 1 ELSE 0 END), 0) AS lyrics_platform_unavailable,
                   COALESCE(SUM(CASE WHEN rl.status = 'pending' THEN 1 ELSE 0 END), 0) AS lyrics_pending,
                   COALESCE(SUM(CASE WHEN rl.recording_id IS NULL THEN 1 ELSE 0 END), 0) AS lyrics_missing
            FROM recording r
            LEFT JOIN recording_lyric rl ON rl.recording_id = r.id
            WHERE r.canonical_analysis_id IS NOT NULL
            """
        ).fetchone()
        lyric_count_keys = (
            "canonical_recordings",
            "lyrics_available",
            "lyrics_instrumental",
            "lyrics_platform_unavailable",
            "lyrics_pending",
            "lyrics_missing",
        )
        counts.update(
            {
                key: int(value or 0)
                for key, value in zip(lyric_count_keys, lyric_counts, strict=True)
            }
        )
        counts["lyrics_unresolved"] = (
            counts["canonical_recordings"]
            - counts["lyrics_available"]
            - counts["lyrics_instrumental"]
            - counts["lyrics_platform_unavailable"]
        )
        source_ids: dict[str, set[str]] = {}
        for source_name, source_track_id in connection.execute(
            "SELECT lower(source_name), source_track_id FROM source_track"
        ):
            source_ids.setdefault(str(source_name), set()).add(str(source_track_id))
    except sqlite3.Error as exc:
        raise RuntimeError(f"知识库缺少可验证的链接/来源表，不能删除音频: {exc}") from exc
    finally:
        connection.close()
    normalized = {key: int(value) for key, value in counts.items()}
    if normalized["campaign_delivery_provenance"] <= 0:
        raise RuntimeError("知识库没有 campaign delivery provenance，不能删除音频")
    if normalized["source_tracks"] <= 0 or normalized["source_links"] != normalized["source_tracks"]:
        raise RuntimeError(
            "知识库 source link 不完整，不能删除音频: "
            f"source_tracks={normalized['source_tracks']} source_links={normalized['source_links']}"
        )
    if normalized["lyrics_unresolved"]:
        raise RuntimeError(
            "知识库歌词覆盖不完整，不能删除音频: "
            f"canonical_recordings={normalized['canonical_recordings']} "
            f"lyrics_available={normalized['lyrics_available']} "
            f"lyrics_instrumental={normalized['lyrics_instrumental']} "
            f"lyrics_platform_unavailable={normalized['lyrics_platform_unavailable']} "
            f"lyrics_pending={normalized['lyrics_pending']} "
            f"lyrics_missing={normalized['lyrics_missing']} "
            f"lyrics_unresolved={normalized['lyrics_unresolved']}"
        )
    return normalized, source_ids


def source_track_is_analyzed(song: dict[str, Any], source_ids: dict[str, set[str]]) -> bool:
    platform = str(song.get("platform") or "kugou").strip().lower()
    raw_id = str(song.get("platform_track_key") or "").strip()
    identity_key = str(song.get("identity_key") or "").strip()
    if not raw_id and ":" in identity_key:
        raw_id = identity_key.split(":", 1)[1]
    candidates = {identity_key, raw_id}
    if raw_id:
        candidates.update({f"{platform}-{raw_id}", f"{platform}:{raw_id}"})
    return any(candidate in source_ids.get(platform, set()) for candidate in candidates if candidate)


def prune(
    inventory_path: Path,
    audio_root: Path,
    knowledge_db: Path,
    expected_count: int,
    confirm: bool,
    *,
    delivery_path: Path | None = None,
    campaign_staging_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    songs = [song for song in inventory.get("songs", []) if isinstance(song, dict)]
    if len(songs) != expected_count:
        raise RuntimeError(f"inventory 歌曲数为 {len(songs)}，不是预期的 {expected_count}")
    release_counts, source_ids = validate_release(knowledge_db)
    delivery_staging = _delivery_staging_cleanup_plan(delivery_path, campaign_staging_paths)
    root = audio_root.expanduser().resolve()
    present_paths: dict[int, Path] = {}
    directory_owners: dict[Path, set[int]] = {}
    for index, song in enumerate(songs):
        relative_path = str(song.get("download", {}).get("path") or "").strip()
        if not relative_path:
            continue
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"inventory 音频路径越界，拒绝删除: {path}") from exc
        if path.is_file():
            present_paths[index] = path
            directory_owners.setdefault(path.parent, set()).add(index)

    eligible_indexes = {
        index
        for index, song in enumerate(songs)
        if index in present_paths
        and song.get("download", {}).get("status") == "downloaded"
        and source_track_is_analyzed(song, source_ids)
    }
    retained_indexes = set(present_paths) - eligible_indexes
    already_purged_indexes = {
        index
        for index, song in enumerate(songs)
        if index not in present_paths
        and song.get("download", {}).get("retention") == "purged_after_analysis"
    }
    selected_directories = {
        directory
        for directory, owners in directory_owners.items()
        if owners and owners <= eligible_indexes and directory != root
    }
    selected_files = {
        present_paths[index]
        for index in eligible_indexes
        if present_paths[index].parent not in selected_directories
    }
    directory_files = {
        path
        for directory in selected_directories
        for path in directory.rglob("*")
        if path.is_file()
    }
    deletion_files = directory_files | selected_files
    bytes_total = sum(path.stat().st_size for path in deletion_files)
    summary: dict[str, Any] = {
        "inventory": str(inventory_path.resolve()),
        "audio_root": str(root),
        "knowledge_db": str(knowledge_db.resolve()),
        "song_count": len(songs),
        "eligible_song_count": len(eligible_indexes),
        "retained_song_count": len(retained_indexes),
        "already_purged_song_count": len(already_purged_indexes),
        "selected_directory_count": len(selected_directories),
        "selected_audio_file_count": len(eligible_indexes),
        "file_count": len(deletion_files),
        "bytes": bytes_total,
        "release_counts": release_counts,
        "delivery_staging": {
            key: value for key, value in delivery_staging.items() if key != "plans"
        },
        "dry_run": not confirm,
    }
    if not confirm:
        return summary

    for directory in sorted(selected_directories, key=lambda path: len(path.parts), reverse=True):
        shutil.rmtree(directory)
    for path in selected_files:
        path.unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    deleted_at = now_iso()
    for index in sorted(eligible_indexes):
        song = songs[index]
        download = song.setdefault("download", {})
        download.update(
            {
                "status": "downloaded",
                "retention": "purged_after_analysis",
                "file_present": False,
                "exists": False,
                "purged_at": deleted_at,
                "purged_reason": "audio no longer needed after knowledge-base import",
            }
        )
    inventory["generated_at"] = deleted_at
    inventory["audio_retention"] = (
        "partial_purge_after_analysis" if retained_indexes else "purged_after_analysis"
    )
    inventory["audio_files_deleted_at"] = deleted_at
    atomic_write_json(inventory_path, inventory)
    summary["delivery_staging"] = _remove_delivery_staging_audio(delivery_staging)
    summary.update(
        {
            "dry_run": False,
            "deleted_at": deleted_at,
            "inventory_updated": True,
            "remaining_present_song_count": len(retained_indexes),
        }
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--knowledge-db", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--delivery", type=Path)
    parser.add_argument(
        "--campaign-staging",
        type=Path,
        action="append",
        default=[],
        help="Local campaign staging directory that must exactly bind to --delivery before audio is removed",
    )
    parser.add_argument("--confirm-delete-audio", action="store_true")
    args = parser.parse_args()
    result = prune(
        args.inventory.expanduser(),
        args.audio_root.expanduser(),
        args.knowledge_db.expanduser(),
        args.expected_count,
        args.confirm_delete_audio,
        delivery_path=args.delivery.expanduser() if args.delivery else None,
        campaign_staging_paths=[path.expanduser() for path in args.campaign_staging],
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
