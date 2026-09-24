import concurrent.futures
import pathlib
import sys
import time
import unittest
from unittest.mock import patch


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import vod_resolver  # noqa: E402
import vod_sources  # noqa: E402


NOW = 1_798_200_000
_UNSET = object()


def _metadata(mytv_id=10_000_123, douban_id="1292052", *, media_type="tv", episodes=2):
    external_ids = {} if douban_id is None else {"douban": douban_id}
    return {
        "id": mytv_id,
        "mytv_id": mytv_id,
        "title": "早春晴朗",
        "media_type": media_type,
        "episodes": episodes,
        "external_ids": external_ids,
    }


def _line(episode_count=2, *, source_id="source-1", match_score=90):
    return {
        "id": source_id + ":content-42:0",
        "source_id": source_id,
        "source_name": "线路甲",
        "name": "线路甲·主线",
        "height": 1080,
        "score": 91,
        "match_score": match_score,
        "episodes": [
            {"index": index, "name": "第%d集" % (index + 1),
             "url": "https://media.example/%d.m3u8?token=secret" % index}
            for index in range(episode_count)
        ],
    }


class _FakeMetadataClient:
    def __init__(self, detail):
        self.value = detail
        self.requested = []

    def detail(self, mytv_id):
        self.requested.append(mytv_id)
        if callable(self.value):
            return self.value(mytv_id)
        return self.value


class _RecordingIndex:
    def __init__(self, entries=None, *, version="2026092401", now=NOW, max_age_seconds=30 * 86400):
        self.entries = entries or {}
        self.version = version
        self.now = now
        self.max_age_seconds = max_age_seconds
        self.lookup_was_called_with = []

    def lookup(self, douban_id):
        key = str(douban_id)
        self.lookup_was_called_with.append(key)
        entry = self.entries.get(key)
        if not isinstance(entry, dict):
            return vod_resolver.IndexLookup(entry=None, version=self.version, age_ms=0, stale=False)
        resolved_at = int(entry.get("resolved_at") or self.now)
        age_seconds = max(0, self.now - resolved_at)
        stale = age_seconds > self.max_age_seconds
        return vod_resolver.IndexLookup(
            entry=entry, version=self.version, age_ms=age_seconds * 1000, stale=stale)


def _registry(enabled=True, source_ids=("source-1",)):
    return vod_sources.SourceRegistry([
        vod_sources.VodSource(source_id,
                              "线路甲" if source_id == "source-1" else "线路" + source_id,
                              "https://%s.example" % source_id,
                              "https://api.%s.example" % source_id,
                              "maccms", enabled=enabled)
        for source_id in source_ids
    ], version=17)


class VodResolverContractTest(unittest.TestCase):
    def make_resolver(self, detail=_UNSET, entries=None, **index_args):
        metadata = _FakeMetadataClient(_metadata() if detail is _UNSET else detail)
        index = _RecordingIndex(entries or {"1292052": {"resolved_at": NOW, "lines": [_line()]}}, **index_args)
        resolver = vod_resolver.VodResolver(metadata, index, _registry())
        return resolver, metadata, index

    def test_resolver_maps_mytv_id_through_explicit_douban_external_id(self):
        resolver, _metadata_client, index = self.make_resolver()

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertEqual(result.mytv_id, 10_000_123)
        self.assertEqual(index.lookup_was_called_with, ["1292052"])
        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.resolve_version, "2026092401")
        self.assertEqual(result.preferred.source_id, "source-1")
        self.assertEqual(result.preferred.content_id, "content-42")
        self.assertEqual(result.preferred.episode_index, 0)
        self.assertEqual(result.preferred.match_score, 90)
        self.assertEqual(result.preferred.quality["height"], 1080)
        self.assertNotIn("url", result.to_public_dict()["preferred"])
        self.assertNotIn("secret", repr(result.to_public_dict()))

    def test_result_restricts_status_to_the_client_contract_vocabulary(self):
        self.assertEqual(vod_resolver.RESOLVE_STATUSES, {
            "RESOLVING", "FAST_PATH_FAILED", "FALLBACK_SEARCHING",
            "STALE_AVAILABLE", "TEMPORARILY_UNAVAILABLE", "MATCHED",
        })
        with self.assertRaises(ValueError):
            vod_resolver.ResolveResult(
                status="UNKNOWN", mytv_id=1, episode=1, resolve_version=None,
                preferred=None)

    def test_missing_external_id_never_guesses_an_index_key(self):
        resolver, _metadata_client, index = self.make_resolver(detail=_metadata(douban_id=None))

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertIn(result.status, {"RESOLVING", "TEMPORARILY_UNAVAILABLE"})
        self.assertEqual(index.lookup_was_called_with, [])

    def test_unknown_mytv_id_never_guesses_a_douban_id(self):
        resolver, _metadata_client, index = self.make_resolver(detail=None)

        result = resolver.resolve(mytv_id=99, episode=1)

        self.assertEqual(result.mytv_id, 99)
        self.assertIsNone(result.preferred)
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")
        self.assertEqual(index.lookup_was_called_with, [])

    def test_conflicting_metadata_identity_never_uses_the_external_id(self):
        resolver, _metadata_client, index = self.make_resolver(
            detail=_metadata(mytv_id=10_000_124, douban_id="1292052"))

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")
        self.assertEqual(index.lookup_was_called_with, [])

    def test_ambiguous_external_id_never_selects_one_value(self):
        resolver, _metadata_client, index = self.make_resolver(
            detail=_metadata(douban_id=["1292052", "1291546"]))

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(index.lookup_was_called_with, [])

    def test_index_entry_for_a_different_mytv_id_is_a_conflict(self):
        entries = {"1292052": {"resolved_at": NOW, "mytv_id": 10_000_124, "lines": [_line()]}}
        resolver, _metadata_client, index = self.make_resolver(entries=entries)

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertEqual(index.lookup_was_called_with, ["1292052"])
        self.assertIsNone(result.preferred)
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_expired_index_cannot_produce_a_playable_candidate(self):
        old_entry = {"resolved_at": NOW - 31 * 86400, "lines": [_line()]}
        resolver, _metadata_client, _index = self.make_resolver(entries={"1292052": old_entry})

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")
        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())
        self.assertGreater(result.cache["age_ms"], 30 * 86400 * 1000)

    def test_entry_from_an_expired_index_version_is_marked_stale(self):
        payload = {
            "version": "current-v2",
            "generated_at": NOW,
            "entries": {"1292052": {
                "index_version": "expired-v1",
                "resolved_at": NOW,
                "lines": [_line()],
            }},
        }
        metadata = _FakeMetadataClient(_metadata())
        index = vod_resolver.StaticLineIndex(lambda: payload, clock=lambda: NOW)
        resolver = vod_resolver.VodResolver(metadata, index, _registry())

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertEqual(result.resolve_version, "current-v2")
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")
        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())

    def test_quality_score_is_not_a_substitute_for_match_confidence(self):
        line = _line()
        del line["match_score"]
        resolver, _metadata_client, _index = self.make_resolver(
            entries={"1292052": {"resolved_at": NOW, "lines": [line]}})

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertEqual(line["score"], 91)
        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_match_confidence_below_auto_threshold_is_not_playable(self):
        resolver, _metadata_client, _index = self.make_resolver(
            entries={"1292052": {"resolved_at": NOW, "lines": [_line(match_score=79)]}})

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_match_confidence_at_auto_threshold_is_playable(self):
        threshold = vod_sources.AUTO_MATCH_THRESHOLD
        resolver, _metadata_client, _index = self.make_resolver(
            entries={"1292052": {"resolved_at": NOW, "lines": [_line(match_score=threshold)]}})

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertEqual(result.preferred.match_score, threshold)

    def test_episode_less_than_one_is_rejected(self):
        resolver, _metadata_client, _index = self.make_resolver()

        with self.assertRaises(ValueError):
            resolver.resolve(mytv_id=10_000_123, episode=0)

    def test_episode_over_metadata_total_is_rejected(self):
        resolver, _metadata_client, _index = self.make_resolver(detail=_metadata(episodes=2))

        with self.assertRaises(ValueError):
            resolver.resolve(mytv_id=10_000_123, episode=3)

    def test_single_item_does_not_accept_a_series_episode_number(self):
        resolver, _metadata_client, _index = self.make_resolver(
            detail=_metadata(media_type="movie", episodes=0))

        with self.assertRaises(ValueError):
            resolver.resolve(mytv_id=10_000_123, episode=2)

    def test_missing_source_episode_is_not_clamped_to_a_different_episode(self):
        short_entry = {"resolved_at": NOW, "lines": [_line(episode_count=1)]}
        resolver, _metadata_client, _index = self.make_resolver(entries={"1292052": short_entry})

        result = resolver.resolve(mytv_id=10_000_123, episode=2)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_static_index_without_requested_episode_uses_dynamic_lookup(self):
        short_entry = {"resolved_at": NOW, "lines": [_line(episode_count=1)]}
        resolver, _metadata_client, _index = self.make_resolver(
            entries={"1292052": short_entry})
        registry = _registry()
        self.addCleanup(registry.close)
        future = concurrent.futures.Future()
        future.set_result((registry.sources[0], [_line()], None, 100))
        submitted = []

        def submit(subject, max_sources):
            submitted.append((subject, max_sources))
            return (future,)

        registry.submit_fast_searches = submit
        resolver.registry = registry

        result = resolver.resolve(mytv_id=10_000_123, episode=2)

        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][0]["title"], "早春晴朗")
        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.preferred.episode_index, 1)

    def test_late_source_cannot_become_preferred_without_an_on_time_candidate(self):
        resolver, _metadata_client, _index = self.make_resolver(
            entries={"different-key": {"resolved_at": NOW, "lines": []}})
        registry = _registry()
        self.addCleanup(registry.close)
        late = concurrent.futures.Future()
        registry.submit_fast_searches = lambda _subject, max_sources: (late,)
        resolver.registry = registry

        with patch.object(vod_resolver, "FAST_SOURCE_SOFT_TIMEOUT_SECONDS", 0.01):
            result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")
        self.assertIsNone(result.preferred)
        late.set_result((registry.sources[0], [_line()], None, 701))

        key = (10_000_123, 1, "2026092401")
        cached = resolver._hot_cache.get(key)
        self.assertNotIn(key, resolver._last_good)
        self.assertTrue(cached is None or cached[1].preferred is None)

    def test_late_source_only_joins_backups_behind_on_time_preferred(self):
        resolver, _metadata_client, _index = self.make_resolver(
            entries={"different-key": {"resolved_at": NOW, "lines": []}})
        registry = _registry(source_ids=("source-1", "source-2"))
        self.addCleanup(registry.close)
        on_time = concurrent.futures.Future()
        on_time.set_result((registry.sources[0], [_line(source_id="source-1")], None, 1))
        late = concurrent.futures.Future()
        registry.submit_fast_searches = lambda _subject, max_sources: (on_time, late)
        resolver.registry = registry

        with patch.object(vod_resolver, "FAST_SOURCE_SOFT_TIMEOUT_SECONDS", 0.01):
            result = resolver.resolve(mytv_id=10_000_123, episode=1)
            time.sleep(0.02)
            late.set_result((registry.sources[1], [_line(source_id="source-2")], None, 701))

        key = (10_000_123, 1, "2026092401")
        cached = resolver._last_good[key][1]
        self.assertEqual(result.preferred.source_id, "source-1")
        self.assertEqual(cached.preferred.source_id, "source-1")
        self.assertEqual([candidate.source_id for candidate in cached.backups], ["source-2"])

    def test_disabled_or_unknown_source_is_not_returned(self):
        metadata = _FakeMetadataClient(_metadata())
        index = _RecordingIndex({"1292052": {"resolved_at": NOW, "lines": [_line(source_id="unknown")]}})
        resolver = vod_resolver.VodResolver(metadata, index, _registry())

        self.assertIsNone(resolver.resolve(mytv_id=10_000_123, episode=1).preferred)

    def test_full_playback_url_cannot_become_a_public_content_id(self):
        line = dict(_line())
        line["content_id"] = "https://media.example/video.m3u8?token=secret"
        resolver, _metadata_client, _index = self.make_resolver(
            entries={"1292052": {"resolved_at": NOW, "lines": [line]}})

        result = resolver.resolve(mytv_id=10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertNotIn("secret", repr(result.to_public_dict()))

    def test_static_index_uses_exact_id_key_without_title_fallback(self):
        index = vod_resolver.StaticLineIndex(
            lambda: {"version": "v1", "generated_at": NOW,
                     "entries": {"different-id": {"title": "早春晴朗", "resolved_at": NOW,
                                                    "lines": [_line()]}}},
            clock=lambda: NOW)

        result = index.lookup("1292052")

        self.assertIsNone(result.entry)
        self.assertEqual(result.version, "v1")


if __name__ == "__main__":
    unittest.main()
