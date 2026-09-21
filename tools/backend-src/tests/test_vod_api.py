import json
import pathlib
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402
import vod_api  # noqa: E402
import vod_sources  # noqa: E402


class _StubCatalog:
    """只回一份最小元数据，避免测试依赖豆瓣抓取。"""

    def detail(self, douban_id, force_refresh=False):
        return {"id": "douban:" + str(douban_id), "douban_id": str(douban_id),
                "title": "更新中的剧", "year": "2026", "category": "tv", "sources": []}

    def refresh_detail(self, douban_id):
        return self.detail(douban_id, force_refresh=True)


class VodServiceRefreshTest(unittest.TestCase):
    """refresh=1 必须真的清掉片源短缓存——默认 resolver 与注入 resolver 两条路径都要覆盖。"""

    def setUp(self):
        self.registry = vod_sources.SourceRegistry([], version=1)
        self.invalidated = []
        real_invalidate = self.registry.invalidate_subject

        def spy(subject):
            self.invalidated.append(subject.get("title"))
            return real_invalidate(subject)

        self.registry.invalidate_subject = spy

    def test_default_resolver_clears_short_cache_only_on_refresh(self):
        service = vod_api.VodService(_StubCatalog(), self.registry)
        service.detail("1292052", refresh=True)
        self.assertEqual(self.invalidated, ["更新中的剧"])
        service.detail("1292052")
        self.assertEqual(self.invalidated, ["更新中的剧"])

    def test_injected_resolver_also_clears_short_cache_on_refresh(self):
        seen = []
        service = vod_api.VodService(
            _StubCatalog(), self.registry,
            resolve_sources=lambda subject, wait_for_all=False: seen.append(subject) or [])
        service.detail("1292052", refresh=True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.invalidated, ["更新中的剧"])
        service.detail("1292052")
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.invalidated, ["更新中的剧"])


class _FakeVodService:
    home_refreshes = []
    detail_refreshes = []

    def home(self, category, refresh=False):
        self.home_refreshes.append(refresh)
        item = {"id": "douban:1292052", "title": "肖申克的救赎"}
        return {"category": category, "sections": [
            {"title": "最近热门", "items": [item]},
            {"title": "最新上映", "items": [item]},
            {"title": "豆瓣高分", "items": [item]},
        ]}

    def filters(self, category):
        return {"category": category, "filters": ["类别", "类型", "地区", "年份", "评分", "排序"]}

    def show(self, category, query):
        return {"category": category, "items": [], "page": int(query.get("page", ["1"])[0])}

    def search(self, query, page=1, limit=30):
        return {"query": query, "page": page, "limit": limit, "items": [], "total": 0, "local": True}

    def detail(self, douban_id, defer_sources=False, refresh=False):
        self.detail_refreshes.append(refresh)
        return {"id": "douban:" + douban_id, "title": "肖申克的救赎",
                "source_state": "matching" if defer_sources else "unavailable", "sources": []}

    def play(self, douban_id, line_id, episode):
        return {"id": "douban:" + douban_id, "line_id": line_id, "episode": episode, "url": "https://video.example/a.m3u8"}

    def source_status(self):
        return {"version": 1, "sources": []}


class VodApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_service = getattr(proxy, "VOD_SERVICE", None)
        proxy.VOD_SERVICE = _FakeVodService()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        proxy.VOD_SERVICE = cls.old_service

    def fetch(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.server.server_port, path), timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_vod_home_uses_douban_cards(self):
        payload = self.fetch("/vod/home?category=movie")
        self.assertEqual([s["title"] for s in payload["sections"]], ["最近热门", "最新上映", "豆瓣高分"])
        self.assertTrue(all(i["id"].startswith("douban:") for s in payload["sections"] for i in s["items"]))

    def test_refresh_query_reaches_home_service(self):
        self.fetch("/vod/home?category=movie&refresh=1")
        self.assertTrue(proxy.VOD_SERVICE.home_refreshes[-1])

    def test_detail_keeps_subject_when_no_source_matches(self):
        payload = self.fetch("/vod/detail/1292052")
        self.assertEqual(payload["id"], "douban:1292052")
        self.assertEqual(payload["source_state"], "unavailable")
        self.assertEqual(payload["sources"], [])

    def test_detail_distinguishes_matching_from_unavailable(self):
        self.assertEqual(self.fetch("/vod/detail/1292052?defer_sources=1")["source_state"], "matching")

    def test_refresh_query_reaches_detail_service(self):
        self.fetch("/vod/detail/1292052?refresh=1")
        self.assertTrue(proxy.VOD_SERVICE.detail_refreshes[-1])

    def test_search_has_one_local_api_for_all_clients(self):
        payload = self.fetch("/api/search?q=%E8%82%96%E7%94%B3%E5%85%8B&limit=7")
        self.assertTrue(payload["local"])
        self.assertEqual(payload["limit"], 7)

    def test_filter_contract_has_no_language_row(self):
        payload = self.fetch("/vod/filters/movie")
        self.assertEqual(payload["filters"], ["类别", "类型", "地区", "年份", "评分", "排序"])

    def test_backend_packaging_requires_new_vod_modules(self):
        root = BACKEND.parents[1]
        admin = (root / "tools" / "admin_server.py").read_text(encoding="utf-8")
        build = (root / "tools" / "build_backend_zip.py").read_text(encoding="utf-8")
        web_build = (root / "tools" / "build_web_function.py").read_text(encoding="utf-8")
        for name in ("douban_catalog.py", "douban_snapshot.json", "vod_api.py", "vod_sources.py", "vod_sources.default.json"):
            self.assertIn(name, admin)
            self.assertIn(name, build)
            self.assertIn(name, web_build)


if __name__ == "__main__":
    unittest.main()
