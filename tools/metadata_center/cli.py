"""Command line entry point for the MyTV metadata centre."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .builder import MetadataBuilder, content_fingerprint
from .providers.douban_snapshot import DoubanSnapshotProvider
from .publish import build_delta, next_data_version, publish, read_manifest, verify_publication
from .read_api import MetadataStore

DEFAULT_METADATA_DIR = "/srv/metadata"
DEFAULT_PUBLIC_DIR = "/srv/objects/hotupdate/metadata"
DEFAULT_SNAPSHOT = "/srv/metadata/snapshots/douban_snapshot.json"


def _report(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _providers(args) -> list[DoubanSnapshotProvider]:
    return [DoubanSnapshotProvider(args.snapshot)]


def _paths(args) -> tuple[Path, Path]:
    return Path(args.metadata_dir), Path(args.public_dir)


def command_build(args) -> int:
    builder = MetadataBuilder()
    report = builder.build(
        _providers(args),
        args.out,
        data_version=args.version,
        previous_db=args.previous,
    )
    _report(report.as_dict())
    return 0 if report.integrity == "ok" else 1


def command_publish(args) -> int:
    report = publish(
        database_path=args.db,
        public_dir=args.public_dir,
        metadata_dir=args.metadata_dir,
        version=args.version,
        public_base=args.public_base,
        previous_db=args.previous_db,
        previous_version=args.previous_version,
        keep_full=args.keep_full,
    )
    payload = report.as_dict()
    payload["verification"] = verify_publication(args.public_dir)
    _report(payload)
    return 0 if not payload["verification"]["problems"] else 1


def command_rebuild(args) -> int:
    metadata_dir, public_dir = _paths(args)
    previous_manifest = read_manifest(public_dir)
    version = args.version or next_data_version(previous_manifest=previous_manifest)
    previous_version = (previous_manifest or {}).get("latest_version")
    previous_db = metadata_dir / "db" / "metadata-latest.sqlite"
    database = metadata_dir / "build" / f"metadata-v{version}.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)

    builder = MetadataBuilder()
    build_report = builder.build(
        _providers(args),
        database,
        data_version=version,
        previous_db=str(previous_db) if previous_db.exists() else None,
    )
    payload = {"version": version, "previous_version": previous_version, "build": build_report.as_dict()}
    if build_report.integrity != "ok":
        payload["result"] = "build_failed"
        _report(payload)
        return 1

    previous_fingerprint = content_fingerprint(previous_db) if previous_db.exists() else ""
    current_fingerprint = content_fingerprint(database)
    payload["fingerprint"] = {"previous": previous_fingerprint, "current": current_fingerprint}
    if previous_fingerprint and previous_fingerprint == current_fingerprint and not args.force_publish:
        payload["result"] = "unchanged"
        payload["note"] = "catalogue content unchanged; nothing published"
        _report(payload)
        return 0

    has_previous = previous_db.exists()
    publish_report = publish(
        database_path=database,
        public_dir=public_dir,
        metadata_dir=metadata_dir,
        version=version,
        public_base=args.public_base,
        previous_db=str(previous_db) if has_previous else None,
        previous_version=previous_version,
        keep_full=args.keep_full,
    )
    payload["publish"] = publish_report.as_dict()
    payload["verification"] = verify_publication(public_dir)
    payload["result"] = "ok" if not payload["verification"]["problems"] else "verification_failed"
    _report(payload)
    return 0 if payload["result"] == "ok" else 1


def command_verify(args) -> int:
    result = verify_publication(args.public_dir)
    _report(result)
    return 0 if not result["problems"] else 1


def command_delta(args) -> int:
    report = build_delta(args.previous_db, args.db, Path(args.out))
    _report(report)
    return 0


def command_search(args) -> int:
    store = MetadataStore(Path(args.metadata_dir), Path(args.public_dir))
    _report({"query": args.query, "items": store.search(args.query, args.limit)})
    return 0


def command_stats(args) -> int:
    metadata_dir, public_dir = _paths(args)
    database = Path(args.db) if args.db else metadata_dir / "db" / "metadata-latest.sqlite"
    if not database.exists():
        _report({"error": "database_missing", "path": str(database)})
        return 1
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'search_fts%' ORDER BY name"
        )]
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        sample = [dict(row) for row in connection.execute(
            "SELECT media_id, title_norm, pinyin, initials FROM search_doc ORDER BY media_id LIMIT 3"
        )]
    finally:
        connection.close()
    _report({
        "database": str(database),
        "size": database.stat().st_size,
        "meta": meta,
        "counts": counts,
        "integrity": integrity,
        "manifest": read_manifest(public_dir),
        "sample": sample,
    })
    return 0 if integrity == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metadata-center", description="MyTV 影视元数据中心")
    parser.add_argument("--metadata-dir", default=os.environ.get("OTV_METADATA_DIR", DEFAULT_METADATA_DIR))
    parser.add_argument("--public-dir", default=os.environ.get("OTV_METADATA_PUBLIC_DIR", DEFAULT_PUBLIC_DIR))
    parser.add_argument("--public-base", default=os.environ.get(
        "OTV_METADATA_PUBLIC_BASE",
        "https://103.236.97.137:35208/v1/storages/object/hotupdate/metadata",
    ))
    parser.add_argument("--snapshot", default=os.environ.get("OTV_METADATA_SNAPSHOT", DEFAULT_SNAPSHOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="构建 metadata.sqlite")
    build.add_argument("--out", required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--previous", default=None)
    build.set_defaults(func=command_build)

    publish_parser = subparsers.add_parser("publish", help="发布数据库与 manifest")
    publish_parser.add_argument("--db", required=True)
    publish_parser.add_argument("--version", required=True)
    publish_parser.add_argument("--previous-db", default=None)
    publish_parser.add_argument("--previous-version", default=None)
    publish_parser.add_argument("--keep-full", type=int, default=4)
    publish_parser.set_defaults(func=command_publish)

    rebuild = subparsers.add_parser("rebuild", help="构建并发布一个新版本")
    rebuild.add_argument("--version", default=None)
    rebuild.add_argument("--keep-full", type=int, default=4)
    rebuild.add_argument("--force-publish", action="store_true", help="内容未变化也发布新版本")
    rebuild.set_defaults(func=command_rebuild)

    verify = subparsers.add_parser("verify", help="校验已发布产物")
    verify.set_defaults(func=command_verify)

    delta = subparsers.add_parser("delta", help="生成两个版本之间的增量包")
    delta.add_argument("--db", required=True)
    delta.add_argument("--previous-db", required=True)
    delta.add_argument("--out", required=True)
    delta.set_defaults(func=command_delta)

    search = subparsers.add_parser("search", help="用已发布数据库做一次查询")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=command_search)

    stats = subparsers.add_parser("stats", help="查看当前数据库与发布状态")
    stats.add_argument("--db", default=None)
    stats.set_defaults(func=command_stats)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
