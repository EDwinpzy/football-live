"""Versioned publishing for the metadata database (full, delta, manifest)."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .builder import sha256_file

COMPRESSION = "gzip"
# Version stamps and the daily refresh schedule follow the operator's clock.
VERSION_TIMEZONE = timezone(timedelta(hours=8))
DEFAULT_KEEP_FULL = 4
MANIFEST_NAME = "manifest.json"

# Primary keys used to diff two database versions.  Tables without a key are
# rebuilt wholesale inside the delta.
DELTA_KEYS: dict[str, tuple[str, ...]] = {
    "media": ("id",),
    "external_id": ("provider", "external_id"),
    "media_title": ("id",),
    "person": ("id",),
    "credit": ("media_id", "person_id", "department", "role"),
    "genre": ("id",),
    "media_genre": ("media_id", "genre_id"),
    "region": ("id",),
    "media_region": ("media_id", "region_id"),
    "rating": ("media_id", "provider"),
    "image": ("media_id", "image_type"),
    "sync_state": ("media_id",),
    "search_doc": ("media_id",),
    "review_queue": ("left_key", "right_key"),
    "meta": ("key",),
}
DELTA_REBUILD = ("id_allocator",)


def _gzip_deterministic(source: Path, target: Path, compresslevel: int = 9) -> None:
    """gzip without a timestamp or filename so identical input gives identical output."""
    with open(source, "rb") as reader, open(target, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=compresslevel, mtime=0) as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)


def next_data_version(now=None, previous_manifest=None) -> str:
    # A fixed offset keeps the version independent of where the builder runs.
    stamp = datetime.fromtimestamp(now or time.time(), VERSION_TIMEZONE).strftime("%Y%m%d")
    latest = ""
    if isinstance(previous_manifest, dict):
        latest = str(previous_manifest.get("latest_version") or "")
    if latest.startswith(stamp) and latest[8:].isdigit():
        return f"{stamp}{int(latest[8:]) + 1:02d}"
    return f"{stamp}01"


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _rows(connection: sqlite3.Connection, table: str) -> tuple[list[str], dict[tuple, tuple]]:
    cursor = connection.execute(f"SELECT * FROM {table}")
    columns = [item[0] for item in cursor.description]
    key_columns = DELTA_KEYS[table]
    indexes = [columns.index(name) for name in key_columns]
    rows: dict[tuple, tuple] = {}
    for row in cursor:
        key = tuple(row[index] for index in indexes)
        rows[key] = tuple(row)
    return columns, rows


def build_delta(previous_db, current_db, target: Path) -> dict:
    """Write a compressed SQL delta that turns previous_db into current_db."""
    previous = sqlite3.connect(f"file:{Path(previous_db)}?mode=ro", uri=True)
    current = sqlite3.connect(f"file:{Path(current_db)}?mode=ro", uri=True)
    statements: list[str] = ["PRAGMA foreign_keys = OFF;", "BEGIN IMMEDIATE;"]
    changed_rows = 0
    counts: dict[str, int] = {}
    try:
        for table, key_columns in DELTA_KEYS.items():
            try:
                columns, old_rows = _rows(previous, table)
                _, new_rows = _rows(current, table)
            except sqlite3.DatabaseError:
                continue
            insert_prefix = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ("
            table_changes = 0
            for key, row in new_rows.items():
                if old_rows.get(key) == row:
                    continue
                statements.append(insert_prefix + ", ".join(_literal(value) for value in row) + ");")
                table_changes += 1
            for key in old_rows:
                if key in new_rows:
                    continue
                where_clause = " AND ".join(
                    f"{name} = {_literal(value)}" for name, value in zip(key_columns, key)
                )
                statements.append(f"DELETE FROM {table} WHERE {where_clause};")
                table_changes += 1
            if table_changes:
                counts[table] = table_changes
                changed_rows += table_changes

        for table in DELTA_REBUILD:
            try:
                cursor = current.execute(f"SELECT * FROM {table}")
            except sqlite3.DatabaseError:
                continue
            columns = [item[0] for item in cursor.description]
            rows = list(cursor)
            statements.append(f"DELETE FROM {table};")
            prefix = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ("
            for row in rows:
                statements.append(prefix + ", ".join(_literal(value) for value in row) + ");")
            counts[table] = len(rows)

        # FTS5 rows cannot be diffed by primary key, so refresh the entries whose
        # search document changed.
        changed_media = _changed_media_ids(previous, current)
        if changed_media:
            statements.append("DELETE FROM search_fts WHERE media_id IN (%s);" % ", ".join(str(int(i)) for i in changed_media))
            cursor = current.execute(
                "SELECT media_id, title, aliases, pinyin, initials, people, genres FROM search_fts"
            )
            prefix = "INSERT INTO search_fts (media_id, title, aliases, pinyin, initials, people, genres) VALUES ("
            wanted = set(changed_media)
            for row in cursor:
                if int(row[0]) not in wanted:
                    continue
                statements.append(prefix + ", ".join(_literal(value) for value in row) + ");")
            counts["search_fts"] = len(changed_media)
    finally:
        previous.close()
        current.close()
    statements.append("COMMIT;")
    payload = ("\n".join(statements) + "\n").encode("utf-8")
    with open(target, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as writer:
            writer.write(payload)
    return {"changed_rows": changed_rows, "tables": counts}


def _changed_media_ids(previous: sqlite3.Connection, current: sqlite3.Connection) -> list[int]:
    try:
        _, old_rows = _rows(previous, "search_doc")
        _, new_rows = _rows(current, "search_doc")
    except sqlite3.DatabaseError:
        return []
    changed = [key[0] for key, row in new_rows.items() if old_rows.get(key) != row]
    changed.extend(key[0] for key in old_rows if key not in new_rows)
    return sorted({int(value) for value in changed})


@dataclass
class PublishReport:
    version: str
    public_dir: str
    files: list[dict] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    delta: dict | None = None
    pruned: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def read_manifest(public_dir) -> dict | None:
    path = Path(public_dir) / MANIFEST_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def publish(
    database_path,
    public_dir,
    metadata_dir,
    version: str,
    public_base: str,
    previous_db=None,
    previous_version: str | None = None,
    keep_full: int = DEFAULT_KEEP_FULL,
    now=None,
) -> PublishReport:
    now = int(now if now is not None else time.time())
    database_path = Path(database_path)
    public_dir = Path(public_dir)
    metadata_dir = Path(metadata_dir)
    public_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "db").mkdir(parents=True, exist_ok=True)

    base = public_base.rstrip("/")
    staging = public_dir / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    files: list[dict] = []
    try:
        full_name = f"metadata-v{version}.sqlite.gz"
        full_path = staging / full_name
        _gzip_deterministic(database_path, full_path)
        full_entry = {
            "version": version,
            "url": f"{base}/{full_name}",
            "file": full_name,
            "sha256": sha256_file(full_path),
            "size": full_path.stat().st_size,
            "compression": COMPRESSION,
            "uncompressed_sha256": sha256_file(database_path),
            "uncompressed_size": database_path.stat().st_size,
        }
        files.append(full_entry)

        deltas: list[dict] = []
        delta_report = None
        if previous_db and Path(previous_db).exists() and previous_version and str(previous_version) != str(version):
            delta_name = f"delta-{previous_version}-{version}.sql.gz"
            delta_path = staging / delta_name
            delta_report = build_delta(previous_db, database_path, delta_path)
            deltas.append({
                "from": str(previous_version),
                "to": str(version),
                "url": f"{base}/{delta_name}",
                "file": delta_name,
                "sha256": sha256_file(delta_path),
                "size": delta_path.stat().st_size,
                "compression": COMPRESSION,
                "changed_rows": delta_report["changed_rows"],
                "tables": delta_report["tables"],
            })
            files.append(deltas[-1])

        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("media", "external_id", "media_title", "person", "credit", "genre",
                              "region", "rating", "image", "search_doc")
            }
            generated_at = connection.execute("SELECT value FROM meta WHERE key = 'generated_at'").fetchone()
            source_version = connection.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
        finally:
            connection.close()

        manifest = {
            "schema": SCHEMA_VERSION,
            "latest_version": str(version),
            "generated_at": int(generated_at[0]) if generated_at else now,
            "source_data_version": source_version[0] if source_version else str(version),
            "published_at": now,
            "counts": counts,
            "full": full_entry,
            "deltas": deltas,
            "compatibility": {
                "external_id_provider": "douban",
                "legacy_reference": "douban:<douban_id>",
                "mytv_id_base": 10000001,
            },
        }

        published: list[dict] = []
        for entry in files:
            target = public_dir / entry["file"]
            os.replace(staging / entry["file"], target)
            published.append(entry)

        manifest_path = public_dir / MANIFEST_NAME
        manifest_tmp = staging / MANIFEST_NAME
        manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        os.replace(manifest_tmp, manifest_path)

        latest = metadata_dir / "db" / "metadata-latest.sqlite"
        latest_tmp = metadata_dir / "db" / f".metadata-latest-{uuid.uuid4().hex}"
        shutil.copyfile(database_path, latest_tmp)
        os.replace(latest_tmp, latest)
        shutil.copyfile(database_path, metadata_dir / "db" / f"metadata-v{version}.sqlite")

        pruned = _prune(public_dir, metadata_dir, keep_full=keep_full)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return PublishReport(version=str(version), public_dir=str(public_dir), files=published,
                         manifest=manifest, delta=delta_report, pruned=pruned)


def _prune(public_dir: Path, metadata_dir: Path, keep_full: int) -> list[str]:
    """Keep the newest N full packages plus every delta that still leads to them."""
    fulls = sorted(
        (path for path in public_dir.glob("metadata-v*.sqlite.gz")),
        key=lambda path: path.name,
    )
    removed: list[str] = []
    keep_names = {path.name for path in fulls[-keep_full:]} if keep_full > 0 else {path.name for path in fulls}
    kept_versions: set[str] = set()
    for name in keep_names:
        kept_versions.add(name[len("metadata-v"):-len(".sqlite.gz")])
    for path in fulls:
        if path.name in keep_names:
            continue
        path.unlink(missing_ok=True)
        removed.append(path.name)
    for path in public_dir.glob("delta-*.sql.gz"):
        parts = path.name[len("delta-"):-len(".sql.gz")].split("-")
        if len(parts) == 2 and parts[1] in kept_versions:
            continue
        path.unlink(missing_ok=True)
        removed.append(path.name)
    workers = sorted((metadata_dir / "db").glob("metadata-v*.sqlite"), key=lambda path: path.name)
    for path in workers[:-keep_full] if keep_full > 0 else []:
        path.unlink(missing_ok=True)
        removed.append(f"db/{path.name}")
    builds = sorted((metadata_dir / "build").glob("metadata-v*.sqlite"), key=lambda path: path.name)
    for path in builds[:-2]:
        path.unlink(missing_ok=True)
        removed.append(f"build/{path.name}")
    return removed


def verify_publication(public_dir, expect_files: bool = True) -> dict:
    """Re-read the published manifest and check every referenced file."""
    public_dir = Path(public_dir)
    manifest = read_manifest(public_dir)
    if not manifest:
        raise FileNotFoundError(f"manifest missing under {public_dir}")
    problems: list[str] = []
    checked: list[dict] = []
    entries = [manifest.get("full"), *(manifest.get("deltas") or [])]
    for entry in entries:
        if not entry:
            continue
        path = public_dir / entry["file"]
        if not path.exists():
            if expect_files:
                problems.append(f"missing {entry['file']}")
            continue
        size = path.stat().st_size
        digest = sha256_file(path)
        if size != entry["size"]:
            problems.append(f"{entry['file']} size {size} != {entry['size']}")
        if digest != entry["sha256"]:
            problems.append(f"{entry['file']} sha256 mismatch")
        checked.append({"file": entry["file"], "size": size, "sha256": digest})
    full = manifest.get("full") or {}
    if full.get("file"):
        full_path = public_dir / full["file"]
        if full_path.exists():
            digest = hashlib.sha256()
            try:
                with gzip.open(full_path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except (OSError, EOFError) as error:
                problems.append(f"{full['file']} is not readable gzip: {error}")
            else:
                if full.get("uncompressed_sha256") and digest.hexdigest() != full["uncompressed_sha256"]:
                    problems.append("decompressed full package sha256 mismatch")
    return {"manifest_version": manifest.get("latest_version"), "checked": checked, "problems": problems}
