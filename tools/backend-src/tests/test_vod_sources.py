import pathlib
import sys
import tempfile
import unittest
import urllib.parse


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import vod_sources  # noqa: E402


class VodSourcesTest(unittest.TestCase):
    def test_default_registry_contains_current_nine_sources(self):
        registry = vod_sources.SourceRegistry.load_default()
        self.assertEqual([s.name for s in registry.sources],
                         ["好好看", "暴风", "极速", "金鹰", "虎牙", "豪华", "红牛", "速播", "360"])

    def test_discover_maccms_home_normalizes_api(self):
        def fake_fetch(url):
            if url.endswith("/api.php/provide/vod/?ac=list"):
                return {"final_url": url, "content_type": "application/json", "body": '{"code":1,"list":[]}'}
            return {"final_url": url, "content_type": "text/html", "body": "<html></html>"}

        result = vod_sources.discover_source("https://example.test", fake_fetch)
        self.assertEqual(result.adapter_type, "macms_json")
        self.assertEqual(result.api_url, "https://example.test/api.php/provide/vod/")

    def test_discovery_rejects_private_redirect(self):
        def redirects_to_private(url):
            return {"final_url": "http://127.0.0.1/admin", "content_type": "text/html", "body": ""}

        with self.assertRaises(vod_sources.UnsafeSourceUrl):
            vod_sources.discover_source("https://example.test", redirects_to_private)

    def test_alias_exact_year_type_and_season_auto_matches(self):
        subject = {"title": "庆余年 第二季", "aliases": ["庆余年2"], "year": "2024", "category": "tv", "season": 2}
        candidate = {"title": "庆余年2", "year": "2024", "category": "tv", "season": 2}
        self.assertEqual(vod_sources.match_score(subject, candidate), 100)

    def test_same_title_wrong_year_does_not_auto_match(self):
        subject = {"title": "电影甲", "year": "2024", "category": "movie", "season": 0}
        candidate = {"title": "电影甲", "year": "2004", "category": "movie", "season": 0}
        self.assertLess(vod_sources.match_score(subject, candidate), vod_sources.AUTO_MATCH_THRESHOLD)

    def test_punctuation_difference_still_matches(self):
        subject = {"title": "蜘蛛侠：英雄无归", "year": "2021", "category": "movie", "season": 0}
        candidate = {"title": "蜘蛛侠英雄无归", "year": "2021", "category": "movie", "season": 0}
        self.assertGreaterEqual(vod_sources.match_score(subject, candidate), vod_sources.AUTO_MATCH_THRESHOLD)

    def test_rank_prefers_quality_latency_and_reliability(self):
        ranked = vod_sources.rank_lines([
            {"id": "slow-1080", "height": 1080, "latency_ms": 5000, "alive": True},
            {"id": "fast-1080", "height": 1080, "latency_ms": 800, "alive": True},
            {"id": "fast-720", "height": 720, "latency_ms": 500, "alive": True},
            {"id": "dead-4k", "height": 2160, "latency_ms": 200, "alive": False},
        ], {"fast-1080": 0.9, "slow-1080": 1.0, "fast-720": 1.0})
        self.assertEqual([x["id"] for x in ranked], ["fast-1080", "fast-720", "slow-1080", "dead-4k"])
        self.assertFalse(ranked[-1]["available"])

    def test_history_keeps_only_latest_twenty_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = vod_sources.LineHistory(pathlib.Path(tmp) / "history.json")
            for i in range(25):
                history.record("line-a", i % 2 == 0)
            self.assertEqual(len(history.attempts("line-a")), 20)
            self.assertEqual(history.success_rate("line-a"), 0.5)

    def test_maccms_adapter_builds_stable_episode_lines(self):
        source = vod_sources.VodSource("demo", "演示", "https://example.test", "https://example.test/api.php/provide/vod/", "macms_json")
        adapter = vod_sources.MacCmsAdapter(source, fetch=lambda _url: {
            "body": '{"list":[{"vod_id":7,"vod_name":"庆余年2","vod_year":"2024","type_name":"国产剧","vod_play_from":"line-a","vod_play_url":"第1集$https://v.example/1.m3u8#第2集$https://v.example/2.m3u8"}]}'
        })
        candidate = adapter.search({"title": "庆余年2"})[0]
        lines = adapter.detail(candidate)
        self.assertEqual(lines[0]["id"], "demo:7:0")
        self.assertEqual(lines[0]["episodes"][1]["name"], "第2集")
        self.assertEqual(adapter.resolve(lines[0], 1)["url"], "https://v.example/2.m3u8")

    def test_maccms_search_falls_back_to_base_title_when_primary_has_no_result(self):
        source = vod_sources.VodSource(
            "demo", "演示", "https://example.test",
            "https://example.test/api.php/provide/vod/", "macms_json")
        calls = []

        def fake_fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("wd", [""])[0]
            calls.append(query)
            if query == "凡人修仙传":
                return {"body": '{"list":[{"vod_id":99260,"vod_name":"凡人修仙传 慕兰之战",'
                                 '"vod_year":"2026","type_name":"动漫",'
                                 '"vod_play_from":"360zy",'
                                 '"vod_play_url":"第1集$https://v.example/1.m3u8"}]}' }
            return {"body": '{"list":[]}' }

        adapter = vod_sources.MacCmsAdapter(source, fetch=fake_fetch)
        candidates = adapter.search({"title": "凡人修仙传 年番4", "year": "2026", "category": "anime"})
        self.assertEqual([candidate["title"] for candidate in candidates], ["凡人修仙传 慕兰之战"])
        self.assertEqual(calls, ["凡人修仙传 年番4", "凡人修仙传"])

    def test_maccms_search_stops_after_confident_primary_match(self):
        source = vod_sources.VodSource(
            "demo", "演示", "https://example.test",
            "https://example.test/api.php/provide/vod/", "macms_json")
        calls = []

        def fake_fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("wd", [""])[0]
            calls.append(query)
            return {"body": '{"list":[{"vod_id":7,"vod_name":"庆余年2",'
                             '"vod_year":"2024","type_name":"国产剧",'
                             '"vod_play_from":"line-a",'
                             '"vod_play_url":"第1集$https://v.example/1.m3u8"}]}' }

        adapter = vod_sources.MacCmsAdapter(source, fetch=fake_fetch)
        candidates = adapter.search({"title": "庆余年2", "year": "2024", "category": "tv", "season": 2})
        self.assertEqual(len(candidates), 1)
        self.assertEqual(calls, ["庆余年2"])

    def test_maccms_search_prioritizes_specific_alias_over_episode_alias(self):
        source = vod_sources.VodSource(
            "demo", "演示", "https://example.test",
            "https://example.test/api.php/provide/vod/", "macms_json")
        calls = []

        def fake_fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("wd", [""])[0]
            calls.append(query)
            if query == "凡人修仙传：慕兰之战":
                return {"body": '{"list":[{"vod_id":99260,"vod_name":"凡人修仙传 慕兰之战",'
                                 '"vod_year":"2026","type_name":"动漫",'
                                 '"vod_play_from":"360zy",'
                                 '"vod_play_url":"第1集$https://v.example/1.m3u8"}]}' }
            return {"body": '{"list":[]}' }

        adapter = vod_sources.MacCmsAdapter(source, fetch=fake_fetch)
        candidates = adapter.search({
            "title": "凡人修仙传 年番4", "year": "2026", "category": "anime",
            "aliases": ["凡人修仙传 第177-228集", "凡人修仙传 年番4之慕兰之战", "凡人修仙传：慕兰之战"],
        })
        self.assertEqual([candidate["title"] for candidate in candidates], ["凡人修仙传 慕兰之战"])
        self.assertEqual(calls, ["凡人修仙传 年番4", "凡人修仙传", "凡人修仙传：慕兰之战"])

    def test_maccms_adapter_resolves_player_page_to_media_url(self):
        source = vod_sources.VodSource("demo", "演示", "https://example.test", "https://example.test/api.php/provide/vod/", "macms_json")
        calls = []

        def fake_fetch(url):
            calls.append(url)
            if url.endswith("?ac=detail&wd=%E5%BA%86%E4%BD%99%E5%B9%B42"):
                return {"body": '{"list":[{"vod_id":7,"vod_name":"庆余年2","vod_play_from":"line-a","vod_play_url":"第1集$https://player.example/play/7"}]}' }
            if url == "https://player.example/play/7":
                return {"final_url": url, "body": "<script>const vid = 'https://cdn.example/episode/index.m3u8';</script>"}
            raise AssertionError(url)

        adapter = vod_sources.MacCmsAdapter(source, fetch=fake_fetch)
        candidate = adapter.search({"title": "庆余年2"})[0]
        line = adapter.detail(candidate)[0]
        self.assertEqual(adapter.resolve(line, 0)["url"], "https://cdn.example/episode/index.m3u8")
        self.assertIn("https://player.example/play/7", calls)

    def test_maccms_adapter_unwraps_jisu_player_query_to_m3u8(self):
        source = vod_sources.VodSource(
            "demo", "演示", "https://example.test",
            "https://example.test/api.php/provide/vod/", "macms_json")
        media = "https://cdn.example/episode/index.m3u8?token=a%2Bb"
        wrapper = "https://jisuzyjx.com/play/?url=" + urllib.parse.quote(media, safe="")
        adapter = vod_sources.MacCmsAdapter(source, fetch=lambda _url: self.fail("不应抓取播放器包装页"))
        line = {"id": "demo:7:0", "episodes": [{"index": 0, "name": "第1集", "url": wrapper}]}
        self.assertEqual(adapter.resolve(line, 0)["url"], media)

    def test_hhkan_adapter_searches_and_resolves_episode(self):
        class FakeHhkan:
            @staticmethod
            def get_search_token(): return "token"
            @staticmethod
            def get_page(path):
                if path.startswith("/search"):
                    return '<a href="/detail/7.html" class="search-result-item"><div class="title">庆余年2</div></a>'
                if path == "/detail/7.html":
                    return "detail"
                if path == "/play/7-2-3.html":
                    return "play"
                raise AssertionError(path)
            @staticmethod
            def parse_detail(_html, vid=0):
                return {"id": vid, "title": "庆余年2", "year": "2024", "meta": "电视剧", "sources": [
                    {"name": "好好看", "episodes": [{"ep": "第1集", "pid": 2, "vid": 3}]}
                ]}
            @staticmethod
            def parse_play_page(_html):
                return {"sources": [{"name": "好好看", "url": "https://v.example/1.m3u8"}]}

        source = vod_sources.VodSource("hhkan", "好好看", "https://www.hhkan0.com", "", "hhkan")
        adapter = vod_sources.HhkanAdapter(source, module=FakeHhkan)
        candidate = adapter.search({"title": "庆余年2"})[0]
        line = adapter.detail(candidate)[0]
        self.assertEqual(line["id"], "hhkan:7:2")
        self.assertEqual(adapter.resolve(line, 0)["url"], "https://v.example/1.m3u8")

    def test_probe_playlist_reports_latency_and_best_height(self):
        result = vod_sources.probe_stream("https://v.example/master.m3u8", fetch=lambda _url: {
            "elapsed_ms": 420,
            "body": '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720\n720.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2,RESOLUTION=1920x1080\n1080.m3u8',
        })
        self.assertTrue(result["alive"])
        self.assertEqual(result["height"], 1080)
        self.assertEqual(result["latency_ms"], 420)

    def test_probe_rejects_successful_html_error_page(self):
        result = vod_sources.probe_stream("https://v.example/master.m3u8", fetch=lambda _url: {
            "elapsed_ms": 80,
            "body": "<!doctype html><html><body>Access denied</body></html>",
        })
        self.assertFalse(result["alive"])

    def test_detail_can_wait_for_all_matching_sources(self):
        class FakeAdapter:
            def __init__(self, source): self.source = source
            def search(self, _subject):
                return [{"title": "兰香如故", "year": "2026", "category": "tv", "season": 0}]
            def detail(self, _candidate):
                return [{"id": self.source.id + ":1", "source_id": self.source.id,
                         "name": self.source.name, "alive": True,
                         "episodes": [{"index": 0, "name": "第1集", "url": "https://v.example/1.m3u8"}]}]

        old = vod_sources.MacCmsAdapter
        vod_sources.MacCmsAdapter = FakeAdapter
        try:
            registry = vod_sources.SourceRegistry([
                vod_sources.VodSource("first", "首源", "https://first.example", "https://first.example/api", "macms_json"),
                vod_sources.VodSource("second", "360", "https://360.example", "https://360.example/api", "macms_json"),
            ], probe=lambda _url: {"alive": True, "height": 1080, "latency_ms": 1})
            lines = registry.resolve_subject({"title": "兰香如故", "year": "2026", "category": "tv"}, wait_for_all=True)
            self.assertEqual({line["source_id"] for line in lines}, {"first", "second"})
        finally:
            vod_sources.MacCmsAdapter = old


if __name__ == "__main__":
    unittest.main()
