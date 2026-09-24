"""Canonical My TV ID to playback-source reference resolution."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import metadata_client
import vod_sources


DEFAULT_INDEX_MAX_AGE_SECONDS = 30 * 86400
RESOLVE_STATUSES = frozenset({
    "RESOLVING",
    "FAST_PATH_FAILED",
    "FALLBACK_SEARCHING",
    "STALE_AVAILABLE",
    "TEMPORARILY_UNAVAILABLE",
    "MATCHED",
})
_DIGITS = re.compile(r"[0-9]+\Z")


@dataclass(frozen=True)
class IndexLookup:
    entry: dict | None
    version: object = None
    age_ms: int = 0
    stale: bool = False
    conflict: bool = False


class StaticLineIndex:
    """Exact-key adapter for the existing static ``vod-lines`` index.

    The provider is normally ``proxy._vod_index_payload``. This adapter never
    searches titles or derives a Douban ID from a My TV ID.
    """

    def __init__(self, payload_loader, max_age_seconds=DEFAULT_INDEX_MAX_AGE_SECONDS,
                 clock=time.time):
        self._payload_loader = payload_loader
        self.max_age_seconds = max(0.0, float(max_age_seconds))
        self._clock = clock

    def lookup(self, douban_id):
        key = str(douban_id)
        try:
            payload = self._payload_loader()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return IndexLookup(entry=None)

        generated_at = _timestamp(payload.get("generated_at", payload.get("generatedAt")))
        version = payload.get("version")
        if version is None:
            version = payload.get("generated_at", payload.get("generatedAt"))
        if version is None:
            version = payload.get("schema")

        entries = payload.get("entries")
        entry = entries.get(key) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            age_ms = _age_ms(generated_at, self._clock())
            stale = generated_at is None or age_ms > self.max_age_seconds * 1000
            return IndexLookup(entry=None, version=version, age_ms=age_ms, stale=stale)

        entry_douban_id = entry.get("douban_id")
        conflict = (entry_douban_id is not None and str(entry_douban_id) != key)
        resolved_at = _timestamp(entry.get("resolved_at"))
        timestamp = resolved_at if resolved_at is not None else generated_at
        age_ms = _age_ms(timestamp, self._clock())
        stale = timestamp is None or age_ms > self.max_age_seconds * 1000
        entry_version = entry.get("index_version")
        if entry_version is not None and version is not None and str(entry_version) != str(version):
            stale = True
        return IndexLookup(entry=entry, version=version, age_ms=age_ms,
                           stale=stale, conflict=conflict)


@dataclass(frozen=True)
class ResolveCandidate:
    source_id: str
    source_name: str
    content_id: str
    episode_index: int
    match_score: int
    quality: dict = field(default_factory=dict)

    def to_public_dict(self):
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "content_id": self.content_id,
            "episode_index": self.episode_index,
            "match_score": self.match_score,
            "quality": dict(self.quality),
        }


@dataclass(frozen=True)
class ResolveResult:
    status: str
    mytv_id: int
    episode: int
    resolve_version: object
    preferred: ResolveCandidate | None
    backups: tuple[ResolveCandidate, ...] = ()
    cache: dict = field(default_factory=lambda: {"kind": "miss", "age_ms": 0})

    def __post_init__(self):
        if self.status not in RESOLVE_STATUSES:
            raise ValueError("unknown resolve status")

    def to_public_dict(self):
        return {
            "mytv_id": self.mytv_id,
            "episode": self.episode,
            "resolve_version": self.resolve_version,
            "preferred": self.preferred.to_public_dict() if self.preferred else None,
            "backups": [candidate.to_public_dict() for candidate in self.backups],
            "cache": dict(self.cache),
            "status": self.status,
        }

    def to_dict(self):
        return self.to_public_dict()


class VodResolver:
    """Resolve an explicit metadata mapping to URL-free source references."""

    def __init__(self, metadata, index, registry):
        self.metadata = metadata
        self.index = index
        self.registry = registry

    def resolve(self, mytv_id, episode=1):
        canonical_id = _positive_integer(mytv_id, "mytv_id")
        requested_episode = _positive_integer(episode, "episode")
        unavailable = lambda version=None, cache=None: ResolveResult(
            status="TEMPORARILY_UNAVAILABLE", mytv_id=canonical_id,
            episode=requested_episode, resolve_version=version, preferred=None,
            backups=(), cache=cache or {"kind": "miss", "age_ms": 0})

        try:
            detail = self.metadata.detail(canonical_id)
        except (metadata_client.MetadataUnavailable, metadata_client.MetadataIdentityConflict):
            return unavailable()
        if not isinstance(detail, dict):
            return unavailable()
        if not _metadata_identity_matches(detail, canonical_id):
            return unavailable()

        douban_id = metadata_client.explicit_douban_id(detail)
        if douban_id is None:
            return unavailable()

        limit = _episode_limit(detail)
        if limit is not None and requested_episode > limit:
            raise ValueError("episode exceeds metadata episode count")

        try:
            index_result = self.index.lookup(douban_id)
        except Exception:
            return unavailable()
        if not isinstance(index_result, IndexLookup):
            return unavailable()
        cache = {"kind": "static_index" if index_result.entry else "miss",
                 "age_ms": max(0, int(index_result.age_ms or 0))}
        if index_result.conflict:
            return unavailable(index_result.version, cache)
        entry = index_result.entry
        if not isinstance(entry, dict):
            return unavailable(index_result.version, cache)
        if index_result.stale:
            return unavailable(index_result.version, {
                "kind": "stale_index", "age_ms": cache["age_ms"]})
        if not _entry_identity_matches(entry, canonical_id, douban_id):
            return unavailable(index_result.version, cache)

        candidates = self._candidates(entry, requested_episode)
        if not candidates:
            return unavailable(index_result.version, cache)

        status = "STALE_AVAILABLE" if index_result.stale else "MATCHED"
        cache["kind"] = "stale_index" if index_result.stale else "static_index"
        return ResolveResult(
            status=status,
            mytv_id=canonical_id,
            episode=requested_episode,
            resolve_version=index_result.version,
            preferred=candidates[0],
            backups=tuple(candidates[1:]),
            cache=cache,
        )

    def _candidates(self, entry, requested_episode):
        configured = {
            str(getattr(source, "id", "")): source
            for source in getattr(self.registry, "sources", ())
            if getattr(source, "id", None)
        }
        result = []
        seen = set()
        for line in entry.get("lines") or ():
            if not isinstance(line, dict):
                continue
            source_id = str(line.get("source_id") or "").strip()
            source = configured.get(source_id)
            if source is None or not bool(getattr(source, "enabled", True)):
                continue
            source_name = str(getattr(source, "name", "") or line.get("source_name") or "").strip()
            if not source_name:
                continue
            match_score = _match_confidence(line.get("match_score"))
            if match_score is None or match_score < vod_sources.AUTO_MATCH_THRESHOLD:
                continue

            episodes = line.get("episodes")
            if not isinstance(episodes, list):
                continue
            wanted_index = requested_episode - 1
            matching = []
            for position, item in enumerate(episodes):
                if not isinstance(item, dict):
                    continue
                mapped_index = _nonnegative_integer(item.get("index", position))
                if mapped_index == wanted_index:
                    matching.append(item)
            if len(matching) != 1:
                continue

            content_id = _content_id(line, source_id)
            if not content_id:
                continue
            episode_index = wanted_index
            key = (source_id, content_id, episode_index)
            if key in seen:
                continue
            seen.add(key)
            result.append(ResolveCandidate(
                source_id=source_id,
                source_name=source_name,
                content_id=content_id,
                episode_index=episode_index,
                match_score=match_score,
                quality=_quality(line),
            ))
        return result


def _positive_integer(value, name):
    number = _nonnegative_integer(value)
    if number is None or number < 1:
        raise ValueError("%s must be a positive integer" % name)
    return number


def _nonnegative_integer(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not _DIGITS.fullmatch(text):
        return None
    try:
        return int(text)
    except (ValueError, OverflowError):
        return None


def _match_confidence(value):
    """Accept only the explicit title-match score persisted with a line.

    ``score`` is the source-line quality/ranking score in the existing index;
    it is never substituted for match confidence. The index must carry the
    resolver's verified ``match_score`` and meet the shared AUTO_MATCH_THRESHOLD.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        score = int(value)
    else:
        text = str(value).strip()
        if not _DIGITS.fullmatch(text):
            return None
        try:
            score = int(text)
        except (ValueError, OverflowError):
            return None
    return score if 0 <= score <= 100 else None


def _metadata_identity_matches(detail, mytv_id):
    identities = [detail.get(key) for key in ("mytv_id", "id") if detail.get(key) is not None]
    parsed = [_nonnegative_integer(value) for value in identities]
    return bool(parsed) and all(value is not None and value > 0 and value == mytv_id
                                for value in parsed)


def _entry_identity_matches(entry, mytv_id, douban_id):
    mapped_douban = entry.get("douban_id")
    if mapped_douban is not None and str(mapped_douban) != str(douban_id):
        return False
    mapped_mytv = entry.get("mytv_id")
    if mapped_mytv is not None:
        parsed = _nonnegative_integer(mapped_mytv)
        if parsed is None or parsed != mytv_id:
            return False
    return True


def _episode_limit(detail):
    media_type = str(detail.get("media_type") or detail.get("category") or detail.get("type") or "")
    media_type = media_type.strip().lower()
    if media_type in {"movie", "film", "电影"}:
        return 1
    for key in ("episodes", "episodes_count", "episode_count"):
        value = detail.get(key)
        if value is None or isinstance(value, bool):
            continue
        count = _nonnegative_integer(value)
        if count is not None and count > 0:
            return count
    return None


def _content_id(line, source_id):
    value = line.get("content_id")
    if value is None:
        value = line.get("vod_id")
    if value is not None and str(value).strip():
        value = str(value).strip()
        return "" if re.match(r"https?://", value, re.IGNORECASE) else value
    line_id = str(line.get("id") or "").strip()
    prefix = source_id + ":"
    remainder = line_id[len(prefix):] if line_id.startswith(prefix) else line_id
    first = remainder.split(":", 1)[0].strip()
    return "" if re.match(r"https?://", first, re.IGNORECASE) else first


def _quality(line):
    height = _nonnegative_integer(line.get("height"))
    latency = _nonnegative_integer(line.get("latency_ms"))
    score = line.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError, OverflowError):
        score = None
    if score is not None and (score != score or abs(score) == float("inf")):
        score = None
    return {"height": height or 0, "score": score, "latency_ms": latency}


def _timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if _DIGITS.fullmatch(text):
        try:
            return float(text)
        except (ValueError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _age_ms(timestamp, now):
    if timestamp is None:
        return 0
    try:
        return max(0, int((float(now) - float(timestamp)) * 1000))
    except (TypeError, ValueError, OverflowError):
        return 0
