"""Build metadata.sqlite from one or more providers."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import SCHEMA_VERSION
from .matcher import group_media, merge_group
from .model import NormalizedMedia, normalize_text
from .pinyin import PinyinEngine, default_engine
from .providers.base import MetadataProvider

SEPARATOR = "\x1f"
ID_BASE = 10_000_001
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _join(values) -> str:
    return SEPARATOR.join(str(value) for value in values if str(value or "").strip())


def _unique(values) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


FINGERPRINT_TABLES = (
    "media",
    "external_id",
    "media_title",
    "person",
    "credit",
    "genre",
    "media_genre",
    "region",
    "media_region",
    "rating",
    "image",
    "search_doc",
    "review_queue",
)
# Volatile columns are excluded so an unchanged catalogue keeps the same fingerprint.
VOLATILE_COLUMNS = {"created_at", "updated_at", "metadata_updated", "rating_updated", "image_updated"}


def content_fingerprint(database_path) -> str:
    """Hash the catalogue content, ignoring build timestamps."""
    path = Path(database_path)
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table in FINGERPRINT_TABLES:
            try:
                cursor = connection.execute(f"SELECT * FROM {table}")
            except sqlite3.DatabaseError:
                continue
            columns = [item[0] for item in cursor.description]
            keep = [index for index, name in enumerate(columns) if name not in VOLATILE_COLUMNS]
            digest.update(table.encode("utf-8"))
            digest.update(b"\x00")
            digest.update("\x1f".join(columns[index] for index in keep).encode("utf-8"))
            rows = []
            for row in cursor:
                rows.append("\x1f".join(repr(row[index]) for index in keep))
            for value in sorted(rows):
                digest.update(value.encode("utf-8"))
                digest.update(b"\x00")
    finally:
        connection.close()
    return digest.hexdigest()


@dataclass
class BuildReport:
    db_path: str
    data_version: str
    counts: dict[str, int] = field(default_factory=dict)
    integrity: str = "unknown"
    sha256: str = ""
    size: int = 0
    sources: dict = field(default_factory=dict)
    reused_ids: int = 0
    new_ids: int = 0
    review_pairs: int = 0

    def as_dict(self) -> dict:
        return {
            "db_path": self.db_path,
            "data_version": self.data_version,
            "counts": self.counts,
            "integrity": self.integrity,
            "sha256": self.sha256,
            "size": self.size,
            "sources": self.sources,
            "reused_ids": self.reused_ids,
            "new_ids": self.new_ids,
            "review_pairs": self.review_pairs,
        }


def _read_previous_state(previous_db) -> dict:
    state = {
        "externals": {},
        "people": {},
        "genres": {},
        "regions": {},
        "media_rows": {},
        "media_times": {},
        "rating_rows": {},
        "sync_rows": {},
        "image_rows": {},
        "next_media_id": ID_BASE,
        "next_person_id": 1,
        "next_genre_id": 1,
        "next_region_id": 1,
    }
    if not previous_db:
        return state
    path = Path(previous_db)
    if not path.exists():
        return state
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for media_id, provider, external_id in connection.execute(
            "SELECT media_id, provider, external_id FROM external_id"
        ):
            state["externals"][(provider, external_id)] = media_id
        # Secondary tables are keyed by their normalized name, the same key the
        # writer uses, so ids survive a rebuild.  The stored spelling is kept so a
        # rebuild reproduces identical rows.
        for person_id, name in connection.execute("SELECT id, name FROM person"):
            state["people"][normalize_text(name) or name] = (person_id, name)
        for genre_id, name in connection.execute("SELECT id, name FROM genre"):
            state["genres"][normalize_text(name) or name] = (genre_id, name)
        for region_id, name in connection.execute("SELECT id, name FROM region"):
            state["regions"][normalize_text(name) or name] = (region_id, name)
        row = connection.execute("SELECT MAX(id) FROM media").fetchone()
        if row and row[0]:
            state["next_media_id"] = max(ID_BASE, int(row[0]) + 1)
        for row in connection.execute(
            "SELECT id, media_type, title, original_title, year, runtime, overview, status, language,"
            " country, release_date, episodes, season, popularity, created_at, updated_at FROM media"
        ):
            state["media_rows"][row[0]] = tuple(row[1:14])
            state["media_times"][row[0]] = (row[14], row[15])
        for row in connection.execute("SELECT media_id, provider, score, vote_count, updated_at FROM rating"):
            state["rating_rows"][(row[0], row[1])] = (row[2], row[3], row[4])
        for row in connection.execute(
            "SELECT media_id, metadata_updated, rating_updated, image_updated, priority FROM sync_state"
        ):
            state["sync_rows"][row[0]] = (row[1], row[2], row[3], row[4])
        for row in connection.execute("SELECT media_id, image_type, original_url, cached_url FROM image"):
            state["image_rows"][(row[0], row[1])] = (row[2], row[3])
        for key, table in (("next_person_id", "person"), ("next_genre_id", "genre"), ("next_region_id", "region")):
            row = connection.execute(f"SELECT MAX(id) FROM {table}").fetchone()
            state[key] = int(row[0]) + 1 if row and row[0] else 1
    finally:
        connection.close()
    return state


class _IdAllocator:
    def __init__(self, start: int):
        self.next_value = start

    def take(self) -> int:
        value = self.next_value
        self.next_value += 1
        return value


class MetadataBuilder:
    def __init__(self, engine: PinyinEngine | None = None):
        self.engine = engine or default_engine()

    def collect(self, providers: list[MetadataProvider]) -> tuple[list[NormalizedMedia], dict]:
        items: list[NormalizedMedia] = []
        sources: dict = {}
        for provider in providers:
            collected = provider.collect()
            items.extend(collected)
            meta = getattr(provider, "source_meta", None)
            sources[getattr(provider, "name", "provider")] = {
                "items": len(collected),
                **(meta if isinstance(meta, dict) else {}),
            }
        return items, sources

    def build(
        self,
        providers: list[MetadataProvider],
        out_path,
        data_version: str,
        previous_db=None,
        now: int | None = None,
    ) -> BuildReport:
        now = int(now if now is not None else time.time())
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            out_path.unlink()

        items, sources = self.collect(providers)
        outcome = group_media(items)
        merged = [merge_group(group) for group in outcome.groups]
        merged.sort(key=lambda item: (item.provider, item.external_id))

        previous = _read_previous_state(previous_db)
        media_allocator = _IdAllocator(previous["next_media_id"])
        person_allocator = _IdAllocator(previous["next_person_id"])
        genre_allocator = _IdAllocator(previous["next_genre_id"])
        region_allocator = _IdAllocator(previous["next_region_id"])

        connection = sqlite3.connect(str(out_path))
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            counts = self._write_media(connection, merged, previous, media_allocator, person_allocator,
                                       genre_allocator, region_allocator, now)
            self._write_review_queue(connection, outcome.review, now)
            connection.execute("DELETE FROM id_allocator")
            connection.execute("INSERT INTO id_allocator (next_id) VALUES (?)", (media_allocator.next_value,))
            meta = {
                "schema_version": str(SCHEMA_VERSION),
                "data_version": str(data_version),
                "generated_at": str(now),
                "source_count": str(len(providers)),
                "media_count": str(counts["media"]),
            }
            connection.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", sorted(meta.items()))
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()

        report = BuildReport(
            db_path=str(out_path),
            data_version=str(data_version),
            counts=counts,
            integrity=integrity,
            sha256=sha256_file(out_path),
            size=out_path.stat().st_size,
            sources=sources,
            reused_ids=counts.get("media_reused", 0),
            new_ids=counts.get("media_new", 0),
            review_pairs=len(outcome.review),
        )
        return report

    def _write_media(
        self,
        connection: sqlite3.Connection,
        merged: list[NormalizedMedia],
        previous: dict,
        media_allocator: _IdAllocator,
        person_allocator: _IdAllocator,
        genre_allocator: _IdAllocator,
        region_allocator: _IdAllocator,
        now: int,
    ) -> dict[str, int]:
        # key -> (id, stored name); names are registered here and written after
        # the media loop so reusing an id from the previous build still emits the row.
        genre_ids: dict[str, tuple[int, str]] = dict(previous["genres"])
        region_ids: dict[str, tuple[int, str]] = dict(previous["regions"])
        person_ids: dict[str, tuple[int, str]] = dict(previous["people"])
        counts = {"media": 0, "media_reused": 0, "media_new": 0, "titles": 0, "people": 0, "credits": 0,
                  "genres": 0, "regions": 0, "ratings": 0, "images": 0}

        def genre_id(name: str) -> int:
            key = normalize_text(name) or name
            if key not in genre_ids:
                genre_ids[key] = (genre_allocator.take(), name)
                counts["genres"] += 1
            return genre_ids[key][0]

        def region_id(name: str) -> int:
            key = normalize_text(name) or name
            if key not in region_ids:
                region_ids[key] = (region_allocator.take(), name)
                counts["regions"] += 1
            return region_ids[key][0]

        def person_id(name: str) -> int:
            key = normalize_text(name) or name
            if key not in person_ids:
                person_ids[key] = (person_allocator.take(), name)
                counts["people"] += 1
            return person_ids[key][0]

        for item in merged:
            media_id = None
            for provider, external_id in sorted(item.external_ids.items()):
                found = previous["externals"].get((provider, external_id))
                if found:
                    media_id = found if media_id is None else min(media_id, found)
            if media_id is None:
                media_id = media_allocator.take()
                counts["media_new"] += 1
            else:
                counts["media_reused"] += 1
            counts["media"] += 1

            content = (
                item.media_type,
                item.title,
                item.original_title,
                item.year,
                item.runtime_minutes,
                item.overview,
                None,
                getattr(item, "language", None),
                item.regions[0] if item.regions else None,
                item.release_date,
                item.episodes or None,
                item.season or None,
                float(item.vote_count or 0),
            )
            # Timestamps only move when the content actually moved, otherwise every
            # rebuild would produce a "changed" delta for the whole catalogue.
            unchanged = previous["media_rows"].get(media_id) == content
            old_times = previous["media_times"].get(media_id) or (None, None)
            created_at = old_times[0] or now
            updated_at = (old_times[1] or now) if unchanged else now
            connection.execute(
                """
                INSERT INTO media (id, media_type, title, original_title, year, runtime, overview, status,
                                   language, country, release_date, episodes, season, popularity,
                                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    media_id,
                    *content,
                    created_at,
                    updated_at,
                ),
            )
            for provider, external_id in sorted(item.external_ids.items()):
                connection.execute(
                    "INSERT OR REPLACE INTO external_id (media_id, provider, external_id) VALUES (?, ?, ?)",
                    (media_id, provider, external_id),
                )

            title_forms = [(item.title, "primary")]
            if item.original_title:
                title_forms.append((item.original_title, "original"))
            title_forms.extend((alias, "alias") for alias in item.aliases)
            seen_titles: set[tuple[str, str]] = set()
            for value, title_type in title_forms:
                key = (normalize_text(value), title_type)
                if not key[0] or key in seen_titles:
                    continue
                seen_titles.add(key)
                roman = self.engine.romanize(value)
                connection.execute(
                    """
                    INSERT INTO media_title (media_id, title, language, title_type, normalized,
                                             pinyin, pinyin_alts, pinyin_initials, pinyin_initials_alts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        media_id,
                        value,
                        None,
                        title_type,
                        key[0],
                        roman.pinyin,
                        " ".join(roman.pinyin_alts),
                        roman.initials,
                        " ".join(roman.initials_alts),
                    ),
                )
                counts["titles"] += 1

            for order, name in enumerate(item.directors):
                connection.execute(
                    "INSERT OR REPLACE INTO credit (media_id, person_id, department, role, credit_order)"
                    " VALUES (?, ?, 'directing', 'director', ?)",
                    (media_id, person_id(name), order),
                )
                counts["credits"] += 1
            for order, name in enumerate(item.actors):
                connection.execute(
                    "INSERT OR REPLACE INTO credit (media_id, person_id, department, role, credit_order)"
                    " VALUES (?, ?, 'acting', 'actor', ?)",
                    (media_id, person_id(name), order),
                )
                counts["credits"] += 1

            for name in item.genres:
                connection.execute(
                    "INSERT OR IGNORE INTO media_genre (media_id, genre_id) VALUES (?, ?)",
                    (media_id, genre_id(name)),
                )
            for name in item.regions:
                connection.execute(
                    "INSERT OR IGNORE INTO media_region (media_id, region_id) VALUES (?, ?)",
                    (media_id, region_id(name)),
                )

            rating_updated = None
            if item.rating is not None or item.vote_count:
                old_rating = previous["rating_rows"].get((media_id, item.provider))
                rating_unchanged = bool(old_rating) and old_rating[0] == item.rating and old_rating[1] == item.vote_count
                rating_updated = (old_rating[2] or now) if rating_unchanged else now
                connection.execute(
                    "INSERT OR REPLACE INTO rating (media_id, provider, score, vote_count, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (media_id, item.provider, item.rating, item.vote_count, rating_updated),
                )
                counts["ratings"] += 1

            image_updated = None
            for image_type, url in (("poster", item.poster_url), ("hero", item.hero_url), ("backdrop", item.backdrop_url)):
                if not url:
                    continue
                old_image = previous["image_rows"].get((media_id, image_type))
                if not old_image or old_image[0] != url:
                    image_updated = now
                # The database is rebuilt from scratch, so every row is inserted;
                # only the cached copy survives from the previous build.
                connection.execute(
                    "INSERT INTO image (media_id, image_type, original_url, cached_url)"
                    " VALUES (?, ?, ?, ?)",
                    (media_id, image_type, url, old_image[1] if old_image else None),
                )
                counts["images"] += 1

            old_sync = previous["sync_rows"].get(media_id) or (None, None, None, None)
            connection.execute(
                """
                INSERT OR REPLACE INTO sync_state (media_id, metadata_updated, rating_updated, image_updated, priority)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    media_id,
                    (old_sync[0] or now) if unchanged else now,
                    rating_updated if rating_updated is not None else old_sync[1],
                    image_updated if image_updated is not None else old_sync[2],
                    1 if item.has_detail else 2,
                ),
            )

            self._write_search(connection, media_id, item)

        # Secondary tables are written once, after every name has been registered.
        connection.executemany(
            "INSERT OR IGNORE INTO person (id, name) VALUES (?, ?)",
            sorted((value[0], value[1]) for value in person_ids.values()),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO genre (id, name) VALUES (?, ?)",
            sorted((value[0], value[1]) for value in genre_ids.values()),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO region (id, name) VALUES (?, ?)",
            sorted((value[0], value[1]) for value in region_ids.values()),
        )
        return counts

    def _write_search(self, connection: sqlite3.Connection, media_id: int, item: NormalizedMedia) -> None:
        title_roman = self.engine.romanize(item.title)
        alias_romans = [self.engine.romanize(alias) for alias in item.aliases]
        alternatives = _unique([*title_roman.pinyin_alts, *(roman.pinyin for roman in alias_romans)])
        initials_alts = _unique([*title_roman.initials_alts, *(roman.initials for roman in alias_romans)])
        people = [*item.directors, *item.actors]
        people_norm = _join(normalize_text(name) for name in people)
        people_pinyin = _join(self.engine.romanize(name).pinyin for name in people)
        doc = {
            "media_id": media_id,
            "title_norm": normalize_text(item.title),
            "aliases_norm": _join(normalize_text(alias) for alias in item.aliases),
            "pinyin": title_roman.pinyin,
            "pinyin_alts": " ".join(alternatives),
            "initials": title_roman.initials,
            "initials_alts": " ".join(initials_alts),
            "people_norm": people_norm,
            "people_pinyin": people_pinyin,
            "genres_norm": _join(normalize_text(genre) for genre in item.genres),
            "popularity": float(item.vote_count or 0),
            "year": item.year,
        }
        connection.execute(
            """
            INSERT OR REPLACE INTO search_doc (media_id, title_norm, aliases_norm, pinyin, pinyin_alts,
                                               initials, initials_alts, people_norm, people_pinyin,
                                               genres_norm, popularity, year)
            VALUES (:media_id, :title_norm, :aliases_norm, :pinyin, :pinyin_alts, :initials, :initials_alts,
                    :people_norm, :people_pinyin, :genres_norm, :popularity, :year)
            """,
            doc,
        )
        connection.execute("DELETE FROM search_fts WHERE media_id = ?", (media_id,))
        connection.execute(
            """
            INSERT INTO search_fts (media_id, title, aliases, pinyin, initials, people, genres)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                media_id,
                doc["title_norm"],
                doc["aliases_norm"].replace(SEPARATOR, " "),
                " ".join([doc["pinyin"], *alternatives]) if alternatives else doc["pinyin"],
                " ".join([doc["initials"], *initials_alts]) if initials_alts else doc["initials"],
                " ".join([doc["people_norm"].replace(SEPARATOR, " "), doc["people_pinyin"].replace(SEPARATOR, " ")]).strip(),
                doc["genres_norm"].replace(SEPARATOR, " "),
            ),
        )

    def _write_review_queue(self, connection: sqlite3.Connection, review, now: int) -> None:
        for left, right, score, reason in review:
            left_key, right_key = sorted([left.key, right.key])
            connection.execute(
                "INSERT OR REPLACE INTO review_queue (left_key, right_key, score, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (left_key, right_key, float(score), reason, now),
            )
