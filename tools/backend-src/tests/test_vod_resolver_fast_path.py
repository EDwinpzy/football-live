import pathlib
import sys
import threading
import time
import unittest
import concurrent.futures


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import vod_resolver  # noqa: E402
import vod_sources  # noqa: E402


NOW = 1_798_200_000
MYTV_ID = 10_000_123
DOUBAN_ID = "1292052"


def _metadata():
    return {
        "id": MYTV_ID,
        "mytv_id": MYTV_ID,
        "title": "早春晴朗",
        "media_type": "tv",
        "episodes": 2,
        "external_ids": {"douban": DOUBAN_ID},
    }


def _line(*, source_id="source-1", content_id="content-42", episode_indexes=(0, 1), match_score=90):
    return {
        "id": source_id + ":" + content_id,
        "source_id": source_id,
        "source_name": "线路甲",
        "height": 1080,
        "score": 91,
        "match_score": match_score,
        "episodes": [
            {"index": index, "name": "Episode %d" % (index + 1),
             "url": "https://media.example/%d.m3u8?token=private" % index}
            for index in episode_indexes
        ],
    }


class FakeMetadata:
    def detail(self, mytv_id):
        assert mytv_id == MYTV_ID
        return _metadata()


class MutableIndex:
    def __init__(self, entry=None, *, version="index-v1", stale=False):
        self.entry = entry
        self.version = version
        self.stale = stale
        self.lookups = []

    def lookup(self, douban_id):
        self.lookups.append(str(douban_id))
        entry = self.entry
        return vod_resolver.IndexLookup(
            entry=entry,
            version=self.version,
            age_ms=31 * 86400 * 1000 if self.stale else 0,
            stale=self.stale,
        )


def _registry(source_ids=("source-1",)):
    return vod_sources.SourceRegistry([
        vod_sources.VodSource(
            source_id, "Source " + source_id, "https://source.example", "https://api.example",
            "maccms", enabled=True)
        for source_id in source_ids
    ], version=17)


class ResolverFastPathCacheTest(unittest.TestCase):
    def make_resolver(self, line=None, *, index=None, clock=lambda: 0.0):
        entry = {"resolved_at": NOW, "lines": [line or _line()]}
        index = index or MutableIndex(entry)
        resolver = vod_resolver.VodResolver(
            FakeMetadata(), index, _registry(), clock=clock)
        return resolver, index

    def test_static_index_hit_is_url_free_and_does_not_search_sources(self):
        resolver, index = self.make_resolver()

        result = resolver.resolve(MYTV_ID, episode=1)

        self.assertEqual(index.lookups, [DOUBAN_ID])
        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.cache["kind"], "static_index")
        self.assertEqual(result.preferred.content_id, "content-42")
        self.assertNotIn("url", result.to_public_dict()["preferred"])
        self.assertNotIn("private", repr(result.to_public_dict()))

    def test_static_episode_without_server_index_fails_closed(self):
        line = _line(episode_indexes=())
        line["episodes"] = [{"name": "第1集", "url": "https://media.example/1.m3u8"}]
        resolver, _index = self.make_resolver(line)

        result = resolver.resolve(MYTV_ID, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_static_hit_is_cached_by_episode_and_index_version(self):
        clock = [0.0]
        index = MutableIndex({"resolved_at": NOW, "lines": [_line()]})
        resolver, _index = self.make_resolver(index=index, clock=lambda: clock[0])

        first = resolver.resolve(MYTV_ID, episode=1)
        cached_same_episode = resolver.resolve(MYTV_ID, episode=1)
        second_episode = resolver.resolve(MYTV_ID, episode=2)
        index.version = "index-v2"
        changed_version = resolver.resolve(MYTV_ID, episode=1)

        self.assertEqual(first.preferred.episode_index, 0)
        self.assertEqual(cached_same_episode.cache["kind"], "hot_cache")
        self.assertEqual(second_episode.cache["kind"], "static_index")
        self.assertEqual(second_episode.preferred.episode_index, 1)
        self.assertEqual(changed_version.cache["kind"], "static_index")
        self.assertEqual(changed_version.resolve_version, "index-v2")

    def test_hot_success_cache_expires_after_six_hours(self):
        clock = [0.0]
        resolver, _index = self.make_resolver(clock=lambda: clock[0])

        resolver.resolve(MYTV_ID, episode=1)
        clock[0] = 6 * 60 * 60 - 1
        hit = resolver.resolve(MYTV_ID, episode=1)
        clock[0] = 6 * 60 * 60
        expired = resolver.resolve(MYTV_ID, episode=1)

        self.assertEqual(hit.cache["kind"], "hot_cache")
        self.assertEqual(expired.cache["kind"], "static_index")

    def test_dynamic_cache_ttl_and_episode_key_isolation(self):
        clock = [0.0]
        registry = _registry(("source-1",))
        calls = []

        def trusted_search(source, _subject):
            calls.append(source.id)
            return source, [_line()], None, 10

        registry._search_one = trusted_search
        resolver, _index = self.make_resolver(
            index=MutableIndex(entry=None), clock=lambda: clock[0])
        resolver.registry = registry
        try:
            first = resolver.resolve(MYTV_ID, episode=1)
            recent = resolver.resolve(MYTV_ID, episode=1)
            second_episode = resolver.resolve(MYTV_ID, episode=2)
            clock[0] = vod_resolver.LAST_GOOD_TTL_SECONDS + 1
            hot = resolver.resolve(MYTV_ID, episode=1)
            clock[0] = vod_resolver.HOT_RESULT_TTL_SECONDS + 1
            refreshed = resolver.resolve(MYTV_ID, episode=1)
        finally:
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertEqual(first.preferred.episode_index, 0)
        self.assertEqual(recent.cache["kind"], "last_good")
        self.assertEqual(second_episode.cache["kind"], "miss")
        self.assertEqual(second_episode.preferred.episode_index, 1)
        self.assertEqual(hot.cache["kind"], "hot_cache")
        self.assertEqual(refreshed.cache["kind"], "miss")
        self.assertEqual(calls, ["source-1", "source-1", "source-1"])

    def test_cached_candidate_is_rechecked_when_source_registry_changes(self):
        old_registry = _registry(("source-1",))

        def trusted_search(source, _subject):
            return source, [_line()], None, 10

        old_registry._search_one = trusted_search
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = old_registry
        try:
            first = resolver.resolve(MYTV_ID, episode=1)
            resolver.registry = _registry(())
            after_source_retirement = resolver.resolve(MYTV_ID, episode=1)
        finally:
            for registry in (old_registry, resolver.registry):
                close = getattr(registry, "close", None)
                if close:
                    close()

        self.assertIsNotNone(first.preferred)
        self.assertIsNone(after_source_retirement.preferred)
        self.assertEqual(after_source_retirement.backups, ())

    def test_source_configuration_change_invalidates_same_id_cached_content(self):
        old_registry = _registry(("source-1",))
        old_registry._search_one = lambda source, _subject: (
            source, [_line(content_id="old-content")], None, 10)
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = old_registry
        new_registry = _registry(("source-1",))
        new_registry._search_one = lambda source, _subject: (
            source, [_line(content_id="new-content")], None, 10)
        try:
            first = resolver.resolve(MYTV_ID, episode=1)
            resolver.registry = new_registry
            resolver.invalidate_source_configuration()
            second = resolver.resolve(MYTV_ID, episode=1)
        finally:
            for registry in (old_registry, new_registry):
                close = getattr(registry, "close", None)
                if close:
                    close()

        self.assertEqual(first.preferred.content_id, "old-content")
        self.assertEqual(second.preferred.content_id, "new-content")
        self.assertEqual(second.cache["kind"], "miss")

    def test_failure_cache_expires_after_fifteen_seconds(self):
        clock = [0.0]
        registry = _registry(("source-1",))
        calls = []

        def no_result(source, _subject):
            calls.append(source.id)
            return source, [], None, 10

        registry._search_one = no_result
        resolver, _index = self.make_resolver(
            index=MutableIndex(entry=None), clock=lambda: clock[0])
        resolver.registry = registry
        try:
            first = resolver.resolve(MYTV_ID, episode=1)
            cached = resolver.resolve(MYTV_ID, episode=1)
            clock[0] = 16
            retried = resolver.resolve(MYTV_ID, episode=1)
        finally:
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertEqual(first.status, "TEMPORARILY_UNAVAILABLE")
        self.assertEqual(cached.cache["kind"], "failure")
        self.assertEqual(retried.status, "TEMPORARILY_UNAVAILABLE")
        self.assertEqual(calls, ["source-1", "source-1"])

    def test_stale_index_never_serves_cached_playable_candidates(self):
        index = MutableIndex({"resolved_at": NOW, "lines": [_line()]})
        resolver, _index = self.make_resolver(index=index)
        self.assertIsNotNone(resolver.resolve(MYTV_ID, episode=1).preferred)

        index.stale = True
        stale = resolver.resolve(MYTV_ID, episode=1)

        self.assertEqual(stale.cache["kind"], "stale_index")
        self.assertEqual(stale.status, "TEMPORARILY_UNAVAILABLE")
        self.assertIsNone(stale.preferred)
        self.assertEqual(stale.backups, ())

    def test_first_trusted_source_at_650ms_returns_before_slow_sources(self):
        registry = _registry(("fast", "slow-1", "slow-2", "disabled"))
        registry.sources = tuple(
            source if source.id != "disabled" else
            vod_sources.VodSource(source.id, source.name, source.site_url, source.api_url,
                                  source.adapter_type, enabled=False)
            for source in registry.sources)
        slow_release = threading.Event()
        started = {source.id: threading.Event() for source in registry.sources}
        calls = []

        def controlled_search(source, _subject):
            calls.append(source.id)
            started[source.id].set()
            if source.id == "fast":
                time.sleep(0.65)
                return source, [_line(source_id="fast", content_id="fast-content")], None, 650
            slow_release.wait()
            return source, [_line(source_id=source.id, content_id=source.id)], None, 2000

        registry._search_one = controlled_search
        index = MutableIndex(entry=None)
        resolver, _index = self.make_resolver(index=index)
        resolver.registry = registry
        elapsed_start = time.monotonic()
        try:
            result = resolver.resolve(MYTV_ID, episode=1)
            elapsed = time.monotonic() - elapsed_start
        finally:
            slow_release.set()
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertEqual(set(calls), {"fast", "slow-1", "slow-2"})
        self.assertTrue(all(started[source_id].is_set() for source_id in ("fast", "slow-1", "slow-2")))
        self.assertFalse(started["disabled"].is_set())
        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.preferred.source_id, "fast")
        self.assertLess(elapsed, 1.5)
        self.assertNotIn("url", result.to_public_dict()["preferred"])

    def test_no_trusted_result_is_bounded_by_the_1500ms_fast_budget(self):
        registry = _registry(("slow-1", "slow-2", "slow-3", "not-used"))
        release = threading.Event()
        started = {source.id: threading.Event() for source in registry.sources}
        calls = []

        def controlled_search(source, _subject):
            calls.append(source.id)
            started[source.id].set()
            if source.id == "slow-1":
                line = _line(source_id=source.id)
                line["episodes"] = [{"name": "Episode 1"}]
                return source, [line], None, 10
            release.wait()
            return source, [], None, 2000

        registry._search_one = controlled_search
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = registry
        began = time.monotonic()
        try:
            result = resolver.resolve(MYTV_ID, episode=1)
            elapsed = time.monotonic() - began
        finally:
            release.set()
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertEqual(set(calls), {"slow-1", "slow-2", "slow-3"})
        self.assertTrue(all(started[source_id].is_set() for source_id in calls))
        self.assertGreaterEqual(elapsed, 1.2)
        self.assertLess(elapsed, 2.2)
        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_unanswered_sources_stop_at_the_700ms_soft_timeout(self):
        registry = _registry(("slow-1", "slow-2", "slow-3"))
        release = threading.Event()
        started = {source.id: threading.Event() for source in registry.sources}

        def blocked_search(source, _subject):
            started[source.id].set()
            release.wait()
            return source, [], None, 2000

        registry._search_one = blocked_search
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = registry
        began = time.monotonic()
        try:
            result = resolver.resolve(MYTV_ID, episode=1)
            elapsed = time.monotonic() - began
        finally:
            release.set()
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertTrue(all(started[source_id].is_set() for source_id in started))
        self.assertGreaterEqual(elapsed, 0.55)
        self.assertLess(elapsed, 1.2)
        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())

    def test_request_cancellation_stops_waiting_and_cancels_pending_futures(self):
        registry = _registry(("slow-1", "slow-2", "slow-3"))
        release = threading.Event()
        started = {source.id: threading.Event() for source in registry.sources}

        def blocked_search(source, _subject):
            started[source.id].set()
            release.wait()
            return source, [], None, 2000

        registry._search_one = blocked_search
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = registry
        cancel_event = threading.Event()
        outcome = {}
        finished = threading.Event()

        def run_resolve():
            try:
                outcome["result"] = resolver.resolve(
                    MYTV_ID, episode=1, cancel_event=cancel_event)
            finally:
                finished.set()

        worker = threading.Thread(target=run_resolve)
        worker.start()
        try:
            self.assertTrue(all(started[source_id].wait(1) for source_id in started))
            cancel_event.set()
            self.assertTrue(finished.wait(0.5))
        finally:
            release.set()
            close = getattr(registry, "close", None)
            if close:
                close()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["result"].status, "TEMPORARILY_UNAVAILABLE")
        self.assertIsNone(outcome["result"].preferred)
        self.assertEqual(outcome["result"].backups, ())

    def test_unexpected_candidate_exception_cancels_other_pending_work(self):
        registry = _registry(("source-1",))
        completed = concurrent.futures.Future()
        pending = concurrent.futures.Future()
        completed.set_result((registry.sources[0], [], None, 10))
        registry.submit_fast_searches = lambda _subject, max_sources=3: (completed, pending)
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = registry

        def explode(_future, _episode, _registry=None, allow_late=False):
            raise RuntimeError("controlled candidate failure")

        resolver._future_candidates = explode
        try:
            result = resolver.resolve(MYTV_ID, episode=1)
        finally:
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertTrue(pending.cancelled())
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_dynamic_line_quality_score_cannot_replace_match_score(self):
        registry = _registry(("source-1",))
        calls = []

        def untrusted_search(source, _subject):
            calls.append(source.id)
            line = _line(match_score=79)
            line["score"] = 100
            return source, [line], None, 20

        registry._search_one = untrusted_search
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = registry

        try:
            result = resolver.resolve(MYTV_ID, episode=1)
        finally:
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertEqual(calls, ["source-1"])
        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())

    def test_source_result_after_700ms_soft_timeout_is_not_preferred(self):
        registry = _registry(("source-1",))
        registry._search_one = lambda source, _subject: (
            source, [_line()], None, 701)
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = registry

        try:
            result = resolver.resolve(MYTV_ID, episode=1)
        finally:
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertIsNone(result.preferred)
        self.assertEqual(result.backups, ())
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_late_trusted_source_is_added_as_a_backup_after_fast_return(self):
        registry = _registry(("fast", "slow"))
        release_slow = threading.Event()
        slow_done = threading.Event()

        def controlled_search(source, _subject):
            if source.id == "slow":
                release_slow.wait()
                slow_done.set()
                return source, [_line(source_id="slow", content_id="slow-content")], None, 2000
            return source, [_line(source_id="fast", content_id="fast-content")], None, 10

        registry._search_one = controlled_search
        resolver, _index = self.make_resolver(index=MutableIndex(entry=None))
        resolver.registry = registry
        try:
            first = resolver.resolve(MYTV_ID, episode=1)
            self.assertEqual(first.preferred.source_id, "fast")
            self.assertEqual(first.backups, ())
            release_slow.set()
            self.assertTrue(slow_done.wait(1))

            deadline = time.monotonic() + 1
            later = None
            while time.monotonic() < deadline:
                later = resolver.resolve(MYTV_ID, episode=1)
                if later.backups:
                    break
                time.sleep(0.01)
        finally:
            release_slow.set()
            close = getattr(registry, "close", None)
            if close:
                close()

        self.assertIsNotNone(later)
        self.assertEqual(later.cache["kind"], "last_good")
        self.assertEqual(later.preferred.source_id, "fast")
        self.assertEqual([line.source_id for line in later.backups], ["slow"])


if __name__ == "__main__":
    unittest.main()
