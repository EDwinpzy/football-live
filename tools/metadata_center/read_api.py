"""Read-only HTTP API for the published metadata database."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .model import normalize_text

DEFAULT_METADATA_DIR = "/srv/metadata"
DEFAULT_PUBLIC_DIR = "/srv/objects/hotupdate/metadata"
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
SEPARATOR = "\x1f"


class MetadataStore:
    """Serves queries from metadata-latest.sqlite, reopening when it changes."""

    def __init__(self, metadata_dir: Path, public_dir: Path):
        self.metadata_dir = Path(metadata_dir)
        self.public_dir = Path(public_dir)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._stamp: tuple[int, int] | None = None
        self._fts5: bool | None = None

    @property
    def db_path(self) -> Path:
        return self.metadata_dir / "db" / "metadata-latest.sqlite"

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                self._stamp = None

    def connection(self) -> sqlite3.Connection:
        path = self.db_path
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError as error:
            raise FileNotFoundError(f"metadata database not published yet: {path}") from error
        with self._lock:
            if self._connection is not None and self._stamp == stamp:
                return self._connection
            if self._connection is not None:
                self._connection.close()
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            self._connection = connection
            self._stamp = stamp
            self._fts5 = None
            return connection

    def has_fts5(self) -> bool:
        if self._fts5 is None:
            try:
                row = self.connection().execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_fts'"
                ).fetchone()
                self._fts5 = bool(row)
            except (sqlite3.DatabaseError, FileNotFoundError):
                self._fts5 = False
        return bool(self._fts5)

    def meta(self) -> dict:
        try:
            rows = self.connection().execute("SELECT key, value FROM meta").fetchall()
        except (sqlite3.DatabaseError, FileNotFoundError):
            return {}
        return {row["key"]: row["value"] for row in rows}

    def manifest(self) -> dict | None:
        path = self.public_dir / "manifest.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = self.meta()
            if not info:
                return None
            return {
                "schema": int(info.get("schema_version", 0) or 0),
                "latest_version": info.get("data_version"),
                "generated_at": int(info.get("generated_at", 0) or 0),
                "source": "database",
            }

    # -- search ---------------------------------------------------------------
    @staticmethod
    def _fts_tokens(raw: str) -> list[str]:
        tokens: list[str] = []
        for part in re.split(r"\s+", str(raw or "").strip()):
            value = normalize_text(part).replace('"', "")
            if value:
                tokens.append('"' + value + '"*')
        return tokens

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        tokens = self._fts_tokens(query)
        rows: list[sqlite3.Row] = []
        if tokens and self.has_fts5():
            for operator in (" AND ", " OR "):
                try:
                    rows = self.connection().execute(
                        "SELECT search_fts.media_id AS media_id, bm25(search_fts) AS rank"
                        " FROM search_fts WHERE search_fts MATCH ? ORDER BY rank LIMIT ?",
                        (operator.join(tokens), limit),
                    ).fetchall()
                except sqlite3.DatabaseError:
                    rows = []
                if rows:
                    break
        if rows:
            identifiers = [int(row["media_id"]) for row in rows]
            ranked = self._hydrate(identifiers)
            order = {value: index for index, value in enumerate(identifiers)}
            ranked.sort(key=lambda item: order.get(item["id"], len(order)))
            return ranked
        return self._scan_search(normalize_text(query), limit)

    def _hydrate(self, identifiers: list[int]) -> list[dict]:
        if not identifiers:
            return []
        placeholders = ", ".join("?" for _ in identifiers)
        cursor = self.connection().execute(
            f"""
            SELECT m.id, m.title, m.media_type, m.year, m.runtime, m.popularity,
                   (SELECT original_url FROM image WHERE media_id = m.id AND image_type = 'poster') AS poster_url,
                   (SELECT score FROM rating WHERE media_id = m.id ORDER BY provider LIMIT 1) AS rating,
                   (SELECT external_id FROM external_id WHERE media_id = m.id AND provider = 'douban') AS douban_id
            FROM media m WHERE m.id IN ({placeholders})
            """,
            identifiers,
        )
        return [self._row_to_summary(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> dict:
        douban_id = row["douban_id"]
        return {
            "id": row["id"],
            "mytv_id": row["id"],
            "title": row["title"],
            "type": row["media_type"],
            "year": row["year"],
            "runtime": row["runtime"],
            "rating": row["rating"],
            "popularity": row["popularity"],
            "poster_url": row["poster_url"],
            "douban_id": douban_id,
            "legacy_ref": f"douban:{douban_id}" if douban_id else None,
        }

    def _scan_search(self, needle: str, limit: int) -> list[dict]:
        if not needle:
            return []
        scored: list[tuple[float, int]] = []
        cursor = self.connection().execute(
            "SELECT media_id, title_norm, aliases_norm, pinyin, pinyin_alts, initials, initials_alts,"
            " people_norm, people_pinyin, genres_norm, popularity FROM search_doc"
        )
        for row in cursor.fetchall():
            score = self._score_row(row, needle)
            if score > 0:
                scored.append((score, int(row["media_id"])))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return self._hydrate([media_id for _, media_id in scored[:limit]])

    @staticmethod
    def _field_score(value: str, query: str, exact: float, prefix: float, contains: float) -> float:
        parts = [part for part in str(value or "").split(SEPARATOR) if part]
        if not parts:
            return 0.0
        if any(part == query for part in parts):
            return exact
        if any(part.startswith(query) for part in parts):
            return prefix
        if any(query in part for part in parts):
            return contains
        return 0.0

    def _score_row(self, row: sqlite3.Row, needle: str) -> float:
        score = self._field_score(row["title_norm"], needle, 120, 100, 70)
        score = max(score, self._field_score(row["initials"], needle, 106, 93, 62))
        score = max(score, self._field_score(row["pinyin"], needle, 104, 97, 72))
        score = max(score, self._field_score(row["pinyin_alts"], needle, 103, 94, 70))
        score = max(score, self._field_score(row["initials_alts"], needle, 102, 92, 62))
        score = max(score, self._field_score(row["aliases_norm"], needle, 102, 92, 66))
        score = max(score, self._field_score(row["people_norm"], needle, 70, 58, 46))
        score = max(score, self._field_score(row["genres_norm"], needle, 45, 40, 34))
        return score

    # -- detail ---------------------------------------------------------------
    def detail(self, media_id: int) -> dict | None:
        row = self.connection().execute(
            """
            SELECT m.*, (SELECT original_url FROM image WHERE media_id = m.id AND image_type = 'poster') AS poster_url
            FROM media m WHERE m.id = ?
            """,
            (int(media_id),),
        ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["mytv_id"] = payload["id"]
        payload["genres"] = [item["name"] for item in self.connection().execute(
            "SELECT g.name FROM media_genre mg JOIN genre g ON g.id = mg.genre_id"
            " WHERE mg.media_id = ? ORDER BY g.name",
            (payload["id"],),
        )]
        payload["regions"] = [item["name"] for item in self.connection().execute(
            "SELECT r.name FROM media_region mr JOIN region r ON r.id = mr.region_id"
            " WHERE mr.media_id = ? ORDER BY r.name",
            (payload["id"],),
        )]
        payload["external_ids"] = {
            item["provider"]: item["external_id"] for item in self.connection().execute(
                "SELECT provider, external_id FROM external_id WHERE media_id = ? ORDER BY provider",
                (payload["id"],),
            )
        }
        douban_id = payload["external_ids"].get("douban")
        payload["legacy_ref"] = f"douban:{douban_id}" if douban_id else None
        payload["ratings"] = [dict(item) for item in self.connection().execute(
            "SELECT provider, score, vote_count, updated_at FROM rating WHERE media_id = ? ORDER BY provider",
            (payload["id"],),
        )]
        payload["images"] = [dict(item) for item in self.connection().execute(
            "SELECT image_type, original_url, cached_url FROM image WHERE media_id = ? ORDER BY image_type",
            (payload["id"],),
        )]
        return payload

    def credits(self, media_id: int) -> list[dict]:
        return [dict(item) for item in self.connection().execute(
            """
            SELECT c.department, c.role, c.credit_order, p.id AS person_id, p.name
            FROM credit c JOIN person p ON p.id = c.person_id
            WHERE c.media_id = ? ORDER BY c.department, c.credit_order, p.name
            """,
            (int(media_id),),
        )]

    def similar(self, media_id: int, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit or 10), MAX_LIMIT))
        rows = self.connection().execute(
            """
            SELECT mg.media_id, COUNT(*) AS shared
            FROM media_genre mg
            WHERE mg.genre_id IN (SELECT genre_id FROM media_genre WHERE media_id = ?)
              AND mg.media_id != ?
            GROUP BY mg.media_id
            ORDER BY shared DESC, (SELECT popularity FROM media WHERE id = mg.media_id) DESC
            LIMIT ?
            """,
            (int(media_id), int(media_id), limit),
        ).fetchall()
        return self._hydrate([int(row["media_id"]) for row in rows])


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload, cache: str = "no-store") -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", cache)
    handler.end_headers()
    handler.wfile.write(body)


class MetadataHandler(BaseHTTPRequestHandler):
    server_version = "MyTVMetadata/1.0"
    store: MetadataStore

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/metadata"
        query = parse_qs(parsed.query)

        def limit_from(default: int) -> int:
            try:
                return int((query.get("limit") or [default])[0])
            except (TypeError, ValueError):
                return default

        try:
            if path == "/metadata/health":
                info = self.store.meta()
                _json_response(self, 200, {
                    "status": "ok",
                    "data_version": info.get("data_version"),
                    "schema_version": info.get("schema_version"),
                    "generated_at": info.get("generated_at"),
                    "media": int(info.get("media_count", 0) or 0),
                    "fts5": self.store.has_fts5(),
                })
                return
            if path == "/metadata/manifest.json":
                manifest = self.store.manifest()
                if manifest is None:
                    _json_response(self, 404, {"error": "manifest_unavailable"})
                else:
                    _json_response(self, 200, manifest, cache="public, max-age=60")
                return
            if path == "/metadata/search":
                value = (query.get("q") or [""])[0]
                results = self.store.search(value, limit_from(DEFAULT_LIMIT))
                _json_response(self, 200, {"query": value, "count": len(results), "items": results})
                return
            match = re.fullmatch(r"/metadata/(\d+)(?:/(credits|similar))?", path)
            if match:
                media_id = int(match.group(1))
                section = match.group(2)
                if section == "credits":
                    _json_response(self, 200, {"id": media_id, "credits": self.store.credits(media_id)})
                    return
                if section == "similar":
                    _json_response(self, 200, {"id": media_id, "items": self.store.similar(media_id, limit_from(10))})
                    return
                detail = self.store.detail(media_id)
                if detail is None:
                    _json_response(self, 404, {"error": "not_found", "id": media_id})
                else:
                    _json_response(self, 200, detail)
                return
            _json_response(self, 404, {"error": "not_found", "path": path})
        except FileNotFoundError as error:
            _json_response(self, 503, {"error": "database_unavailable", "detail": str(error)})
        except sqlite3.DatabaseError as error:
            _json_response(self, 500, {"error": "database_error", "detail": str(error)})


def main() -> int:
    metadata_dir = Path(os.environ.get("OTV_METADATA_DIR", DEFAULT_METADATA_DIR))
    public_dir = Path(os.environ.get("OTV_METADATA_PUBLIC_DIR", DEFAULT_PUBLIC_DIR))
    host = os.environ.get("OTV_METADATA_BIND", "0.0.0.0")
    port = int(os.environ.get("OTV_METADATA_PORT", "8094"))
    store = MetadataStore(metadata_dir, public_dir)
    handler = type("BoundMetadataHandler", (MetadataHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"metadata api listening on {host}:{port} (db={store.db_path})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
