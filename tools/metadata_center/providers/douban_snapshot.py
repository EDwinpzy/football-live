"""Reads the douban_snapshot.json the existing scraper already produces."""

from __future__ import annotations

import json
from pathlib import Path

from ..model import NormalizedMedia, as_text, parse_runtime_minutes, parse_year
from .base import MetadataProvider, ProviderError

PROVIDER = "douban"


def _identifier(raw: dict) -> str:
    value = as_text(raw.get("douban_id"))
    if value:
        return value
    raw_id = as_text(raw.get("id"))
    if raw_id.startswith(f"{PROVIDER}:"):
        return raw_id.split(":", 1)[1]
    return raw_id


def to_normalized(raw: dict, has_detail: bool = False) -> NormalizedMedia | None:
    identifier = _identifier(raw)
    title = as_text(raw.get("title"))
    if not identifier or not title:
        return None
    return NormalizedMedia(
        provider=PROVIDER,
        external_id=identifier,
        title=title,
        media_type=as_text(raw.get("category")) or "movie",
        original_title=as_text(raw.get("original_title")) or None,
        aliases=list(raw.get("aliases") or ()),
        year=parse_year(raw.get("year")),
        overview=as_text(raw.get("summary")) or None,
        genres=list(raw.get("genres") or ()),
        regions=list(raw.get("regions") or ()),
        tags=list(raw.get("tags") or ()),
        directors=list(raw.get("directors") or ()),
        actors=list(raw.get("actors") or ()),
        external_ids={PROVIDER: identifier},
        rating=raw.get("rating") if isinstance(raw.get("rating"), (int, float)) else None,
        vote_count=raw.get("rating_count") if isinstance(raw.get("rating_count"), int) else None,
        poster_url=as_text(raw.get("poster_url")) or None,
        hero_url=as_text(raw.get("hero_url")) or None,
        backdrop_url=as_text(raw.get("backdrop_url")) or None,
        release_date=as_text(raw.get("release_date")) or None,
        runtime_minutes=parse_runtime_minutes(raw.get("durations")),
        episodes=raw.get("episodes_count") if isinstance(raw.get("episodes_count"), int) else None,
        season=raw.get("season") if isinstance(raw.get("season"), int) else None,
        has_detail=has_detail,
    )


class DoubanSnapshotProvider(MetadataProvider):
    name = PROVIDER

    def __init__(self, snapshot_path):
        self.path = Path(snapshot_path)
        self._cache: list[NormalizedMedia] | None = None
        self.source_meta: dict = {}

    def load_document(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ProviderError(f"snapshot unreadable: {error}") from error
        except ValueError as error:
            raise ProviderError(f"snapshot is not valid JSON: {error}") from error
        if not isinstance(document, dict):
            raise ProviderError("snapshot root must be an object")
        return document

    def collect(self, limit: int | None = None) -> list[NormalizedMedia]:
        if self._cache is None:
            document = self.load_document()
            self.source_meta = {
                "source": as_text(document.get("source")),
                "generated_at": as_text(document.get("generatedAt")),
                "stats": document.get("stats") or {},
            }
            merged: dict[str, NormalizedMedia] = {}
            for raw in document.get("index") or ():
                item = to_normalized(raw, has_detail=False)
                if item:
                    merged[item.external_id] = item
            details = document.get("details") or {}
            if isinstance(details, dict):
                for raw in details.values():
                    item = to_normalized(raw, has_detail=True)
                    if not item:
                        continue
                    existing = merged.get(item.external_id)
                    if existing is None:
                        merged[item.external_id] = item
                    else:
                        existing.fill_blanks_from(item)
                        existing.rating = item.rating if item.rating is not None else existing.rating
                        existing.vote_count = item.vote_count if item.vote_count is not None else existing.vote_count
                        existing.has_detail = True
            self._cache = sorted(merged.values(), key=lambda value: value.external_id)
        return self._cache[:limit] if limit else list(self._cache)

    def get_movie(self, external_id: str) -> NormalizedMedia | None:
        wanted = as_text(external_id)
        for item in self.collect():
            if item.external_id == wanted:
                return item
        return None

    def search(self, query: str) -> list[NormalizedMedia]:
        needle = as_text(query).lower()
        return [item for item in self.collect() if needle and needle in item.title.lower()]

    def get_person(self, external_id: str):  # pragma: no cover - snapshot has no person ids
        return None

    def get_updates(self, since):  # pragma: no cover - snapshot is a full export
        return self.collect()
