"""Normalized metadata model shared by every provider."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MEDIA_TYPES = ("movie", "tv", "anime", "variety", "short")
DEFAULT_MEDIA_TYPE = "movie"
DEFAULT_PROVIDER = "unknown"


def normalize_text(value) -> str:
    """Match the device-local index normalization so both layers agree."""
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def parse_year(value) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def parse_runtime_minutes(durations) -> int | None:
    if isinstance(durations, str):
        durations = [durations]
    for value in durations or ():
        match = re.search(r"(\d{1,3})\s*(?:分钟|min|mins|minutes)", str(value or ""), re.I)
        if match:
            return int(match.group(1))
    return None


def as_text(value) -> str:
    return str(value or "").strip()


def as_unique_list(values) -> list[str]:
    seen: dict[str, None] = {}
    for value in values or ():
        text = as_text(value)
        if text and text not in seen:
            seen[text] = None
    return list(seen)


@dataclass
class NormalizedMedia:
    """One provider's view of a title, before matching against other providers."""

    provider: str
    external_id: str
    title: str
    media_type: str = DEFAULT_MEDIA_TYPE
    original_title: str | None = None
    aliases: list[str] = field(default_factory=list)
    year: int | None = None
    overview: str | None = None
    genres: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    external_ids: dict[str, str] = field(default_factory=dict)
    rating: float | None = None
    vote_count: int | None = None
    poster_url: str | None = None
    hero_url: str | None = None
    backdrop_url: str | None = None
    release_date: str | None = None
    runtime_minutes: int | None = None
    episodes: int | None = None
    season: int | None = None
    has_detail: bool = False

    def __post_init__(self) -> None:
        self.provider = as_text(self.provider) or DEFAULT_PROVIDER
        self.external_id = as_text(self.external_id)
        self.title = as_text(self.title)
        if self.media_type not in MEDIA_TYPES:
            self.media_type = DEFAULT_MEDIA_TYPE
        self.aliases = as_unique_list(self.aliases)
        self.genres = as_unique_list(self.genres)
        self.regions = as_unique_list(self.regions)
        self.tags = as_unique_list(self.tags)
        self.directors = as_unique_list(self.directors)
        self.actors = as_unique_list(self.actors)
        identifiers = {as_text(k).lower(): as_text(v) for k, v in (self.external_ids or {}).items()}
        identifiers.setdefault(self.provider.lower(), self.external_id)
        self.external_ids = {k: v for k, v in identifiers.items() if v}

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.external_id}"

    def alias_titles(self) -> list[str]:
        values = [self.original_title, *self.aliases]
        return [value for value in as_unique_list(values) if normalize_text(value) != normalize_text(self.title)]

    def director_set(self) -> set[str]:
        return {as_text(value) for value in self.directors if as_text(value)}

    def actor_set(self) -> set[str]:
        return {as_text(value) for value in self.actors if as_text(value)}

    def fill_blanks_from(self, other: "NormalizedMedia") -> None:
        """Merge another provider's record into this one without overwriting data."""
        for name in ("original_title", "overview", "language", "country"):
            if not getattr(self, name, None) and getattr(other, name, None):
                setattr(self, name, getattr(other, name))
        for name in ("year", "runtime_minutes", "episodes", "season"):
            if getattr(self, name, None) in (None, 0) and getattr(other, name, None) not in (None, 0):
                setattr(self, name, getattr(other, name))
        for name in ("rating", "vote_count"):
            if getattr(self, name, None) in (None, 0) and getattr(other, name, None) not in (None, 0):
                setattr(self, name, getattr(other, name))
        for name in ("poster_url", "hero_url", "backdrop_url", "release_date"):
            if not getattr(self, name, None) and getattr(other, name, None):
                setattr(self, name, getattr(other, name))
        self.aliases = as_unique_list([*self.aliases, *other.alias_titles()])
        self.genres = as_unique_list([*self.genres, *other.genres])
        self.regions = as_unique_list([*self.regions, *other.regions])
        self.tags = as_unique_list([*self.tags, *other.tags])
        self.directors = as_unique_list([*self.directors, *other.directors])
        self.actors = as_unique_list([*self.actors, *other.actors])
        merged_ids = dict(other.external_ids)
        merged_ids.update(self.external_ids)
        self.external_ids = merged_ids
        self.has_detail = self.has_detail or other.has_detail
