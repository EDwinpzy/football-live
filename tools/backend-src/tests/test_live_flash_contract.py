import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("proxy_live_flash", ROOT / "proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class LiveFlashContractTest(unittest.TestCase):
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
        self.assertEqual("finished", projected["status"])

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
