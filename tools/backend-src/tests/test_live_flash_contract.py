import importlib.util
import copy
import pathlib
import sys
import unittest
from datetime import datetime
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("proxy_live_flash", ROOT / "proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class LiveFlashContractTest(unittest.TestCase):
    def test_failed_cold_schedule_returns_error_without_retired_cloud_fallback(self):
        previous_cache = copy.deepcopy(proxy._cache)
        proxy._cache.update(ts=0, data=None, raw_error=None, refreshing=False)
        try:
            with mock.patch.object(proxy, "fetch_source", side_effect=TimeoutError("source timeout")), \
                    mock.patch.object(proxy, "_cloud_get_json") as cloud:
                payload, cached = proxy.build_api(wait_for_fresh=True)
            self.assertFalse(cached)
            self.assertEqual([], payload["matches"])
            self.assertEqual("source timeout", payload["error"])
            cloud.assert_not_called()
        finally:
            proxy._cache.clear()
            proxy._cache.update(previous_cache)
            proxy._cache_refresh_event.set()

    def test_background_schedule_refresh_keeps_proxy_context(self):
        previous_cache = copy.deepcopy(proxy._cache)
        observed = []
        proxy._cache.update(ts=0, data={"matches": []}, raw_error=None, refreshing=False)
        def refresh():
            observed.append(proxy._active_upstream_proxy())
            with proxy._cache_lock:
                proxy._cache["refreshing"] = False
            proxy._cache_refresh_event.set()
        token = proxy._UPSTREAM_PROXY_URL.set(proxy.FOOTBALL_PROXY_URL)
        try:
            with mock.patch.object(proxy, "_refresh_matches_bg", side_effect=refresh):
                proxy.build_api()
                self.assertTrue(proxy._cache_refresh_event.wait(timeout=2))
            self.assertEqual([proxy.FOOTBALL_PROXY_URL], observed)
        finally:
            proxy._UPSTREAM_PROXY_URL.reset(token)
            proxy._cache.clear()
            proxy._cache.update(previous_cache)
            proxy._cache_refresh_event.set()

    def test_live_kickoff_time_survives_minute_projection(self):
        original_now = proxy._now_cn
        proxy._now_cn = lambda: datetime(2026, 9, 13, 22, 40)
        try:
            html = (
                '<li data-role="list-divider">2026年09月13日</li>'
                '<li><a href="#page_45490631">'
                '<span class="live">22:30</span> 罗马 VS 国际米兰'
                '</a><li>'
                '<div id="page_45490631">'
                '<a href="/tv/bb-45490631.html">高清</a>'
                '</div>'
            )
            parsed = proxy.parse_matches(html)
        finally:
            proxy._now_cn = original_now

        self.assertEqual(1, len(parsed))
        self.assertEqual("22:30", parsed[0]["time"])
        projected = proxy.project_match_state(parsed[0], datetime(2026, 9, 14, 0, 20))
        # 110 minutes is no longer enough to infer a final state: stoppage and
        # delayed source updates can leave a regular match running this long.
        self.assertEqual("live", projected["status"])
        self.assertEqual("90", projected["minute"])

    def test_regular_match_finish_grace_boundary(self):
        item = {
            "date_full": "2026-09-13",
            "date": "09-13",
            "time": "20:00",
            "league": "Premier League",
            "status": "upcoming",
        }

        before_grace = proxy.project_match_state(item, datetime(2026, 9, 13, 22, 29))
        at_grace = proxy.project_match_state(item, datetime(2026, 9, 13, 22, 30))

        self.assertEqual("live", before_grace["status"])
        self.assertEqual("finished", at_grace["status"])

    def test_knockout_match_finish_grace_boundary(self):
        item = {
            "date_full": "2026-09-13",
            "date": "09-13",
            "time": "20:00",
            "league": "欧冠淘汰赛",
            "status": "upcoming",
        }

        before_grace = proxy.project_match_state(item, datetime(2026, 9, 13, 23, 29))
        at_grace = proxy.project_match_state(item, datetime(2026, 9, 13, 23, 30))

        self.assertEqual("live", before_grace["status"])
        self.assertEqual("finished", at_grace["status"])

    def test_full_date_projects_across_midnight_and_new_year(self):
        item = {
            "date_full": "2026-12-31",
            "date": "12-31",
            "time": "23:50",
            "status": "upcoming",
        }

        got = proxy.project_match_state(item, datetime(2027, 1, 1, 0, 10))

        self.assertEqual("live", got["status"])
        self.assertEqual("20", got["minute"])

    def test_legacy_short_date_near_new_year_does_not_roll_to_next_year(self):
        item = {"date": "12-31", "time": "21:00", "status": "upcoming"}

        got = proxy.project_match_state(item, datetime(2027, 1, 1, 0, 0))

        self.assertEqual("finished", got["status"])

    def test_false_live_before_kickoff_is_corrected_but_deferred_states_are_held(self):
        kickoff = {"date_full": "2026-10-11", "time": "20:00", "status": "live", "minute": ""}
        deferred = [
            {**kickoff, "status": "postponed"},
            {**kickoff, "status": "pending"},
            {**kickoff, "status": "cancelled"},
            {**kickoff, "status": "abandoned"},
        ]

        before_kickoff = proxy.project_match_state(kickoff, datetime(2026, 10, 11, 19, 59))
        self.assertEqual("upcoming", before_kickoff["status"])
        for item in deferred:
            self.assertEqual(item["status"], proxy.project_match_state(item, datetime(2026, 10, 12, 12, 0))["status"])

    def test_authoritative_7m_state_is_preserved_and_private_marker_is_stripped(self):
        item = {
            "date_full": "2026-10-11",
            "time": "20:00",
            "status": "live",
            "minute": "72",
            "_status_authoritative": True,
        }
        payload = {"matches": [item]}

        projected = proxy._project_matches_payload(payload, datetime(2026, 10, 12, 12, 0))

        self.assertEqual("live", projected["matches"][0]["status"])
        self.assertEqual("72", projected["matches"][0]["minute"])
        self.assertNotIn("_status_authoritative", projected["matches"][0])

    def test_fresh_api_waits_for_single_flight_refresh_and_strips_internal_state(self):
        html = '''
        <li class="live-list-group-time">2026-10-11</li>
        <li class="group-game-item" data-id="880901">
          <span class="game-type">英超</span>
          <span class="team-name">主队</span><span class="team-name">客队</span>
          <span class="category-game-time">21:00</span>
          <a href="/live/880901/play">直播中</a>
        </li>
        '''
        previous_cache = copy.deepcopy(proxy._cache)
        proxy._cache.update(
            ts=0,
            data={"matches": [{"match_id": "stale", "status": "upcoming"}]},
            raw_error=None,
            refreshing=False,
        )
        proxy._cache_refresh_event.set()
        try:
            with mock.patch.object(proxy, "_now_cn", return_value=datetime(2026, 10, 11, 21, 10)), \
                    mock.patch.object(proxy, "fetch_source", return_value=html) as fetch, \
                    mock.patch.object(proxy, "_fetch_7m_scores", return_value={}):
                payload, cached = proxy.build_api(wait_for_fresh=True)

            self.assertFalse(cached)
            self.assertEqual(["880901"], [item["match_id"] for item in payload["matches"]])
            self.assertEqual("live", payload["matches"][0]["status"])
            self.assertNotIn("_status_authoritative", payload["matches"][0])
            fetch.assert_called_once()
        finally:
            proxy._cache.clear()
            proxy._cache.update(previous_cache)
            proxy._cache_refresh_event.set()

    def test_cached_upcoming_projects_to_live_at_kickoff(self):
        item = {"date": "09-13", "time": "20:00", "status": "upcoming", "minute": ""}
        got = proxy.project_match_state(item, datetime(2026, 9, 13, 20, 0))
        self.assertEqual("live", got["status"])
        self.assertEqual("0", got["minute"])

    def test_finished_state_is_never_reopened(self):
        item = {"date": "09-13", "time": "20:00", "status": "finished", "minute": "90"}
        self.assertEqual("finished", proxy.project_match_state(item, datetime(2026, 9, 13, 20, 10))["status"])


if __name__ == "__main__":
    unittest.main()
