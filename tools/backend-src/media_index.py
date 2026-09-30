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
# 归一化规则版本，与 PRAGMA user_version 相互独立。
# 规则升级只递增它；user_version 保持不变，旧版回滚仍能打开同一个库。
# 1 = NFKC 保留音符（Les Misérables -> lesmisrables），2 = 丢弃音符。
NORMALIZATION_VERSION = 2
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
SEPARATOR = "\x1f"
PINYIN_PATH = Path(__file__).with_name("pinyin_map.json")
# 强命中门槛：片名/别名/原名精确与整片名包含都在 70 以上，人员 44~48、类型 35~45 在其下。
STRONG_SCORE = 70
# 强命中达到该条数就跳过全表模糊扫描（沿用原有门槛）。
FUZZY_HIT_LIMIT = 8
CATEGORY_SEARCH_TERMS = {
    "电影": "movie", "movie": "movie",
    "电视剧": "tv", "连续剧": "tv", "tv": "tv",
    "动漫": "anime", "动画": "anime", "anime": "anime",
    "综艺": "variety", "variety": "variety",
    "短剧": "short", "short": "short",
}
CATEGORY_SEARCH_SCORE = 130

SCHEMA_SQL = """
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
    CREATE TABLE IF NOT EXISTS media_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_media_title ON media_search(title_norm);
    CREATE INDEX IF NOT EXISTS idx_media_title_pinyin ON media_search(title_pinyin);
    CREATE INDEX IF NOT EXISTS idx_media_title_initials ON media_search(title_initials);
    PRAGMA user_version=1;
"""


def normalize_text(value):
    # NFD 拆出组合音符后丢弃：NFKC 会把 Amélie 留成 Amlie，导致用户输
    # amelie 搜不到。索引与查询共用本函数，两侧一致即可互查。
    text = unicodedata.normalize("NFD", str(value or "")).lower()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = unicodedata.normalize("NFKC", text).strip()
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

    @property
    def normalization_version(self):
        """已写入本库的归一化规则版本；旧库（没有标记）视为 1。"""
        with self._lock:
            return self._read_normalization_version()

    def _read_normalization_version(self):
        try:
            row = self._db.execute(
                "SELECT value FROM media_meta WHERE key='normalization_version'"
            ).fetchone()
        except sqlite3.Error:
            return 1
        try:
            return int(row[0]) if row is not None else 1
        except (TypeError, ValueError):
            return 1

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

    def get(self, subject_id):
        with self._lock:
            row = self._db.execute("SELECT data_json FROM media WHERE douban_id=?",
                                   (str(subject_id).removeprefix("douban:"),)).fetchone()
            return json.loads(row[0]) if row else None

    def _migrate(self):
        with self._lock, self._db:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("media index schema is newer than this backend")
            if version < 1:
                self._db.executescript(SCHEMA_SQL)
        self._migrate_normalization()

    def _migrate_normalization(self):
        """按标记重算检索列；不触碰 user_version，旧版回滚仍可读。"""
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS media_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
            current = self._read_normalization_version()
            if current < NORMALIZATION_VERSION:
                self._rebuild_search_rows()
                self._db.execute(
                    "INSERT INTO media_meta(key,value) VALUES('normalization_version',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(NORMALIZATION_VERSION),))
            elif current > NORMALIZATION_VERSION:
                # 库由更新版本写出：只读不改，回滚后仍应可用。
                return

    def _rebuild_search_rows(self):
        """按当前归一化/拼音规则重算全部检索列，保留 media 上的富元数据。"""
        rows = self._db.execute(
            "SELECT douban_id, title, original_title, data_json FROM media").fetchall()
        for row in rows:
            try:
                item = json.loads(row["data_json"])
            except (TypeError, ValueError):
                item = {}
            aliases = item.get("aliases") if isinstance(item, dict) else None
            aliases = [str(v).strip() for v in (aliases or []) if str(v or "").strip()]
            if not aliases:
                # data_json 不可用时从别名表兜底，避免归一化列被清空。
                aliases = [r[0] for r in self._db.execute(
                    "SELECT alias FROM media_alias WHERE douban_id=?", (row["douban_id"],))]
            people = [(r["name"], r["role"]) for r in self._db.execute(
                "SELECT name, role FROM media_person WHERE douban_id=?", (row["douban_id"],))]
            alias_romanized = [self._romanize(v) for v in aliases]
            people_romanized = [self._romanize(v) for v, _role in people]
            title_pinyin, title_initials = self._romanize(row["title"])
            for alias, value in zip(aliases, (normalize_text(v) for v in aliases)):
                self._db.execute(
                    "UPDATE media_alias SET normalized=? WHERE douban_id=? AND alias=?",
                    (value, row["douban_id"], alias))
            for (name, role), norm, roman in zip(
                    people,
                    (normalize_text(v) for v, _ in people),
                    people_romanized):
                self._db.execute(
                    "UPDATE media_person SET normalized=?, pinyin=?, initials=?"
                    " WHERE douban_id=? AND name=? AND role=?",
                    (norm, roman[0], roman[1], row["douban_id"], name, role))
            self._db.execute("""
                UPDATE media_search SET title_norm=?, original_norm=?, alias_norms=?,
                    title_pinyin=?, title_initials=?, alias_pinyins=?, alias_initials=?,
                    people_norms=?, people_pinyins=?, people_initials=?
                 WHERE douban_id=?
            """, (normalize_text(row["title"]), normalize_text(row["original_title"]),
                  _join(normalize_text(v) for v in aliases),
                  title_pinyin, title_initials,
                  _join(v[0] for v in alias_romanized),
                  _join(v[1] for v in alias_romanized),
                  _join(normalize_text(v) for v, _ in people),
                  _join(v[0] for v in people_romanized),
                  _join(v[1] for v in people_romanized),
                  row["douban_id"]))

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
        category = CATEGORY_SEARCH_TERMS.get(normalized)
        limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
        like = "%" + normalized + "%"
        with self._lock:
            # 不预先 LIMIT：截断在打分之前发生，最相关的那条常被丢掉。
            rows = self._db.execute("""
                SELECT s.*, m.data_json, m.rating, m.rating_count, m.category
                FROM media_search s JOIN media m USING(douban_id)
                WHERE title_norm LIKE ? OR original_norm LIKE ? OR alias_norms LIKE ?
                   OR title_pinyin LIKE ? OR title_initials LIKE ? OR alias_pinyins LIKE ?
                   OR alias_initials LIKE ? OR people_norms LIKE ? OR people_pinyins LIKE ?
                   OR people_initials LIKE ? OR genre_norms LIKE ? OR m.category = ?
            """, (like,) * 11 + (category,)).fetchall()
            ranked = []
            for row in rows:
                score = self._score(row, normalized)
                if category and row["category"] == category:
                    score = max(score, CATEGORY_SEARCH_SCORE)
                ranked.append((score, row))
            ranked = [value for value in ranked if value[0] > 0]
            # 只数「强命中」再决定是否回退：类型/人员这类弱命中再多也不算数，
            # 但强命中仍保留原有的 8 条门槛，避免每次都全表扫描。
            if sum(score >= STRONG_SCORE for score, _row in ranked) < FUZZY_HIT_LIMIT:
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
