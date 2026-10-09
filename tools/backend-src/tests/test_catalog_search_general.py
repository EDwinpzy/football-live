import pathlib
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from douban_catalog import DoubanCatalog, JsonDiskCache, normalize_subject
from media_index import MediaIndex


def row(subject_id, title, target_type="movie", subtitle=""):
    return {"layout": "subject", "target_type": target_type, "target": {
        "id": subject_id, "title": title, "year": "2024", "card_subtitle": subtitle,
        "rating": {"value": 8.0, "count": 100}, "cover_url": "https://img.example/poster.jpg"}}


class GeneralCatalogSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = MediaIndex(pathlib.Path(self.tmp.name) / "media.db")
        self.requests = []

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def catalog(self, rows):
        def fetch(url):
            self.requests.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
            return {"subjects": {"items": rows}}
        return DoubanCatalog(fetch=fetch, snapshot={}, media_index=self.index,
                             cache=JsonDiskCache(pathlib.Path(self.tmp.name) / "cache"))

    def test_local_partial_hits_do_not_hide_unindexed_exact_titles_in_any_category(self):
        cases = [
            ("流浪地球", "movie", "中国大陆 / 科幻 / 郭帆 / 吴京", "movie"),
            ("庆余年", "tv", "中国大陆 / 剧情 古装 / 孙皓 / 张若昀", "tv"),
            ("奔跑吧", "tv", "中国大陆 / 真人秀 / 姚译添 / 李晨", "variety"),
            ("鬼灭之刃", "tv", "日本 / 动画 / 外崎春雄 / 花江夏树", "anime"),
        ]
        for position, (query, kind, subtitle, category) in enumerate(cases):
            with self.subTest(query=query):
                local_id, remote_id = str(100 + position), str(200 + position)
                self.index.upsert_many([normalize_subject({"id": local_id, "title": query + "幕后"}, kind)])
                catalog = self.catalog([row(remote_id, query, kind, subtitle)])
                result = catalog.search(query)
                self.assertEqual(result["items"][0]["douban_id"], remote_id)
                self.assertEqual(result["items"][0]["category"], category)
                self.assertIn(local_id, {item["douban_id"] for item in result["items"]})

    def test_remote_alias_and_translation_matches_survive_local_rescoring(self):
        catalog = self.catalog([row("200", "复仇者联盟")])
        for query in ("复联", "Avengers"):
            with self.subTest(query=query):
                self.assertEqual(catalog.search(query)["items"][0]["douban_id"], "200")

    def test_category_query_prefers_matching_media_category_to_incidental_title_match(self):
        self.index.upsert_many([
            normalize_subject({"id": "101", "title": "奔跑吧", "genres": ["真人秀"]}, "variety"),
            normalize_subject({"id": "102", "title": "声生不息", "genres": ["音乐", "真人秀"]}, "variety"),
        ])
        catalog = self.catalog([row("200", "云画的月光10周年综艺特辑")])

        result = catalog.search("综艺")

        varieties = [item for item in result["items"] if item["category"] == "variety"]
        self.assertEqual({item["douban_id"] for item in varieties}, {"101", "102"})
        self.assertEqual(result["items"][0]["category"], "variety")

    def test_cached_remote_results_continue_to_supplement_local_hits(self):
        self.index.upsert_many([normalize_subject({"id": "100", "title": "星际探索 第一季"}, "tv")])
        catalog = self.catalog([row("200", "星际探索 第二季", "tv")])
        first = catalog.search("星际探索")
        second = catalog.search("星际探索")
        self.assertEqual({item["douban_id"] for item in first["items"]}, {"100", "200"})
        self.assertEqual({item["douban_id"] for item in second["items"]}, {"100", "200"})
        # A complete result includes both the search-discovery endpoint and
        # the independent suggestion endpoint; the second identical query uses
        # both cached responses instead of reissuing either network request.
        self.assertEqual(len(self.requests), 2)

    def test_discovery_does_not_replace_rich_cached_details_with_thin_search_cards(self):
        self.index.upsert_many([normalize_subject({"id": "200", "title": "复仇者联盟",
                                                   "summary": "已抓到的完整详情", "actors": ["演员甲"]})])
        catalog = self.catalog([row("200", "复仇者联盟")])
        result = catalog.search("复联")
        self.assertEqual(result["items"][0]["summary"], "已抓到的完整详情")
        stored = self.index.search("复仇者联盟")[0]
        self.assertEqual(stored["summary"], "已抓到的完整详情")
        self.assertEqual(stored["actors"], ["演员甲"])

    def test_discovery_fills_missing_metadata_and_repairs_known_classification(self):
        self.index.upsert_many([normalize_subject({"id": "200", "title": "鬼灭之刃", "summary": "保留简介"})])
        catalog = self.catalog([row("200", "鬼灭之刃", "tv", "日本 / 动画 / 导演甲 / 演员甲")])
        result = catalog.search("鬼灭")
        self.assertEqual(result["items"][0]["category"], "anime")
        self.assertEqual(result["items"][0]["summary"], "保留简介")
        self.assertEqual(self.index.search("鬼灭")[0]["category"], "anime")

    def test_partial_structured_metadata_does_not_hide_subtitle_genres(self):
        target = row("200", "动画测试", "tv", "日本 / 动画 / 导演甲 / 演员甲")
        target["target"].update(regions=["日本"], genres=[])
        result = self.catalog([target]).search("动画")
        self.assertEqual(result["items"][0]["category"], "anime")

    def test_result_pages_do_not_repeat_the_first_page(self):
        catalog = self.catalog([row(str(i), "星际探索 第%d季" % i, "tv") for i in range(1, 7)])
        first = catalog.search("星际探索", page=1, limit=2)
        second = catalog.search("星际探索", page=2, limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 2)
        self.assertTrue(first["has_more"])
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})

    def test_upstream_alias_relevance_precedes_incidental_local_prefix(self):
        self.index.upsert_many([normalize_subject({"id": "100", "title": "复联幕后"})])
        catalog = self.catalog([row("200", "复仇者联盟")])
        self.assertEqual(catalog.search("复联")["items"][0]["douban_id"], "200")

    def test_primary_failure_uses_short_lived_public_fallback_with_canonical_categories(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if "/search/subjects" in url:
                raise OSError("HTTP 403 need_login")
            if "subject_suggest" in url:
                return [{"id": "200", "title": "庆余年", "type": "movie"},
                        {"id": "201", "title": "奔跑吧", "type": "movie"}]
            return {"id": url.rsplit("/", 1)[-1], "title": "庆余年" if url.endswith("200") else "奔跑吧",
                    "type": "tv", "genres": ["古装"] if url.endswith("200") else ["真人秀"]}
        catalog = DoubanCatalog(fetch=fetch, snapshot={}, media_index=self.index,
                                cache=JsonDiskCache(pathlib.Path(self.tmp.name) / "cache"))
        result = catalog.search("庆余")
        self.assertEqual({item["category"] for item in result["items"]}, {"tv", "variety"})
        self.assertEqual(result["source"], "douban-suggest")
        self.assertIsNone(catalog.cache.get_stale("search-discovery:庆余"))
        catalog.search("奔跑")
        self.assertEqual(sum("/search/subjects" in url for url in calls), 1)
        self.assertEqual(sum("subject_suggest" in url for url in calls), 2)

    def test_recovered_primary_is_not_hidden_by_fallback_cache(self):
        recovered = False
        def fetch(url):
            if "/search/subjects" in url:
                if not recovered:
                    raise OSError("blocked")
                return {"subjects": {"items": [row("201", "星际探索 第二季", "tv")]}}
            if "subject_suggest" in url:
                return [{"id": "200", "title": "星际探索 第一季"}]
            return {"id": "200", "title": "星际探索 第一季", "type": "tv"}
        catalog = DoubanCatalog(fetch=fetch, snapshot={}, media_index=self.index,
                                cache=JsonDiskCache(pathlib.Path(self.tmp.name) / "cache"))
        self.assertEqual(catalog.search("星际")["source"], "douban-suggest")
        recovered = True
        catalog._search_unavailable_until = 0
        result = catalog.search("星际")
        self.assertEqual(result["source"], "douban-search")
        self.assertIn("201", {item["douban_id"] for item in result["items"]})

    def test_empty_primary_does_not_block_public_fallback(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if "/search/subjects" in url:
                return {"subjects": {"items": []}}
            if "subject_suggest" in url:
                return [{"id": "200", "title": "三体"}]
            return {"id": "200", "title": "三体", "type": "tv", "genres": ["科幻"]}
        catalog = DoubanCatalog(fetch=fetch, snapshot={}, media_index=self.index,
                                cache=JsonDiskCache(pathlib.Path(self.tmp.name) / "cache"))
        first = catalog.search("三体")
        second = catalog.search("三体")
        self.assertEqual(first["items"][0]["douban_id"], "200")
        self.assertEqual(second["items"][0]["douban_id"], "200")
        self.assertEqual(sum("/search/subjects" in url for url in calls), 1)
        self.assertEqual(sum("subject_suggest" in url for url in calls), 1)

    def test_fallback_never_trusts_suggestion_movie_type_when_details_fail(self):
        def fetch(url):
            if "subject_suggest" in url:
                return [{"id": "200", "title": "未知剧集", "type": "movie"}]
            raise OSError("offline")
        catalog = DoubanCatalog(fetch=fetch, snapshot={}, media_index=self.index)
        result = catalog.search("未知")
        self.assertEqual(result["items"], [])
        self.assertFalse(result["upstream_available"])


if __name__ == "__main__":
    unittest.main()
