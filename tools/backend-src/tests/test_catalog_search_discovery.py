import pathlib
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from douban_catalog import DoubanCatalog, JsonDiskCache, normalize_subject
from media_index import MediaIndex


class CatalogSearchDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = MediaIndex(pathlib.Path(self.tmp.name) / "media.db")
        self.calls = []

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def fetch(self, url):
        self.calls.append(url)
        if "subject_suggest" in url:
            return []
        self.assertIn(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0],
                      ("声生", "声生不息", "shengsheng"))
        return {"subjects": {"items": [
            {"layout": "subject", "target_type": "tv", "target": {
                "id": "35634451", "title": "声生不息", "year": "2022",
                "card_subtitle": "中国大陆 中国香港 / 音乐 真人秀 / 洪啸 / 何炅 王祖蓝",
                "cover_url": "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p2885762590.jpg",
                "rating": {"value": 7.3, "count": 33623}}},
            {"layout": "subject", "target_type": "music", "target": {
                "id": "99", "title": "声生不息原声带"}},
        ]}}

    def catalog(self, fetch=None, snapshot=None):
        return DoubanCatalog(fetch=fetch or self.fetch, snapshot=snapshot or {},
                             cache=JsonDiskCache(pathlib.Path(self.tmp.name) / "cache"),
                             media_index=self.index)

    def test_missing_partial_title_discovers_and_indexes_douban_subject(self):
        catalog = self.catalog()
        result = catalog.search("声生")
        self.assertEqual([item["id"] for item in result["items"]], ["douban:35634451"])
        self.assertEqual(result["items"][0]["category"], "variety")
        self.assertTrue(result["items"][0]["poster_url"])
        self.assertEqual(catalog.search("声生不息")["items"][0]["douban_id"], "35634451")
        self.assertEqual(catalog.search("shengsheng")["items"][0]["douban_id"], "35634451")
        self.assertEqual(len(self.calls), 6)

    def test_partial_discovery_merges_suggestions_and_local_metadata(self):
        self.index.upsert_many([normalize_subject({
            "id": "1864810", "title": "红楼梦", "year": "1987", "category": "tv",
            "aliases": ["87版红楼梦"], "genres": ["剧情"], "summary": "央视版红楼梦",
        }, "tv"), normalize_subject({
            "id": "39000003", "title": "红楼梦", "year": "2010", "category": "tv",
            "aliases": ["新红楼梦"], "genres": ["剧情"],
        }, "tv")])

        def fetch(url):
            self.calls.append(url)
            if "subject_suggest" in url:
                return [{"id": "1864810", "title": "红楼梦"},
                        {"id": "39000002", "title": "红楼梦"}]
            if "/subject/39000002" in url:
                return {"id": "39000002", "title": "红楼梦", "year": "1996",
                        "type": "tv", "genres": ["剧情"], "episodes_count": 50}
            return {"subjects": {"items": [{
                "layout": "subject", "target_type": "tv", "target": {
                    "id": "1864810", "title": "红楼梦", "year": "1987",
                    "card_subtitle": "剧情 / 王扶林",
                }}]}}

        result = self.catalog(fetch=fetch).search("红楼梦", limit=10)

        self.assertEqual(result["total"], 3)
        self.assertEqual([item["douban_id"] for item in result["items"]],
                         ["1864810", "39000002", "39000003"])
        self.assertEqual({item["title"] for item in result["items"]}, {"红楼梦"})
        self.assertEqual({item["year"] for item in result["items"]}, {"1987", "1996", "2010"})
        self.assertEqual(result["items"][0]["aliases"], ["87版红楼梦"])
        self.assertEqual(result["items"][0]["summary"], "央视版红楼梦")
        self.assertTrue(any("subject_suggest" in url for url in self.calls))

    def test_remote_failure_preserves_local_typo_matches_and_retries_after_cooldown(self):
        self.index.upsert_many([normalize_subject({"id": "35634451", "title": "声生不息"}, "tv")])
        def offline(url):
            self.calls.append(url)
            raise OSError("offline")
        catalog = self.catalog(fetch=offline)
        self.assertEqual(catalog.search("声声不息")["items"][0]["douban_id"], "35634451")
        self.assertEqual(catalog.search("完全不存在的长片名")["items"], [])
        self.assertEqual(len(self.calls), 2)
        catalog._search_unavailable_until = 0
        catalog.search("完全不存在的长片名")
        self.assertEqual(len(self.calls), 3)

    def test_warm_index_adds_new_snapshot_items_to_existing_database(self):
        self.index.upsert_many([normalize_subject({"id": "1", "title": "旧影片"})])
        item = normalize_subject({"id": "35634451", "title": "声生不息", "genres": ["真人秀"]})
        catalog = self.catalog(snapshot={"index": [item]})
        self.assertEqual(catalog.warm_index(), 1)
        self.assertEqual(catalog.warm_index(), 0)
        self.assertEqual(catalog.search("声生")["items"][0]["douban_id"], "35634451")
        self.assertEqual(len(self.calls), 2)

    def test_empty_search_never_contacts_upstream(self):
        self.assertEqual(self.catalog().search("  ")["items"], [])
        self.assertEqual(self.calls, [])

    def test_no_results_expire_after_one_minute_without_blocking_other_queries(self):
        def fetch(url):
            if "subject_suggest" in url:
                return []
            self.calls.append(url)
            return {"subjects": {"items": []}}
        catalog = self.catalog(fetch=fetch)
        result = catalog.search("声生")
        self.assertEqual(result["items"], [])
        self.assertTrue(result["upstream_available"])
        catalog.search("声生")
        self.assertEqual(len(self.calls), 1)
        catalog.cache.put("search-discovery:声生:empty", {"items": []}, fetched_at=time.time() - 61)
        catalog.search("声生")
        self.assertEqual(len(self.calls), 2)
        catalog.search("别的片名")
        self.assertEqual(len(self.calls), 3)

    def test_discovery_network_timeout_fits_the_web_search_budget(self):
        catalog = DoubanCatalog(snapshot={}, media_index=self.index)
        with patch("douban_catalog.urllib.request.urlopen", side_effect=OSError("offline")) as opener:
            catalog.search("声生")
        self.assertEqual([call.kwargs["timeout"] for call in opener.call_args_list], [4, 2])

    def test_blocked_response_is_not_persisted_as_a_successful_empty_search(self):
        catalog = self.catalog(fetch=lambda url: {"banned": "blocked", "subjects": {"items": []}})
        self.assertEqual(catalog.search("声生")["items"], [])
        self.assertIsNone(catalog.cache.get_stale("search-discovery:声生"))
        self.assertGreater(catalog._search_unavailable_until, time.monotonic())


if __name__ == "__main__":
    unittest.main()
