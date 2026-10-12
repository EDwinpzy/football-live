import pathlib
import json
import sys
import tempfile
import unittest
import urllib.parse


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import vod_sources  # noqa: E402


class VodSourcesTest(unittest.TestCase):
    def test_returned_sources_are_cached_while_slow_sources_finish(self):
        import concurrent.futures
        from unittest.mock import Mock
        source = vod_sources.VodSource("test", "test", "https://example.test", "https://example.test/api", "macms_json")
        registry = vod_sources.SourceRegistry([source])
        self.addCleanup(registry.close)
        pending = concurrent.futures.Future()
        line = {"id": "test:1:0", "source_id": "test", "episodes": [{"name": "第1集", "url": "https://example.test/1.m3u8"}]}
        registry._race_search = Mock(return_value=([line], [], [pending]))
        registry._race_probe = Mock(return_value=[])
        subject = {"title": "测试剧", "year": "2026", "category": "tv"}
        try:
            first = registry.resolve_subject(subject)
            second = registry.resolve_subject(subject)
            self.assertEqual(first, second)
            self.assertEqual(registry._race_search.call_count, 1)
        finally:
            pending.set_result((source, [], None, 0))

    def test_real_annual_animation_alias_is_not_displaced_by_short_aliases(self):
        subject = {
            "title": "凡人修仙传 年番4", "year": "2026", "category": "anime",
            "aliases": ["凡人修仙传 第177-228集", "凡人修仙传 年番4之慕兰之战",
                        "凡人修仙传：慕兰之战", "凡人修仙传 第五季",
                        "凡人修仙传 第五季之慕兰之战", "凡人慕兰之战",
                        "A Mortal's Journey 5", "A Mortal's Journey: Fighting in Mulan Grassland"],
        }
        terms = vod_sources._search_terms(subject)
        self.assertEqual(terms[:3], ["凡人修仙传 年番4", "凡人修仙传", "凡人修仙传：慕兰之战"])
        self.assertLessEqual(len(terms), 4)

    def test_chinese_numbered_season_search_falls_back_to_series_title(self):
        self.assertEqual(vod_sources._search_terms({"title": "剑来 第二季"})[:2], ["剑来 第二季", "剑来"])

    def test_annual_animation_merged_source_requires_episode_range_and_slices_lines(self):
        subject = {"title": "仙逆 年番3", "year": "2026", "category": "anime",
                   "aliases": ["仙逆 第129-180集", "仙逆 第四季"]}
        raw = {"vod_id": 7, "vod_name": "仙逆", "vod_year": "2023", "type_name": "国产动漫",
               "vod_play_from": "test", "vod_play_url": "#".join(
                   "第%02d集$https://example.test/%s.m3u8" % (i, i) for i in range(1, 162))}
        source = vod_sources.VodSource("test", "test", "https://example.test", "https://example.test/api", "macms_json")
        registry = vod_sources.SourceRegistry([source])
        self.addCleanup(registry.close)
        from unittest.mock import patch
        with patch.object(vod_sources, "_default_fetch", return_value={"body": json.dumps({"list": [raw]})}):
            _, lines, error, _ = registry._search_one(source, subject)
        self.assertIsNone(error)
        self.assertEqual(len(lines), 1)
        self.assertEqual([e["name"] for e in lines[0]["episodes"]], ["第%02d集" % i for i in range(129, 162)])
        self.assertEqual(lines[0]["episodes"][0]["index"], 0)
        self.assertEqual(vod_sources.MacCmsAdapter(source).resolve(lines[0], 0)["url"], "https://example.test/129.m3u8")
        candidate = {"title": "仙逆", "year": "2023", "category": "anime", "raw": raw}
        self.assertEqual(vod_sources.match_score(subject, candidate), 90)
        self.assertLess(vod_sources.match_score(dict(subject, aliases=[]), candidate), 80)
        self.assertLess(vod_sources.match_score(subject, dict(candidate, category="movie")), 80)
        self.assertLess(vod_sources.match_score(subject, dict(candidate, raw={})), 80)
        self.assertLess(vod_sources.match_score(subject, dict(candidate, title="仙逆剧场版")), 80)

    def test_hhkan_annual_base_title_requires_detail_episode_pair_and_slices_target_range(self):
        from unittest.mock import patch
        import hhkan

        subject = {
            "title": "凡人修仙传 年番4", "year": "2026", "category": "anime",
            "aliases": ["凡人修仙传 第177-228集"],
        }
        search_calls = []
        detail_calls = []

        def search_html():
            return "".join(
                '<div class="module-item"><a href="/detail/%d.html">'
                '<div class="v-item-title" style="display:none">site watermark</div>'
                '<div class="v-item-title">凡人修仙传</div></a></div>' % candidate_id
                for candidate_id in (101, 102))

        def detail_html(vid):
            episode_numbers = [176, 178, 179, 228, 229] if vid == 101 else range(176, 230)
            episodes = "".join(
                '<a href="/play/%d-7-%d.html"><span>第%d集</span></a>'
                % (vid, number, number) for number in episode_numbers)
            return (
                '<div class="detail-title"><strong>片名</strong>'
                '<strong>凡人修仙传</strong></div>'
                '<a class="detail-tags-item">国产动漫</a>'
                '<a class="detail-tags-item">2020</a>'
                '<div class="source-list-box-main"><div>'
                '<span class="source-item-label">线路</span></div></div></div>'
                '<div><div><div class="episode-list-box-main"><div><div class="episode-list">'
                + episodes + '</div><span></span></div></div></div></div>')

        class FakeHhkan:
            @staticmethod
            def get_search_token():
                return "test-token"

            @staticmethod
            def get_page(path, **_kwargs):
                if path.startswith("/search"):
                    search_calls.append(urllib.parse.parse_qs(
                        urllib.parse.urlsplit("https://hhkan.test" + path).query).get("k", [""])[0])
                    return search_html()
                return detail_html(int(path.rsplit("/", 1)[-1].split(".", 1)[0]))

            @staticmethod
            def parse_detail(path, vid=0):
                detail_calls.append(vid)
                return hhkan.parse_detail(path, vid=vid)

            _parse_vod_list = staticmethod(hhkan._parse_vod_list)

        source = vod_sources.VodSource(
            "hhkan", "好好看", "https://hhkan.test", "", "hhkan")
        registry = vod_sources.SourceRegistry([source])
        self.addCleanup(registry.close)
        real_adapter = vod_sources.HhkanAdapter
        with patch.object(vod_sources, "HhkanAdapter",
                          side_effect=lambda current: real_adapter(current, FakeHhkan)):
            _source, lines, error, _elapsed = registry._search_one(source, subject)

        self.assertIsNone(error)
        self.assertEqual(search_calls, vod_sources._search_terms(subject))
        self.assertEqual(detail_calls, [101, 102])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["vod_id"], 102)
        self.assertEqual(lines[0]["match_score"], 90)
        self.assertEqual(len(lines[0]["episodes"]), 52)
        self.assertEqual(lines[0]["episodes"][0]["name"], "第177集")
        self.assertEqual(lines[0]["episodes"][-1]["name"], "第228集")
        self.assertEqual(lines[0]["episodes"][0]["index"], 0)

    def test_hhkan_annual_base_title_without_both_opening_episodes_is_rejected(self):
        from unittest.mock import patch

        subject = {
            "title": "凡人修仙传 年番4", "year": "2026", "category": "anime",
            "aliases": ["凡人修仙传 第177-228集"],
        }
        detail_calls = []

        class FakeHhkan:
            @staticmethod
            def get_search_token(): return "test-token"

            @staticmethod
            def get_page(path, **_kwargs):
                if path.startswith("/search"):
                    return ('<a href="/detail/101.html"><div class="title">'
                            '凡人修仙传</div></a>')
                return path

            @staticmethod
            def parse_detail(_path, vid=0):
                detail_calls.append(vid)
                return {"id": vid, "title": "凡人修仙传", "year": "2020",
                        "meta": "国产动漫 / 2020", "sources": [{"name": "线路", "episodes": [
                            {"ep": "第177集", "pid": 7, "vid": 177},
                            {"ep": "第179集", "pid": 7, "vid": 179},
                        ]}]}

        source = vod_sources.VodSource(
            "hhkan", "好好看", "https://hhkan.test", "", "hhkan")
        registry = vod_sources.SourceRegistry([source])
        self.addCleanup(registry.close)
        real_adapter = vod_sources.HhkanAdapter
        with patch.object(vod_sources, "HhkanAdapter",
                          side_effect=lambda current: real_adapter(current, FakeHhkan)):
            _source, lines, error, _elapsed = registry._search_one(source, subject)

        self.assertIsNone(error)
        self.assertEqual(detail_calls, [101])
        self.assertEqual(lines, [])

    def test_hhkan_other_season_title_is_never_treated_as_the_annual_base_candidate(self):
        from unittest.mock import patch

        subject = {
            "title": "凡人修仙传 年番4", "year": "2026", "category": "anime",
            "aliases": ["凡人修仙传 第177-228集"],
        }
        detail_calls = []

        class FakeHhkan:
            @staticmethod
            def get_search_token(): return "test-token"

            @staticmethod
            def get_page(path, **_kwargs):
                if path.startswith("/search"):
                    return ('<a href="/detail/103.html"><div class="title">'
                            '凡人修仙传 年番3</div></a>')
                return path

            @staticmethod
            def parse_detail(_path, vid=0):
                detail_calls.append(vid)
                return {"id": vid, "title": "凡人修仙传 年番3", "year": "2025",
                        "meta": "国产动漫 / 2025", "sources": []}

        source = vod_sources.VodSource(
            "hhkan", "好好看", "https://hhkan.test", "", "hhkan")
        registry = vod_sources.SourceRegistry([source])
        self.addCleanup(registry.close)
        real_adapter = vod_sources.HhkanAdapter
        with patch.object(vod_sources, "HhkanAdapter",
                          side_effect=lambda current: real_adapter(current, FakeHhkan)):
            _source, lines, error, _elapsed = registry._search_one(source, subject)

        self.assertIsNone(error)
        self.assertEqual(detail_calls, [])
        self.assertEqual(lines, [])

    def test_hhkan_exact_title_is_rechecked_against_real_detail_year(self):
        from unittest.mock import patch
        import hhkan

        subject = {"title": "测试片", "year": "2024", "category": "movie", "season": 0}
        source = vod_sources.VodSource(
            "hhkan", "好好看", "https://hhkan.test", "", "hhkan")
        real_adapter = vod_sources.HhkanAdapter
        for category, actual_type, actual_year, should_match in (
                ("movie", "电影", "2024", True),
                ("movie", "电影", "2004", False),
                ("tv", "电视剧", "2024", True),
                ("tv", "电视剧", "2004", False)):
            subject["category"] = category
            with self.subTest(category=category, actual_year=actual_year):
                detail_html = (
                    '<div class="detail-title"><strong>片名</strong><strong>测试片</strong></div>'
                    '<a class="detail-tags-item">%s</a>'
                    '<a class="detail-tags-item">%s</a>' % (actual_type, actual_year)
                    + '<div class="source-list-box-main"><div>'
                    '<span class="source-item-label">线路</span></div></div></div>'
                    '<div><div><div class="episode-list-box-main"><div><div class="episode-list">'
                    '<a href="/play/8-2-1.html"><span>第1集</span></a>'
                    '</div><span></span></div></div></div></div>')

                class FakeHhkan:
                    @staticmethod
                    def get_search_token(): return "test-token"

                    @staticmethod
                    def get_page(path, **_kwargs):
                        if path.startswith("/search"):
                            return ('<div class="module-item"><a href="/detail/8.html">'
                                    '<div class="v-item-title">测试片</div></a></div>')
                        return detail_html

                    _parse_vod_list = staticmethod(hhkan._parse_vod_list)
                    parse_detail = staticmethod(hhkan.parse_detail)

                registry = vod_sources.SourceRegistry([source])
                self.addCleanup(registry.close)
                with patch.object(vod_sources, "HhkanAdapter",
                                  side_effect=lambda current: real_adapter(current, FakeHhkan)):
                    _source, lines, error, _elapsed = registry._search_one(source, subject)
                self.assertIsNone(error)
                self.assertEqual(bool(lines), should_match)
                if should_match:
                    self.assertEqual(lines[0]["match_score"], 100)
                else:
                    self.assertEqual(lines, [])

    def test_hhkan_search_uses_bounded_terms_without_inventing_metadata_and_detail_cache_is_request_local(self):
        subject = {
            "title": "庆余年2", "year": "2024", "category": "tv", "season": 2,
            "aliases": ["庆余年2"],
        }
        search_calls = []
        detail_calls = []

        class FakeHhkan:
            @staticmethod
            def get_search_token(): return "test-token"

            @staticmethod
            def get_page(path, **_kwargs):
                if path.startswith("/search"):
                    search_calls.append(path)
                    return ('<a href="/detail/7.html"><div class="title">'
                            '庆余年2</div></a>')
                return path

            @staticmethod
            def parse_detail(_path, vid=0):
                detail_calls.append(vid)
                return {"id": vid, "title": "庆余年2", "year": "2024",
                        "meta": "电视剧 / 2024", "sources": [{"name": "线路", "episodes": [
                            {"ep": "第1集", "pid": 2, "vid": 3},
                        ]}]}

        source = vod_sources.VodSource(
            "hhkan", "好好看", "https://hhkan.test", "", "hhkan")
        adapter = vod_sources.HhkanAdapter(source, module=FakeHhkan)
        candidates = adapter.search(subject)
        self.assertEqual(len(candidates), 1)
        self.assertNotIn("year", candidates[0])
        self.assertNotIn("category", candidates[0])
        self.assertNotIn("season", candidates[0])
        self.assertEqual(len(search_calls), 1)

        first = adapter.detail(candidates[0])
        second = adapter.detail(candidates[0])
        self.assertEqual(first, second)
        self.assertEqual(candidates[0]["year"], "2024")
        self.assertEqual(candidates[0]["category"], "tv")
        self.assertEqual(detail_calls, [7])

        # A newly created adapter represents a fresh source-search request, so
        # its detail lookup must reach HHKan again rather than reuse old data.
        refreshed = vod_sources.HhkanAdapter(source, module=FakeHhkan)
        refreshed.detail({"id": "7", "title": "庆余年2"})
        self.assertEqual(detail_calls, [7, 7])

    def test_default_registry_contains_reachable_fallback_candidates(self):
        registry = vod_sources.SourceRegistry.load_default()
        self.addCleanup(registry.close)
        # 2026-10-12 按 artifacts/可用源清单-2026-10-12.md 重置：只保留好好看，
        # 其余换成实测可用的非凡资源（可搜索 + 可出直链 m3u8）。
        # 牛牛视频虽然 ?ac=list 可达，但对 wd 关键词搜索返回「暂不支持搜索」，
        # 本项目的取源路径都要先按标题搜，所以不注册。
        self.assertEqual([s.name for s in registry.sources], ["好好看", "非凡资源"])
        self.assertEqual([s.adapter_type for s in registry.sources], ["hhkan", "macms_json"])

    def test_unreachable_source_does_not_block_reachable_source(self):
        class FakeAdapter:
            def __init__(self, source): self.source = source
            def search(self, _subject):
                if self.source.id == "offline":
                    raise TimeoutError("source unavailable")
                return [{"title": "兰香如故", "year": "2026", "category": "tv", "season": 0}]
            def detail(self, _candidate):
                return [{"id": "online:1:0", "source_id": self.source.id,
                         "name": self.source.name,
                         "episodes": [{"index": 0, "name": "第1集", "url": "https://v.example/1.m3u8"}]}]

        old = vod_sources.MacCmsAdapter
        vod_sources.MacCmsAdapter = FakeAdapter
        registry = vod_sources.SourceRegistry([
            vod_sources.VodSource("offline", "断线", "https://offline.example", "https://offline.example/api", "macms_json"),
            vod_sources.VodSource("online", "可用", "https://online.example", "https://online.example/api", "macms_json"),
        ], probe=lambda _url: {"alive": True, "height": 720, "latency_ms": 1})
        try:
            lines = registry.resolve_subject({"title": "兰香如故", "year": "2026", "category": "tv"}, wait_for_all=True)
            self.assertIn("online", {line["source_id"] for line in lines})
        finally:
            registry.close()
            vod_sources.MacCmsAdapter = old

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

    def test_red_chamber_2010_search_prefers_new_edition_and_rejects_1987_source(self):
        subject = {
            "title": "红楼梦", "aliases": ["新版红楼梦", "The Dream of Red Mansions"],
            "year": "2010", "category": "tv", "season": 0,
        }
        source_1987 = {"title": "红楼梦", "year": "1987", "category": "tv", "season": 0}
        source_2010 = {"title": "新版红楼梦", "year": "2010", "category": "tv", "season": 0}

        self.assertEqual(vod_sources._search_terms(subject)[:2], ["红楼梦", "新版红楼梦"])
        self.assertGreaterEqual(
            vod_sources.match_score(subject, source_2010), vod_sources.AUTO_MATCH_THRESHOLD)
        self.assertLess(
            vod_sources.match_score(subject, source_1987), vod_sources.AUTO_MATCH_THRESHOLD)

    def test_punctuation_difference_still_matches(self):
        subject = {"title": "蜘蛛侠：英雄无归", "year": "2021", "category": "movie", "season": 0}
        candidate = {"title": "蜘蛛侠英雄无归", "year": "2021", "category": "movie", "season": 0}
        self.assertGreaterEqual(vod_sources.match_score(subject, candidate), vod_sources.AUTO_MATCH_THRESHOLD)

    def test_bilingual_source_title_matches_exact_chinese_title(self):
        subject = {"title": "肖申克的救赎", "year": "1994", "category": "movie"}
        candidate = {"title": "肖申克的救赎 The Shawshank Redemption", "year": "1994", "category": "movie"}
        self.assertGreaterEqual(vod_sources.match_score(subject, candidate), vod_sources.AUTO_MATCH_THRESHOLD)
        candidate["title"] = "肖申克的救赎 2 The Shawshank Redemption"
        self.assertEqual(vod_sources.match_score(subject, candidate), 0)

    def test_matching_metadata_year_suffix_is_not_part_of_the_source_title(self):
        subject = {
            "title": "奥德赛", "original_title": "The Odyssey", "year": "2026",
            "category": "movie", "season": 0,
        }
        candidate = {"title": "奥德赛2026", "year": "2026", "category": "movie", "season": 0}
        wrong_year = dict(candidate, year="2025")

        self.assertEqual(vod_sources.match_score(subject, candidate), 100)
        self.assertEqual(vod_sources.match_score(subject, wrong_year), 0)

    def test_maccms_keeps_a_title_with_its_matching_metadata_year_suffix(self):
        source = vod_sources.VodSource(
            "demo", "演示", "https://example.test",
            "https://example.test/api.php/provide/vod/", "macms_json")
        calls = []

        def fake_fetch(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("wd", [""])[0]
            calls.append(query)
            if query == "奥德赛":
                return {"body": '{"list":[{"vod_id":42,"vod_name":"奥德赛2026",'
                                 '"vod_year":"2026","type_name":"电影",'
                                 '"vod_play_from":"line-a",'
                                 '"vod_play_url":"正片$https://v.example/odyssey.m3u8"}]}'}
            return {"body": '{"list":[]}'}

        subject = {
            "title": "奥德赛", "original_title": "The Odyssey", "year": "2026",
            "category": "movie", "season": 0,
        }
        adapter = vod_sources.MacCmsAdapter(source, fetch=fake_fetch)

        candidates = adapter.search(subject)

        self.assertEqual([item["title"] for item in candidates], ["奥德赛2026"])
        self.assertEqual(vod_sources.match_score(subject, candidates[0]), 100)
        self.assertEqual(calls, ["奥德赛"])

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

    def test_probe_percent_encodes_unicode_path_and_preserves_existing_query_escapes(self):
        requested = []
        result = vod_sources.probe_stream(
            "https://v.example/video/第1集/index.m3u8?token=a%2Bb&name=红楼梦#ignored",
            fetch=lambda url: requested.append(url) or {
                "elapsed_ms": 90, "body": "#EXTM3U\n#EXTINF:6,\nsegment.ts",
            },
        )

        self.assertTrue(result["alive"])
        self.assertTrue(requested[0].isascii())
        self.assertIn("token=a%2Bb", requested[0])
        self.assertIn("name=%E7%BA%A2%E6%A5%BC%E6%A2%A6", requested[0])
        self.assertNotIn("#ignored", requested[0])

    def test_probe_rejects_successful_html_error_page(self):
        result = vod_sources.probe_stream("https://v.example/master.m3u8", fetch=lambda _url: {
            "elapsed_ms": 80,
            "body": "<!doctype html><html><body>Access denied</body></html>",
        })
        self.assertFalse(result["alive"])

    def test_probe_accepts_direct_mp4_and_flv_media(self):
        for url, content_type in (
                ("https://cdn.example/movie.mp4?token=x", "video/mp4"),
                ("https://cdn.example/live.flv", "application/octet-stream")):
            with self.subTest(url=url):
                result = vod_sources.probe_stream(url, fetch=lambda _url: {
                    "status": 206, "content_type": content_type,
                    "elapsed_ms": 90, "body": b"media bytes",
                })
                self.assertTrue(result["alive"])
                self.assertEqual(result["latency_ms"], 90)
                self.assertEqual(result["height"], 0)

    def test_probe_rejects_error_status_even_if_body_looks_like_a_playlist(self):
        result = vod_sources.probe_stream("https://cdn.example/master.m3u8", fetch=lambda _url: {
            "status": 503, "content_type": "application/vnd.apple.mpegurl",
            "elapsed_ms": 40, "body": "#EXTM3U\n#EXTINF:6,\nsegment.ts",
        })
        self.assertFalse(result["alive"])

    def test_probe_rejects_json_error_payload_as_healthy_source(self):
        from unittest.mock import patch

        class FakeResponse:
            status = 200

            def __init__(self, body):
                self.body = body.encode("utf-8")

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body)
                body, self.body = self.body[:size], self.body[size:]
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        source = vod_sources.VodSource(
            "broken", "坏源", "https://broken.example",
            "https://broken.example/api.php/provide/vod/", "macms_json")
        with patch("vod_sources.urllib.request.urlopen",
                   return_value=FakeResponse('{"code":0,"msg":"upstream unavailable"}')):
            result = vod_sources.probe_source(source)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unexpected payload")

    def test_probe_accepts_structured_empty_source_catalog(self):
        from unittest.mock import patch

        class FakeResponse:
            status = 200

            def __init__(self, body):
                self.body = body.encode("utf-8")

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body)
                body, self.body = self.body[:size], self.body[size:]
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        source = vod_sources.VodSource(
            "empty", "空目录", "https://empty.example",
            "https://empty.example/api.php/provide/vod/", "macms_json")
        with patch("vod_sources.urllib.request.urlopen",
                   return_value=FakeResponse('{"code":1,"total":0,"list":[]}')):
            result = vod_sources.probe_source(source)
        self.assertTrue(result["ok"])

    def test_probe_parses_large_valid_catalog_without_truncating_json(self):
        from unittest.mock import patch

        class FakeResponse:
            status = 200

            def __init__(self, body):
                self.body = body.encode("utf-8")

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body)
                body, self.body = self.body[:size], self.body[size:]
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        source = vod_sources.VodSource(
            "large", "大目录", "https://large.example",
            "https://large.example/api.php/provide/vod/", "macms_json")
        payload = '{"code":1,"total":1,"list":[],"padding":"%s"}' % ("x" * 6000)
        self.assertGreater(len(payload), 4096)
        with patch("vod_sources.urllib.request.urlopen",
                   return_value=FakeResponse(payload)):
            result = vod_sources.probe_source(source)
        self.assertTrue(result["ok"])

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
