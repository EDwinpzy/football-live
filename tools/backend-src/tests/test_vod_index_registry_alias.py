"""Static-index source identity and freshness must survive a published registry swap.

The static line index is produced on a workstation and pushed to the server as
JSON, so its ``source_id`` is whatever the local registry called the source at
build time. The published registry that serves requests is free to use different
identifiers for the same source (the admin API mints UUIDs). These tests lock the
join that makes an index entry usable anyway, and the two freshness rules that
decide when a stale entry may still be served.
"""

import concurrent.futures
import pathlib
import sys
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import vod_resolver  # noqa: E402
import vod_sources  # noqa: E402


NOW = 1_798_200_000
DAY = 86400


def _detail(douban_id="1292052", *, mytv_id=10_000_123, episodes=2, media_type="tv"):
    return {
        "id": mytv_id,
        "mytv_id": mytv_id,
        "title": "早春晴朗",
        "media_type": media_type,
        "episodes": episodes,
        "external_ids": {"douban": douban_id},
    }


def _line(*, source_id="hongniu", content_id="192708", episodes=1, match_score=90,
           source_name="红牛", source_alias="hongniuzy2.com"):
    return {
        "id": "%s:%s:0" % (source_id, content_id),
        "source_id": source_id,
        "source_name": source_name,
        "source_alias": source_alias,
        "name": "%s·主线" % source_name,
        "match_score": match_score,
        "episodes": [
            {"index": index, "name": "第%d集" % (index + 1),
             "url": "https://media.example/%d.m3u8" % index}
            for index in range(episodes)
        ],
    }


class _Metadata:
    def __init__(self, detail):
        self._detail = detail

    def detail(self, _mytv_id):
        return self._detail


class StaticIndexAliasTest(unittest.TestCase):
    """An index line joins the published registry by identity, not by raw id."""

    def _resolver(self, entry, sources, *, index_now=NOW, clock=lambda: NOW):
        payload = {"version": "2026092801", "generated_at": index_now, "entries": {
            "1292052": dict(entry, resolved_at=entry.get("resolved_at", index_now)),
        }}
        index = vod_resolver.StaticLineIndex(lambda: payload, clock=clock)
        registry = vod_sources.SourceRegistry(sources, version=3)
        self.addCleanup(registry.close)
        return vod_resolver.VodResolver(_Metadata(_detail()), index, registry)

    def test_line_built_under_a_short_id_still_resolves_to_its_published_uuid_source(self):
        """A rebuilt index keeps local ids; the published registry uses UUIDs.

        The production break this prevents: every line is dropped because the
        index says "hongniu" while the registry only knows
        "10000000-0000-0000-0000-000000000007", so the web detail page shows
        "no source" even though the index is fresh and complete.
        """
        published = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000007", "红牛",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json")
        resolver = self._resolver(
            {"lines": [_line(source_id="hongniu", source_name="红牛")]}, [published])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.preferred.source_id, "10000000-0000-0000-0000-000000000007")
        self.assertEqual(result.preferred.content_id, "192708")
        self.assertEqual(result.preferred.source_name, "红牛")

    def test_resolved_source_id_is_the_published_one_so_playback_uses_the_registry(self):
        published = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000007", "红牛",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json")
        resolver = self._resolver(
            {"lines": [_line(source_id="hongniu", source_name="红牛")]}, [published])

        result = resolver.resolve(10_000_123, episode=1)

        # The play step looks the line up by source_id in the live registry, so
        # handing back the index's local id would 404 there.
        registry_ids = {published.id}
        self.assertIn(result.preferred.source_id, registry_ids)
        self.assertNotEqual(result.preferred.source_id, "hongniu")

    def test_exact_id_match_still_wins_when_the_registry_keeps_the_short_name(self):
        source = vod_sources.VodSource(
            "hongniu", "红牛", "https://www.hongniuzy2.com",
            "https://www.hongniuzy2.com/api.php/provide/vod/", "macms_json")
        resolver = self._resolver({"lines": [_line(source_id="hongniu")]}, [source])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.preferred.source_id, "hongniu")

    def test_line_whose_source_is_absent_from_the_registry_is_not_returned(self):
        unrelated = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000001", "好好看",
            "https://www.haohankan.com", "", "hhkan")
        resolver = self._resolver({"lines": [_line(source_id="hongniu")]}, [unrelated])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_two_published_sources_sharing_a_host_are_not_guessed_between(self):
        """Aliasing must stay deterministic when a site is registered twice.

        Picking the first match would silently route a line to whichever source
        the admin happened to list first, which can be a different adapter.
        A duplicate registration is indistinguishable by every key we have, so
        the line is dropped rather than guessed.
        """
        first = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000007", "红牛",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json")
        duplicate = vod_sources.VodSource(
            "10000000-0000-0000-0000-00000000000a", "红牛",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json")
        resolver = self._resolver(
            {"lines": [_line(source_id="hongniu")]}, [first, duplicate])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_a_rebuilt_index_joins_on_a_recorded_alias_instead_of_a_display_name(self):
        """A rebuilt line records a host alias, so a renamed source still matches.

        Display names are editable in the admin UI; the host is not. Once the
        index carries an alias, an admin renaming "红牛" must not orphan every
        line built before the rename.
        """
        renamed = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000007", "红牛高清",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json")
        line = _line(source_id="hongniu", source_name="红牛")
        line["source_alias"] = "hongniuzy2.com"
        resolver = self._resolver({"lines": [line]}, [renamed])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.preferred.source_id, renamed.id)
        self.assertEqual(result.preferred.source_name, "红牛高清")

    def test_a_legacy_line_without_an_alias_is_not_guessed_from_its_display_name(self):
        """The index already on the server has no alias field at all.

        A display name is editable in the admin UI and is not an identity, so
        the 2178 shipped lines cannot be joined by name without risking routing
        a line to the wrong source. They stay unusable until the index is
        rebuilt with recorded aliases -- a rebuild the builder does anyway.
        """
        published = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000007", "红牛",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json")
        line = _line(source_id="hongniu", source_name="红牛")
        del line["source_alias"]
        self.assertNotIn("source_alias", line)
        resolver = self._resolver({"lines": [line]}, [published])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")

    def test_a_line_whose_name_also_matches_nothing_is_not_returned(self):
        published = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000007", "红牛",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json")
        line = _line(source_id="hongniu", source_name="完全不同的名字")
        del line["source_alias"]
        resolver = self._resolver(
            {"lines": [line]}, [published])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertIsNone(result.preferred)

    def test_alias_never_matches_a_disabled_published_source(self):
        disabled = vod_sources.VodSource(
            "10000000-0000-0000-0000-000000000007", "红牛",
            "https://www.hongniuzy2.com", "https://www.hongniuzy2.com/api.php/provide/vod/",
            "macms_json", enabled=False)
        resolver = self._resolver(
            {"lines": [_line(source_id="hongniu")]}, [disabled])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertIsNone(result.preferred)
        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")


class StaleIndexServingTest(unittest.TestCase):
    """A stale index may still be served, but only as STALE_AVAILABLE.

    The old code returned TEMPORARILY_UNAVAILABLE the moment the index was
    older than the freshness window, which made the documented STALE_AVAILABLE
    status unreachable and left the web page with nothing to offer.
    """

    def _resolver(self, *, age_days, sources, max_age_days=30, episode_count=2):
        resolved_at = NOW - int(age_days * DAY)
        entry = {"lines": [_line(episodes=episode_count)], "resolved_at": resolved_at}
        payload = {"version": "2026092801", "generated_at": resolved_at,
                   "entries": {"1292052": entry}}
        index = vod_resolver.StaticLineIndex(
            lambda: payload, clock=lambda: NOW,
            max_age_seconds=max_age_days * DAY)
        registry = vod_sources.SourceRegistry(sources, version=3)
        self.addCleanup(registry.close)
        return vod_resolver.VodResolver(
            _Metadata(_detail(episodes=3)), index, registry)

    def _source(self, source_id="hongniu"):
        return vod_sources.VodSource(
            source_id, "红牛", "https://www.hongniuzy2.com",
            "https://www.hongniuzy2.com/api.php/provide/vod/", "macms_json")

    def test_fresh_index_is_served_as_matched(self):
        resolver = self._resolver(age_days=1, sources=[self._source()])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(result.status, "MATCHED")
        self.assertEqual(result.cache["kind"], "static_index")

    def test_stale_index_still_serves_lines_as_stale_available(self):
        resolver = self._resolver(age_days=45, sources=[self._source()])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(result.status, "STALE_AVAILABLE")
        self.assertIsNotNone(result.preferred)
        self.assertEqual(result.cache["kind"], "stale_index")

    def test_stale_result_never_claims_to_be_a_fresh_match(self):
        """Clients key refresh behaviour off cache kind, not just the status."""
        resolver = self._resolver(age_days=45, sources=[self._source()])

        result = resolver.resolve(10_000_123, episode=1)

        self.assertNotEqual(result.cache["kind"], "static_index")

    def test_a_replayed_stale_hit_still_reports_itself_as_stale(self):
        """The second read must not launder a stale line into a fresh match.

        The client refreshes in the background only when it sees a stale
        status, so replaying the cached answer as MATCHED would silently stop
        the index from ever being refreshed.
        """
        resolver = self._resolver(age_days=45, sources=[self._source()])

        first = resolver.resolve(10_000_123, episode=1)
        second = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(first.status, "STALE_AVAILABLE")
        self.assertEqual(second.status, "STALE_AVAILABLE")
        self.assertIsNotNone(second.preferred)

    def test_stale_index_falls_through_to_a_live_search_when_lines_do_not_match(self):
        """Serving stale data must not cost the request its fresh-search path."""
        # The stale index holds no line for this episode, so the resolver has to
        # go and look for a current one rather than serving the stale answer.
        resolver = self._resolver(age_days=45, sources=[self._source()],
                                  episode_count=1)
        registry = resolver.registry
        live_line = _line(episodes=3)
        fresh = concurrent.futures.Future()
        fresh.set_result((registry.sources[0], [live_line], None, 50))
        submitted = []

        def submit(subject, max_sources):
            submitted.append((subject, max_sources))
            return (fresh,)

        registry.submit_fast_searches = submit

        result = resolver.resolve(10_000_123, episode=3)

        self.assertEqual(len(submitted), 1)
        self.assertEqual(result.status, "MATCHED")

    def test_version_mismatch_marks_the_entry_stale_rather_than_usable(self):
        payload = {
            "version": "v2",
            "generated_at": NOW,
            "entries": {"1292052": {
                "index_version": "v1", "resolved_at": NOW,
                "lines": [_line()],
            }},
        }
        index = vod_resolver.StaticLineIndex(lambda: payload, clock=lambda: NOW)
        registry = vod_sources.SourceRegistry([self._source()], version=3)
        self.addCleanup(registry.close)
        resolver = vod_resolver.VodResolver(_Metadata(_detail()), index, registry)

        result = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(result.status, "STALE_AVAILABLE")
        self.assertEqual(result.cache["kind"], "stale_index")

    def test_absent_entry_is_still_a_miss_not_a_stale_serve(self):
        payload = {"version": "v1", "generated_at": NOW, "entries": {}}
        index = vod_resolver.StaticLineIndex(lambda: payload, clock=lambda: NOW)
        registry = vod_sources.SourceRegistry([self._source()], version=3)
        self.addCleanup(registry.close)
        resolver = vod_resolver.VodResolver(_Metadata(_detail()), index, registry)
        submitted = []
        registry.submit_fast_searches = lambda subject, max_sources: (
            submitted.append(subject) or concurrent.futures.Future())

        result = resolver.resolve(10_000_123, episode=1)

        self.assertEqual(result.status, "TEMPORARILY_UNAVAILABLE")
        self.assertEqual(result.cache["kind"], "miss")
        self.assertEqual(len(submitted), 1)


if __name__ == "__main__":
    unittest.main()
