from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from .campaign_delivery import load_campaign_delivery_file
from .distribution import publish_snapshot
from .errors import DatabaseNotInitializedError, MusicKBError
from .repository import MusicKBRepository, iter_import_file
from .schema import SCHEMA_VERSION, initialize_database
from .snapshot import create_snapshot, install_snapshot, verify_snapshot
from .workflow import run_weekly_update
from .weekly_orchestration import run_weekly_run


def default_client_database() -> Path:
    return Path(os.environ.get("MUSIC_KB_DB", "~/.music-kb/current.sqlite")).expanduser()


def default_peer_inventory() -> Path:
    return Path(os.environ.get("MUSIC_KB_PEERS_FILE", "~/.config/music-kb/peers.toml")).expanduser()


def default_operations_file() -> Path:
    return Path(__file__).resolve().parents[2] / "references" / "validated-operations.json"


def default_chart_profile() -> Path:
    return Path(__file__).resolve().parents[2] / "references" / "kugou-chart-profile.json"


def default_cnb_storage_policy() -> Path:
    return Path(__file__).resolve().parents[2] / "references" / "cnb-storage-policy.json"


def _add_database_argument(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        required=required,
        default=None if required else default_client_database(),
        help="SQLite database path (default: $MUSIC_KB_DB or ~/.music-kb/current.sqlite)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-kb",
        description="Manage a local Music Flamingo SQLite knowledge base and safe client snapshots.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit stable JSON output")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create or initialize a writable publisher database")
    _add_database_argument(init, required=True)

    doctor = commands.add_parser("doctor", help="Inspect a local database without writing")
    _add_database_argument(doctor)

    importer = commands.add_parser("import-analysis", help="Import one JSON/JSONL Music Flamingo analysis")
    _add_database_argument(importer, required=True)
    importer.add_argument("--input", type=Path, required=True, help="JSON object, array, or JSONL input file")
    importer.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Bounded generic import transaction size (1-5000; default: 500); .jsonl/.ndjson streams",
    )

    rebuild_search = commands.add_parser(
        "rebuild-search",
        help="Rebuild all publisher FTS projections after an interrupted batch import",
    )
    _add_database_argument(rebuild_search, required=True)

    campaign_importer = commands.add_parser(
        "import-campaign-delivery",
        help="Import a strict LF JSONL canonical KuGou/Music Flamingo delivery",
    )
    _add_database_argument(campaign_importer, required=True)
    campaign_importer.add_argument(
        "--input", type=Path, required=True, help="Verified canonical campaign delivery JSONL"
    )
    campaign_importer.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help="Optionally require an exact entry count (use 927 for the full campaign)",
    )

    enricher = commands.add_parser(
        "enrich-campaign-tags",
        help="Derive fine-grained deterministic tags for current campaign canonical analyses",
    )
    _add_database_argument(enricher, required=True)
    enricher.add_argument(
        "--dry-run",
        action="store_true",
        help="Report tag/feature coverage without changing the publisher database",
    )
    enricher.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Bounded publisher backfill transaction size (1-5000; default: 500)",
    )

    validator = commands.add_parser("validate", help="Validate canonical and search invariants")
    _add_database_argument(validator, required=True)

    links = commands.add_parser(
        "backfill-source-links",
        help="Backfill public listening URLs from the authoritative Kugou chart database",
    )
    _add_database_argument(links, required=True)
    links.add_argument("--chart-db", type=Path, required=True, help="Kugou chart SQLite database")

    search = commands.add_parser("search", help="Search canonical analyses, title/artist aliases, and exact tags")
    _add_database_argument(search)
    search.add_argument("--query", default="", help="Full-text or generic alias query")
    search.add_argument("--tag", action="append", default=[], help="Exact tag or alias; repeat for AND matching")
    search.add_argument("--title", default="", help="Title/alias filter")
    search.add_argument("--artist", default="", help="Artist/alias filter")
    search.add_argument("--limit", type=int, default=10, help="Maximum records (bounded to 50)")

    getter = commands.add_parser("get", help="Fetch one public canonical analysis")
    _add_database_argument(getter)
    getter.add_argument("recording_id")
    getter.add_argument("--max-chars", type=int, default=24_000)

    snapshot = commands.add_parser("snapshot", help="Create, verify, or install immutable local snapshots")
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    create = snapshot_commands.add_parser("create", help="Build canonical-only release snapshot")
    _add_database_argument(create, required=True)
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--name", default=None, help="Release name, e.g. music-kb-2026w29")
    verify = snapshot_commands.add_parser("verify", help="Verify release manifest and SHA-256")
    verify.add_argument("--manifest", type=Path, required=True)
    install = snapshot_commands.add_parser("install", help="Verify and atomically install a local release")
    install.add_argument("--release-dir", type=Path, required=True)
    install.add_argument("--target-dir", type=Path, default=Path("~/.music-kb").expanduser())

    publish = commands.add_parser(
        "publish", help="Safely distribute an immutable release to private SSH peers"
    )
    publish_commands = publish.add_subparsers(dest="publish_command", required=True)
    push = publish_commands.add_parser(
        "push", help="Stage, verify, and atomically install one release on each configured peer"
    )
    push.add_argument("--release-dir", type=Path, required=True, help="Verified immutable release directory")
    push.add_argument(
        "--peers-file",
        type=Path,
        required=False,
        default=default_peer_inventory(),
        help="Private TOML peer inventory; do not commit this file",
    )
    push.add_argument(
        "--peer",
        action="append",
        default=[],
        help="Only publish to this peer name; repeat to target multiple peers",
    )
    push.add_argument("--dry-run", action="store_true", help="Validate inputs and show the per-peer plan only")

    weekly = commands.add_parser(
        "weekly-update", help="Import a completed delivery, build a release, and optionally publish it"
    )
    _add_database_argument(weekly, required=True)
    weekly.add_argument("--input", type=Path, required=True, help="Completed canonical delivery JSON/JSONL")
    weekly.add_argument(
        "--input-kind",
        choices=("campaign", "generic"),
        default="campaign",
        help="Input contract (default: campaign)",
    )
    weekly.add_argument("--expected-count", type=int, default=None)
    weekly.add_argument("--batch-size", type=int, default=500)
    weekly.add_argument("--output-dir", type=Path, required=True)
    weekly.add_argument("--release-name", default=None)
    weekly.add_argument("--peers-file", type=Path, default=default_peer_inventory())
    weekly.add_argument("--peer", action="append", default=[])
    weekly.add_argument(
        "--publish",
        action="store_true",
        help="Actually push the verified release; otherwise only produce a dry-run plan",
    )
    weekly.add_argument(
        "--state-file",
        type=Path,
        default=Path("~/.music-kb/state/publish-state.json").expanduser(),
    )

    weekly_run = commands.add_parser(
        "weekly-run",
        help="Run the complete resumable chart-to-publisher weekly workflow",
    )
    weekly_run.add_argument("--workspace", type=Path, default=Path.cwd())
    weekly_run.add_argument("--run-id", required=True)
    weekly_run.add_argument("--rank-id", action="append", default=[], help="Kugou rank ID; repeat for multiple charts")
    weekly_run.add_argument("--chart-page", type=int, default=1)
    weekly_run.add_argument("--chart-size", type=int, default=100)
    weekly_run.add_argument(
        "--db",
        type=Path,
        default=Path("~/.music-kb/music-master.sqlite").expanduser(),
        help="Writable publisher master database",
    )
    weekly_run.add_argument("--inventory", type=Path, default=Path("data/song_inventory.json"))
    weekly_run.add_argument("--audio-root", type=Path, default=Path("music_downloads/KugouMusicClient"))
    weekly_run.add_argument("--legacy-progress", type=Path, default=Path("download_progress.json"))
    weekly_run.add_argument("--operations-file", type=Path, default=default_operations_file())
    weekly_run.add_argument(
        "--chart-profile",
        type=Path,
        default=default_chart_profile(),
        help="Versioned full-chart profile used when --rank-id is omitted",
    )
    weekly_run.add_argument("--output-dir", type=Path, default=Path("~/.music-kb/releases").expanduser())
    weekly_run.add_argument("--release-name", default=None)
    weekly_run.add_argument("--peers-file", type=Path, default=default_peer_inventory())
    weekly_run.add_argument("--peer", action="append", default=[])
    weekly_run.add_argument("--publish", action="store_true")
    weekly_run.add_argument(
        "--skip-peers",
        action="store_true",
        help="Explicitly skip peer sync; with --publish, cleanup still requires both confirmation flags",
    )
    weekly_run.add_argument("--delivery", type=Path, help="Existing CNB canonical delivery JSONL")
    weekly_run.add_argument("--cnb-command", help="Command that writes $MUSIC_KB_CNB_OUTPUT canonical JSONL")
    weekly_run.add_argument("--chart-database", type=Path, help="Authoritative chart SQLite used to backfill source links")
    weekly_run.add_argument("--state-file", type=Path, default=Path("~/.music-kb/state/publish-state.json").expanduser())
    weekly_run.add_argument("--proxy")
    weekly_run.add_argument("--download-dry-run", action="store_true")
    weekly_run.add_argument("--download-max-items", type=int)
    weekly_run.add_argument("--confirm-delete-audio", action="store_true")
    weekly_run.add_argument("--cnb-storage-policy", type=Path, default=default_cnb_storage_policy())
    weekly_run.add_argument(
        "--cnb-transport",
        choices=("lfs", "git-objects"),
        default="lfs",
        help="CNB campaign audio transport; git-objects is the bounded no-cost fallback for pending orphan LFS GC",
    )
    weekly_run.add_argument("--confirm-delete-cnb-storage", action="store_true")
    weekly_run.add_argument("--expected-count", type=int)
    weekly_run.add_argument("--timeout-seconds", type=int, default=86_400)

    return parser


def _result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def _error(error: MusicKBError, *, as_json: bool) -> None:
    payload = {"ok": False, "error": {"code": error.code, "message": str(error), "details": error.details}}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    else:
        print(f"error [{error.code}]: {error}", file=sys.stderr)
        if error.details:
            print(json.dumps(error.details, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)


def _with_repository(path: Path, *, read_only: bool, operation: Callable[[MusicKBRepository], dict[str, Any]]) -> dict[str, Any]:
    with MusicKBRepository(path, read_only=read_only) as repository:
        return operation(repository)


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.command == "init":
        database = initialize_database(args.db)
        return 0, {"initialized": True, "database": str(database), "schema_version": SCHEMA_VERSION}
    if args.command == "doctor":
        try:
            return 0, _with_repository(args.db, read_only=True, operation=lambda repo: repo.status())
        except DatabaseNotInitializedError as exc:
            return 1, {
                "ready": False,
                "database": str(Path(args.db).expanduser()),
                "reason": str(exc),
                "expected_default": str(default_client_database()),
            }
    if args.command == "import-analysis":
        return 0, _with_repository(
            args.db,
            read_only=False,
            operation=lambda repo: repo.import_analyses(
                iter_import_file(args.input), batch_size=args.batch_size
            ),
        )
    if args.command == "rebuild-search":
        return 0, _with_repository(
            args.db,
            read_only=False,
            operation=lambda repo: {"recording_count": repo.rebuild_all_search_projections()},
        )
    if args.command == "import-campaign-delivery":
        entries = load_campaign_delivery_file(args.input, expected_count=args.expected_count)
        return 0, _with_repository(
            args.db,
            read_only=False,
            operation=lambda repo: repo.import_campaign_delivery(entries),
        )
    if args.command == "enrich-campaign-tags":
        return 0, _with_repository(
            args.db,
            read_only=False,
            operation=lambda repo: repo.enrich_campaign_tags(
                dry_run=args.dry_run, batch_size=args.batch_size
            ),
        )
    if args.command == "validate":
        result = _with_repository(args.db, read_only=True, operation=lambda repo: repo.validate())
        return (0 if result["valid"] else 1), result
    if args.command == "backfill-source-links":
        return 0, _with_repository(
            args.db,
            read_only=False,
            operation=lambda repo: repo.backfill_source_links(args.chart_db),
        )
    if args.command == "search":
        result = _with_repository(
            args.db,
            read_only=True,
            operation=lambda repo: {
                "results": repo.search(
                    query=args.query,
                    tags=args.tag,
                    title=args.title,
                    artist=args.artist,
                    limit=args.limit,
                )
            },
        )
        result["count"] = len(result["results"])
        return 0, result
    if args.command == "get":
        return 0, _with_repository(
            args.db,
            read_only=True,
            operation=lambda repo: repo.get_canonical_analysis(args.recording_id, max_chars=args.max_chars),
        )
    if args.command == "snapshot":
        if args.snapshot_command == "create":
            return 0, create_snapshot(args.db, args.output_dir, release_name=args.name)
        if args.snapshot_command == "verify":
            return 0, verify_snapshot(args.manifest)
        if args.snapshot_command == "install":
            return 0, install_snapshot(args.release_dir, args.target_dir)
    if args.command == "publish":
        if args.publish_command == "push":
            result = publish_snapshot(
                args.release_dir,
                args.peers_file,
                peer_names=args.peer,
                dry_run=args.dry_run,
            )
            return (0 if result["failed_count"] == 0 else 1), result
    if args.command == "weekly-update":
        result = run_weekly_update(
            database=args.db,
            input_path=args.input,
            input_kind=args.input_kind,
            expected_count=args.expected_count,
            batch_size=args.batch_size,
            output_dir=args.output_dir,
            release_name=args.release_name,
            peers_file=args.peers_file,
            peer_names=args.peer,
            publish=args.publish,
            state_file=args.state_file,
        )
        return (0 if result["publish"]["failed_count"] == 0 else 1), result
    if args.command == "weekly-run":
        result = run_weekly_run(
            workspace=args.workspace,
            run_id=args.run_id,
            rank_ids=args.rank_id,
            chart_page=args.chart_page,
            chart_size=args.chart_size,
            chart_profile=args.chart_profile,
            database=args.db,
            inventory=args.inventory,
            audio_root=args.audio_root,
            legacy_progress=args.legacy_progress,
            operations_file=args.operations_file,
            output_dir=args.output_dir,
            release_name=args.release_name,
            peers_file=args.peers_file,
            peer_names=args.peer,
            publish=args.publish,
            delivery=args.delivery,
            cnb_command=args.cnb_command,
            chart_database=args.chart_database,
            state_file=args.state_file,
            proxy=args.proxy,
            download_dry_run=args.download_dry_run,
            download_max_items=args.download_max_items,
            confirm_delete_audio=args.confirm_delete_audio,
            expected_count=args.expected_count,
            timeout_seconds=args.timeout_seconds,
            skip_peers=args.skip_peers,
            cnb_storage_policy=args.cnb_storage_policy,
            confirm_delete_cnb_storage=args.confirm_delete_cnb_storage,
            cnb_transport=args.cnb_transport,
        )
        return 0, result
    raise AssertionError("unhandled command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, result = run(args)
        if args.command in {"doctor", "publish", "weekly-update"} and code == 1:
            # Missing client configuration and per-peer publish failures are
            # normal result states, not unstructured exceptions. Keep their
            # JSON shape machine-readable while preserving a non-zero status.
            if args.as_json:
                print(json.dumps({"ok": False, "result": result}, ensure_ascii=False, sort_keys=True))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return code
        _result(result, as_json=args.as_json)
        return code
    except MusicKBError as error:
        _error(error, as_json=args.as_json)
        return 2
    except (OSError, ValueError) as error:
        wrapped = MusicKBError(str(error))
        _error(wrapped, as_json=args.as_json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
