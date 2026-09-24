"""Canonical My TV ID to playback-source reference resolution."""

from __future__ import annotations

import re
import threading
import time
import concurrent.futures
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import metadata_client
import vod_sources


DEFAULT_INDEX_MAX_AGE_SECONDS = 30 * 86400
LAST_GOOD_TTL_SECONDS = 30 * 60
HOT_RESULT_TTL_SECONDS = 6 * 60 * 60
FAILURE_TTL_SECONDS = 15
FAST_CACHE_MAX_ENTRIES = 512
MAX_FAST_SOURCES = 3
FAST_SOURCE_SOFT_TIMEOUT_SECONDS = vod_sources.FAST_SOURCE_SOFT_TIMEOUT_SECONDS
FAST_RESOLVE_BUDGET_SECONDS = vod_sources.FAST_RESOLVER_BUDGET_SECONDS
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

    def __init__(self, metadata, index, registry, clock=time.monotonic):
        self.metadata = metadata
        self.index = index
        self.registry = registry
        self._clock = clock
        self._cache_lock = threading.RLock()
        self._last_good = OrderedDict()
        self._hot_cache = OrderedDict()
        self._failure_cache = OrderedDict()
        self._cache_generation = 0

    def invalidate_source_configuration(self):
        """Drop cached references when the configured source registry is replaced."""
        with self._cache_lock:
            self._cache_generation += 1
            self._last_good.clear()
            self._hot_cache.clear()
            self._failure_cache.clear()

    def resolve(self, mytv_id, episode=1, cancel_event=None):
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
        if index_result.stale:
            return unavailable(index_result.version, {
                "kind": "stale_index", "age_ms": cache["age_ms"]})
        entry = index_result.entry
        if isinstance(entry, dict) and not _entry_identity_matches(entry, canonical_id, douban_id):
            return unavailable(index_result.version, cache)

        registry_snapshot = self.registry
        generation = self._cache_generation
        cache_key = (canonical_id, requested_episode,
                     None if index_result.version is None else str(index_result.version))
        cached = self._cache_get(self._last_good, cache_key, LAST_GOOD_TTL_SECONDS)
        if cached is not None:
            result = self._cached_result(cached, canonical_id, requested_episode,
                                         index_result.version, "last_good", registry_snapshot)
            if result is not None and self._cache_snapshot_is_current(registry_snapshot, generation):
                return result
        cached = self._cache_get(self._hot_cache, cache_key, HOT_RESULT_TTL_SECONDS)
        if cached is not None:
            result = self._cached_result(cached, canonical_id, requested_episode,
                                         index_result.version, "hot_cache", registry_snapshot)
            if result is not None and self._cache_snapshot_is_current(registry_snapshot, generation):
                return result
        cached = self._cache_get(self._failure_cache, cache_key, FAILURE_TTL_SECONDS)
        if cached is not None:
            result = self._cached_result(cached, canonical_id, requested_episode,
                                         index_result.version, "failure", registry_snapshot)
            if result is not None and self._cache_snapshot_is_current(registry_snapshot, generation):
                return result

        if not isinstance(entry, dict):
            try:
                return self._resolve_missing_index(
                    detail, canonical_id, requested_episode, index_result.version,
                    cache_key, cache, cancel_event)
            except Exception:
                return unavailable(index_result.version, cache)

        candidates = self._candidates(entry, requested_episode, registry_snapshot)
        if not candidates:
            return unavailable(index_result.version, cache)

        status = "STALE_AVAILABLE" if index_result.stale else "MATCHED"
        cache["kind"] = "stale_index" if index_result.stale else "static_index"
        result = ResolveResult(
            status=status,
            mytv_id=canonical_id,
            episode=requested_episode,
            resolve_version=index_result.version,
            preferred=candidates[0],
            backups=tuple(candidates[1:]),
            cache=cache,
        )
        if not self._cache_put_if_current(
                self._hot_cache, cache_key, result, registry_snapshot, generation):
            return unavailable(index_result.version, cache)
        return result

    def _resolve_missing_index(self, detail, mytv_id, episode, version, cache_key,
                               miss_cache, cancel_event):
        registry_snapshot = self.registry
        generation = self._cache_generation
        submit = getattr(registry_snapshot, "submit_fast_searches", None)
        if not callable(submit):
            result = self._unavailable_result(mytv_id, episode, version, miss_cache)
            self._cache_put_if_current(
                self._failure_cache, cache_key, result, registry_snapshot, generation)
            return result

        subject = _source_subject(detail)
        started = time.monotonic()
        soft_deadline = started + FAST_SOURCE_SOFT_TIMEOUT_SECONDS
        hard_deadline = started + FAST_RESOLVE_BUDGET_SECONDS
        futures = tuple(submit(subject, max_sources=MAX_FAST_SOURCES) or ())
        if not futures:
            result = self._unavailable_result(mytv_id, episode, version, miss_cache)
            self._cache_put_if_current(
                self._failure_cache, cache_key, result, registry_snapshot, generation)
            return result

        pending = set(futures)
        completed_candidates = []
        late_futures = []
        preferred = None
        cancelled = False
        received_source_result = False
        try:
            while pending:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    for future in pending:
                        future.cancel()
                    break
                deadline = hard_deadline if received_source_result else soft_deadline
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                timeout = min(remaining, 0.05 if cancel_event is not None else remaining)
                done, pending = concurrent.futures.wait(
                    pending, timeout=timeout,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    continue
                received_source_result = True
                future_order = {future: index for index, future in enumerate(futures)}
                for future in sorted(done, key=lambda item: future_order[item]):
                    candidates = self._future_candidates(future, episode, registry_snapshot)
                    if not candidates:
                        if self._future_candidates(
                                future, episode, registry_snapshot, allow_late=True):
                            late_futures.append(future)
                        continue
                    completed_candidates.extend(candidates)
                    if preferred is None:
                        preferred = candidates[0]
                if preferred is not None:
                    break
        except BaseException:
            for future in pending:
                future.cancel()
            for future in futures:
                if future not in pending and not future.done():
                    future.cancel()
            raise

        if cancelled:
            return self._unavailable_result(mytv_id, episode, version, miss_cache)

        if preferred is None:
            result = self._unavailable_result(mytv_id, episode, version, miss_cache)
            stored = self._cache_put_if_current(
                self._failure_cache, cache_key, result, registry_snapshot, generation)
        else:
            backups = _unique_backups(preferred, completed_candidates)
            result = ResolveResult(
                status="MATCHED", mytv_id=mytv_id, episode=episode,
                resolve_version=version, preferred=preferred,
                backups=tuple(backups), cache={"kind": "miss", "age_ms": 0})
            with self._cache_lock:
                stored = (generation == self._cache_generation and
                          self.registry is registry_snapshot)
                if stored:
                    now = self._clock()
                    self._cache_store_locked(self._hot_cache, cache_key, result, now)
                    self._cache_store_locked(self._last_good, cache_key, result, now)

        if not stored:
            for future in pending:
                future.cancel()
            return self._unavailable_result(mytv_id, episode, version, miss_cache)

        for future in tuple(pending) + tuple(late_futures):
            future.add_done_callback(
                lambda done, key=cache_key, requested_episode=episode,
                canonical_id=mytv_id, resolve_version=version,
                snapshot=registry_snapshot, current_generation=generation:
                self._safe_merge_late_candidate(done, key, requested_episode,
                                                 canonical_id, resolve_version,
                                                 snapshot, current_generation))
        return result

    def _future_candidates(self, future, episode, registry=None, allow_late=False):
        try:
            value = future.result()
        except (concurrent.futures.CancelledError, Exception):
            return []
        if not isinstance(value, tuple) or len(value) != 4:
            return []
        _source, lines, error, elapsed_ms = value
        if error is not None or not isinstance(lines, (list, tuple)):
            return []
        try:
            elapsed_ms = float(elapsed_ms)
        except (TypeError, ValueError, OverflowError):
            return []
        if elapsed_ms > FAST_SOURCE_SOFT_TIMEOUT_SECONDS * 1000 and not allow_late:
            return []
        return self._candidates({"lines": lines}, episode, registry)

    def _safe_merge_late_candidate(self, future, key, episode, mytv_id, version,
                                   registry, generation):
        try:
            self._merge_late_candidate(future, key, episode, mytv_id, version,
                                       registry, generation)
        except Exception:
            return

    def _merge_late_candidate(self, future, key, episode, mytv_id, version,
                              registry, generation):
        candidates = self._future_candidates(future, episode, registry, allow_late=True)
        if not candidates:
            return
        with self._cache_lock:
            if generation != self._cache_generation or self.registry is not registry:
                return
            now = self._clock()
            current = None
            item = self._hot_cache.get(key)
            if item is not None and now - item[0] < HOT_RESULT_TTL_SECONDS:
                current = item[1]
            if current is None:
                item = self._last_good.get(key)
                if item is not None and now - item[0] < LAST_GOOD_TTL_SECONDS:
                    current = item[1]
            if current is None:
                item = self._failure_cache.get(key)
                if item is not None and now - item[0] < FAILURE_TTL_SECONDS:
                    current = item[1]

            if current is not None and current.preferred is not None:
                preferred = current.preferred
                backups = list(current.backups)
                backups.extend(candidates)
            else:
                preferred = candidates[0]
                backups = list(current.backups) if current is not None else []
                backups.extend(candidates[1:])
            unique = _unique_backups(preferred, backups)
            merged = ResolveResult(
                status="MATCHED", mytv_id=mytv_id, episode=episode,
                resolve_version=version, preferred=preferred,
                backups=tuple(unique), cache={"kind": "miss", "age_ms": 0})
            self._cache_store_locked(self._hot_cache, key, merged, now)
            self._failure_cache.pop(key, None)
            last_good = self._last_good.get(key)
            if last_good is not None and now - last_good[0] < LAST_GOOD_TTL_SECONDS:
                last_result = last_good[1]
                last_preferred = last_result.preferred or preferred
                last_backups = _unique_backups(
                    last_preferred, list(last_result.backups) + candidates)
                last_result = ResolveResult(
                    status="MATCHED", mytv_id=mytv_id, episode=episode,
                    resolve_version=version, preferred=last_preferred,
                    backups=tuple(last_backups), cache={"kind": "miss", "age_ms": 0})
                self._cache_store_locked(self._last_good, key, last_result, now)
            elif current is None or current.preferred is None:
                self._cache_store_locked(self._last_good, key, merged, now)

    def _cache_put_if_current(self, cache, key, result, registry, generation):
        with self._cache_lock:
            if generation != self._cache_generation or self.registry is not registry:
                return False
            self._cache_store_locked(cache, key, result, self._clock())
            return True

    def _cache_store_locked(self, cache, key, result, now):
        cache[key] = (now, result)
        cache.move_to_end(key)
        while len(cache) > FAST_CACHE_MAX_ENTRIES:
            cache.popitem(last=False)

    @staticmethod
    def _unavailable_result(mytv_id, episode, version, cache):
        return ResolveResult(
            status="TEMPORARILY_UNAVAILABLE", mytv_id=mytv_id,
            episode=episode, resolve_version=version, preferred=None,
            backups=(), cache=dict(cache))

    def _cache_get(self, cache, key, ttl_seconds):
        now = self._clock()
        with self._cache_lock:
            item = cache.get(key)
            if item is None:
                return None
            stored_at, result = item
            age = max(0.0, now - stored_at)
            if age >= ttl_seconds:
                cache.pop(key, None)
                return None
            cache.move_to_end(key)
            return result, int(age * 1000)

    def _cached_result(self, cached, mytv_id, episode, version, kind, registry):
        result, age_ms = cached
        if result.preferred is None:
            return ResolveResult(
                status=result.status, mytv_id=mytv_id, episode=episode,
                resolve_version=version, preferred=None, backups=(),
                cache={"kind": kind, "age_ms": age_ms})
        configured = {
            str(getattr(source, "id", "")): source
            for source in getattr(registry, "sources", ())
            if getattr(source, "id", None) and bool(getattr(source, "enabled", True))
        }
        candidates = (result.preferred,) + tuple(result.backups)
        valid = []
        for candidate in candidates:
            source = configured.get(candidate.source_id)
            if (source is None or candidate.episode_index != episode - 1 or
                    candidate.match_score < vod_sources.AUTO_MATCH_THRESHOLD):
                continue
            valid.append(ResolveCandidate(
                source_id=candidate.source_id,
                source_name=str(getattr(source, "name", "") or candidate.source_name),
                content_id=candidate.content_id,
                episode_index=candidate.episode_index,
                match_score=candidate.match_score,
                quality=dict(candidate.quality),
            ))
        if not valid:
            return None
        return ResolveResult(
            status="MATCHED",
            mytv_id=mytv_id,
            episode=episode,
            resolve_version=version,
            preferred=valid[0],
            backups=tuple(valid[1:]),
            cache={"kind": kind, "age_ms": age_ms},
        )

    def _cache_snapshot_is_current(self, registry, generation):
        with self._cache_lock:
            return generation == self._cache_generation and self.registry is registry

    def _candidates(self, entry, requested_episode, registry=None):
        registry = self.registry if registry is None else registry
        configured = {
            str(getattr(source, "id", "")): source
            for source in getattr(registry, "sources", ())
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
            for item in episodes:
                if not isinstance(item, dict):
                    continue
                mapped_index = _nonnegative_integer(item.get("index"))
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


def _source_subject(detail):
    """Pass only reviewed metadata fields to source matching; never derive identity."""
    keys = ("title", "original_title", "aliases", "year", "category", "season")
    subject = {key: detail[key] for key in keys if key in detail}
    if "category" not in subject and detail.get("media_type") is not None:
        subject["category"] = detail["media_type"]
    return subject


def _unique_backups(preferred, candidates):
    preferred_key = (preferred.source_id, preferred.content_id, preferred.episode_index)
    seen = {preferred_key}
    result = []
    for candidate in candidates:
        if not isinstance(candidate, ResolveCandidate):
            continue
        key = (candidate.source_id, candidate.content_id, candidate.episode_index)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


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
