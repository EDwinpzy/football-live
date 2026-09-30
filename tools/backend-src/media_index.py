"""Persistent, device-local search index for scraped Douban subjects.

The index deliberately has no network code.  Callers ingest metadata obtained by
the catalogue scraper and all search requests are answered from SQLite.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path


SCHEMA_VERSION = 1
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
SEPARATOR = "\x1f"
PINYIN_PATH = Path(__file__).with_name("pinyin_map.json")


def normalize_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _load_pinyin(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _join(values):
    return SEPARATOR.join(str(value or "") for value in values if str(value or "").strip())


def _split(value):
    return [part for part in str(value or "").split(SEPARATOR) if part]


def _edit_distance(left, right, cutoff):
    """Levenshtein distance with row and length cutoffs for fuzzy fallback."""
    if left == right:
        return 0
    if abs(len(left) - len(right)) > cutoff:
        return cutoff + 1
    previous = list(range(len(right) + 1))
    for i, lc in enumerate(left, 1):
        current = [i]
        row_min = i
        for j, rc in enumerate(right, 1):
            value = min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (lc != rc))
            current.append(value)
            row_min = min(row_min, value)
        if row_min > cutoff:
            return cutoff + 1
        previous = current
    return previous[-1]


class MediaIndex:
    def __init__(self, db_path, pinyin_path=PINYIN_PATH, pinyin_map=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.pinyin_map = dict(pinyin_map) if isinstance(pinyin_map, dict) else _load_pinyin(pinyin_path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @property
    def schema_version(self):
        with self._lock:
            return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def count(self):
        """已索引条目数。"""
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM media").fetchone()[0])

    def subject_ids(self):
        with self._lock:
            return {row[0] for row in self._db.execute("SELECT douban_id FROM media")}

    def _migrate(self):
        with self._lock, self._db:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("media index schema is newer than this backend")
            if version < 1:
                self._db.executescript("""
                    CREATE TABLE IF NOT EXISTS media (
                        douban_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        original_title TEXT NOT NULL DEFAULT '',
                        year TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT 'movie',
                        season INTEGER NOT NULL DEFAULT 0,
                        rating REAL NOT NULL DEFAULT 0,
                        rating_count INTEGER NOT NULL DEFAULT 0,
                        data_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS media_alias (
                        douban_id TEXT NOT NULL REFERENCES media(douban_id) ON DELETE CASCADE,
                        alias TEXT NOT NULL,
                        normalized TEXT NOT NULL,
                        PRIMARY KEY (douban_id, alias)
                    );
                    CREATE TABLE IF NOT EXISTS media_person (
                        douban_id TEXT NOT NULL REFERENCES media(douban_id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        role TEXT NOT NULL,
                        normalized TEXT NOT NULL,
                        pinyin TEXT NOT NULL,
                        initials TEXT NOT NULL,
                        PRIMARY KEY (douban_id, name, role)
                    );
                    CREATE TABLE IF NOT EXISTS media_genre (
                        douban_id TEXT NOT NULL REFERENCES media(douban_id) ON DELETE CASCADE,
                        genre TEXT NOT NULL,
                        normalized TEXT NOT NULL,
                        PRIMARY KEY (douban_id, genre)
                    );
                    CREATE TABLE IF NOT EXISTS media_search (
                        douban_id TEXT PRIMARY KEY REFERENCES media(douban_id) ON DELETE CASCADE,
                        title_norm TEXT NOT NULL,
                        original_norm TEXT NOT NULL,
                        alias_norms TEXT NOT NULL,
                        title_pinyin TEXT NOT NULL,
                        title_initials TEXT NOT NULL,
                        alias_pinyins TEXT NOT NULL,
                        alias_initials TEXT NOT NULL,
                        people_norms TEXT NOT NULL,
                        people_pinyins TEXT NOT NULL,
                        people_initials TEXT NOT NULL,
                        genre_norms TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_media_title ON media_search(title_norm);
                    CREATE INDEX IF NOT EXISTS idx_media_title_pinyin ON media_search(title_pinyin);
                    CREATE INDEX IF NOT EXISTS idx_media_title_initials ON media_search(title_initials);
                    PRAGMA user_version=1;
                """)

    def _romanize(self, value):
        normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
        pieces = []
        initials = []
        ascii_word = False
        for char in normalized:
            mapped = self.pinyin_map.get(char)
            if mapped:
                syllable = normalize_text(mapped)
                if syllable:
                    pieces.append(syllable)
                    initials.append(syllable[0])
                ascii_word = False
            elif char.isascii() and char.isalnum():
                pieces.append(char)
                if not ascii_word:
                    initials.append(char)
                ascii_word = True
            else:
                ascii_word = False
        return "".join(pieces), "".join(initials)

    @staticmethod
    def _canonical_subject(raw):
        item = dict(raw or {})
        douban_id = str(item.get("douban_id") or item.get("id") or "").removeprefix("douban:")
        if not douban_id or not str(item.get("title") or "").strip():
            return None
        item["douban_id"] = douban_id
        item["id"] = "douban:" + douban_id
        for key in ("aliases", "genres", "regions", "directors", "actors"):
            value = item.get(key)
            item[key] = list(value) if isinstance(value, (list, tuple)) else ([] if not value else [str(value)])
        return item

    def upsert_many(self, subjects):
        rows = [self._canonical_subject(subject) for subject in subjects or []]
        rows = [row for row in rows if row]
        if not rows:
            return 0
        with self._lock, self._db:
            for item in rows:
                douban_id = item["douban_id"]
                aliases = [str(value).strip() for value in item["aliases"] if str(value).strip()]
                people = [(str(value).strip(), role) for role in ("director", "actor")
                          for value in item["directors" if role == "director" else "actors"] if str(value).strip()]
                genres = [str(value).strip() for value in item["genres"] if str(value).strip()]
                title_pinyin, title_initials = self._romanize(item["title"])
                alias_romanized = [self._romanize(value) for value in aliases]
                people_romanized = [self._romanize(value) for value, _role in people]
                self._db.execute("""
                    INSERT INTO media(douban_id,title,original_title,year,category,season,rating,rating_count,data_json,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(douban_id) DO UPDATE SET
                      title=excluded.title, original_title=excluded.original_title, year=excluded.year,
                      category=excluded.category, season=excluded.season, rating=excluded.rating,
                      rating_count=excluded.rating_count, data_json=excluded.data_json, updated_at=CURRENT_TIMESTAMP
                """, (douban_id, str(item.get("title") or ""), str(item.get("original_title") or ""),
                      str(item.get("year") or ""), str(item.get("category") or "movie"), int(item.get("season") or 0),
                      float(item.get("rating") or 0), int(item.get("rating_count") or 0),
                      json.dumps(item, ensure_ascii=False, separators=(",", ":"))))
                for table in ("media_alias", "media_person", "media_genre", "media_search"):
                    self._db.execute("DELETE FROM %s WHERE douban_id=?" % table, (douban_id,))
                self._db.executemany("INSERT INTO media_alias VALUES(?,?,?)",
                                     [(douban_id, value, normalize_text(value)) for value in aliases])
                self._db.executemany("INSERT INTO media_person VALUES(?,?,?,?,?,?)", [
                    (douban_id, value, role, normalize_text(value), roman[0], roman[1])
                    for (value, role), roman in zip(people, people_romanized)
                ])
                self._db.executemany("INSERT INTO media_genre VALUES(?,?,?)",
                                     [(douban_id, value, normalize_text(value)) for value in genres])
                self._db.execute("INSERT INTO media_search VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    douban_id, normalize_text(item["title"]), normalize_text(item.get("original_title")),
                    _join(normalize_text(value) for value in aliases), title_pinyin, title_initials,
                    _join(value[0] for value in alias_romanized), _join(value[1] for value in alias_romanized),
                    _join(normalize_text(value) for value, _role in people),
                    _join(value[0] for value in people_romanized), _join(value[1] for value in people_romanized),
                    _join(normalize_text(value) for value in genres),
                ))
        return len(rows)

    @staticmethod
    def _field_score(value, query, exact, prefix, contains):
        if not value:
            return 0
        if value == query:
            return exact
        if value.startswith(query):
            return prefix
        if query in value:
            return contains
        return 0

    def _score(self, row, query):
        score = self._field_score(row["title_norm"], query, 120, 108, 82)
        score = max(score, self._field_score(row["original_norm"], query, 90, 78, 72))
        for value in _split(row["alias_norms"]):
            score = max(score, self._field_score(value, query, 115, 80, 76))
        score = max(score, self._field_score(row["title_initials"], query, 105, 92, 62))
        score = max(score, self._field_score(row["title_pinyin"], query, 102, 96, 70))
        for field, weights in (("alias_pinyins", (100, 94, 68)), ("alias_initials", (103, 90, 60)),
                               ("people_norms", (68, 55, 48)), ("people_pinyins", (62, 54, 44)),
                               ("people_initials", (58, 50, 40)), ("genre_norms", (45, 40, 35))):
            for value in _split(row[field]):
                score = max(score, self._field_score(value, query, *weights))
        return score

    def _fuzzy_score(self, row, query):
        if len(query) < 3:
            return 0
        cutoff = 1 if len(query) < 7 else 2
        choices = [row["title_norm"], row["title_pinyin"]]
        choices += _split(row["alias_norms"]) + _split(row["alias_pinyins"])
        best = cutoff + 1
        for value in choices:
            if not value:
                continue
            distance = _edit_distance(query, value, cutoff)
            best = min(best, distance)
        return 60 - best * 12 if best <= cutoff else 0

    def search(self, query, limit=DEFAULT_LIMIT):
        normalized = normalize_text(query)
        if not normalized:
            return []
        limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
        like = "%" + normalized + "%"
        with self._lock:
            rows = self._db.execute("""
                SELECT s.*, m.data_json, m.rating, m.rating_count
                FROM media_search s JOIN media m USING(douban_id)
                WHERE title_norm LIKE ? OR original_norm LIKE ? OR alias_norms LIKE ?
                   OR title_pinyin LIKE ? OR title_initials LIKE ? OR alias_pinyins LIKE ?
                   OR alias_initials LIKE ? OR people_norms LIKE ? OR people_pinyins LIKE ?
                   OR people_initials LIKE ? OR genre_norms LIKE ?
                LIMIT 500
            """, (like,) * 11).fetchall()
            ranked = [(self._score(row, normalized), row) for row in rows]
            ranked = [value for value in ranked if value[0] > 0]
            if len(ranked) < 8:
                seen = {row["douban_id"] for _score, row in ranked}
                for row in self._db.execute("""
                    SELECT s.*, m.data_json, m.rating, m.rating_count
                    FROM media_search s JOIN media m USING(douban_id)
                """):
                    if row["douban_id"] in seen:
                        continue
                    score = self._fuzzy_score(row, normalized)
                    if score:
                        ranked.append((score, row))
            ranked.sort(key=lambda value: (-value[0], -float(value[1]["rating"] or 0),
                                           -int(value[1]["rating_count"] or 0), value[1]["douban_id"]))
            results = []
            for score, row in ranked[:limit]:
                item = json.loads(row["data_json"])
                item["search_score"] = score
                results.append(item)
            return results
