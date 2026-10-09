"""影视目录数据边界回归：好好看生成卡片，外部源仅补充详情播放线路。"""

import ast
import json
import pathlib
import sys
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


BACKEND = pathlib.Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
import hhkan  # noqa: E402


class _BytesResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body


class HhkanCatalogContractTest(unittest.TestCase):
    def test_mirror_failover_rebuilds_the_request_url(self):
        calls = []

        def fake_open(request, _timeout):
            calls.append(request.full_url)
            if request.full_url.startswith("https://mirror-a.example"):
                raise OSError("mirror a unavailable")
            return _BytesResponse(b"<html>mirror-b</html>")

        old_base = hhkan.BASE
        old_domains = list(hhkan.DOMAINS)
        old_domain_i = hhkan._domain_i[0]
        old_cookie = hhkan._cookie
        try:
            hhkan.BASE = "https://mirror-a.example"
            hhkan.DOMAINS[:] = ["mirror-a.example", "mirror-b.example"]
            hhkan._domain_i[0] = 0
            hhkan._cookie = None
            with mock.patch.object(hhkan, "_safe_urlopen", side_effect=fake_open):
                self.assertEqual(hhkan.get_page("/channel/1.html"), "<html>mirror-b</html>")
        finally:
            hhkan.BASE = old_base
            hhkan.DOMAINS[:] = old_domains
            hhkan._domain_i[0] = old_domain_i
            hhkan._cookie = old_cookie

        self.assertEqual(calls[:2], [
            "https://mirror-a.example/channel/1.html",
            "https://mirror-b.example/channel/1.html",
        ])

    def test_public_home_endpoint_falls_back_to_hhkan_snapshot(self):
        import proxy
        from hhkan_snapshot import SnapshotCatalog

        snapshot = SnapshotCatalog({
            "schema": 1,
            "source": "hhkan",
            "home": {
                "sections": [{"title": "近期热门电影", "items": [
                    {"id": 101, "title": "好好看影片", "cover": "https://vres.example/101.jpg"}
                ]}],
                "carousel": [{"id": 101, "title": "好好看影片", "backdrop": "https://vres.example/101-wide.jpg"}],
            },
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(proxy, "_HHKAN_SNAPSHOT", snapshot), \
                    mock.patch.object(hhkan, "get_page", side_effect=RuntimeError("origin unavailable")):
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/hhkan/home" % server.server_port,
                        timeout=5) as response:
                    payload = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload["source"], "hhkan-snapshot")
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["sections"][0]["items"][0]["title"], "好好看影片")

    def test_bundled_snapshot_covers_catalog_search_and_detail_during_outage(self):
        import proxy
        from hhkan_snapshot import SnapshotCatalog

        snapshot = SnapshotCatalog.load_default()
        with proxy._HHKAN_ORIGIN_COND:
            proxy._HHKAN_ORIGIN_STATE.update({"status": "unknown", "until": 0.0})
        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.data.get("source"), "hhkan")
        self.assertGreaterEqual(len(snapshot.data.get("items") or {}), 500)
        self.assertGreaterEqual(len(snapshot.data.get("details") or {}), 200)
        for cid in (1, 2, 3, 4, 6):
            sections = snapshot.channel_sections(cid)["sections"]
            self.assertEqual([section["name"] for section in sections],
                             ["最近热门", "最新上线", "最近更新"])
            self.assertTrue(all(section["items"] for section in sections))

        first = snapshot.home()["carousel"][0]
        title_query = first["title"][:2]
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def fetch(path):
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (server.server_port, path),
                    timeout=12) as response:
                return json.load(response)

        origin = mock.Mock(side_effect=RuntimeError("origin unavailable"))
        try:
            with mock.patch.object(proxy, "_HHKAN_SNAPSHOT", snapshot), \
                    mock.patch.object(hhkan, "get_page", origin):
                home = fetch("/hhkan/home?outage=1")
                channel = fetch("/hhkan/channel/1?outage=1")
                sections = fetch("/hhkan/channel-sections/1?outage=1")
                latest = fetch("/hhkan/latest?page=1&outage=1")
                show = fetch("/hhkan/show/1?by=3&page=1&outage=1")
                filters = fetch("/hhkan/filters/1?outage=1")
                detail = fetch("/hhkan/detail/%s?outage=1" % first["id"])
                search = fetch("/hhkan/search?k=%s&page=1&outage=1" %
                               urllib.parse.quote(title_query))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            with proxy._HHKAN_ORIGIN_COND:
                proxy._HHKAN_ORIGIN_STATE.update({"status": "unknown", "until": 0.0})

        self.assertTrue(home["sections"] and home["carousel"])
        self.assertTrue(channel["items"])
        self.assertTrue(all(section["items"] for section in sections["sections"]))
        self.assertTrue(latest["items"])
        self.assertTrue(show["items"])
        self.assertTrue(filters["types"])
        self.assertEqual(detail["title"], first["title"])
        self.assertTrue(search["items"])
        self.assertTrue(all(payload["source"] == "hhkan-snapshot" for payload in
                            (home, channel, sections, latest, show, filters, detail)))
        self.assertEqual(origin.call_count, 1, "源站熔断后不应让每个接口重复等待失败")

    def test_card_parser_skips_hidden_watermark_and_placeholder_image(self):
        page = """
        <div class='section-header-title'>热门推荐</div>
        <div class='module-item'>
          <a href='/detail/294735.html' class='v-item'>
            <div class='v-item-cover'>
              <img data-original='https://vf.example/logo_placeholder_vertical.png'
                   title='可可影视 kekys.com' id='noneCoverImg'>
              <img data-original='/vod1/vod/cover/real.jpg'>
            </div>
            <div class='v-item-top-left'><span>豆瓣:8.5分</span></div>
            <div class='v-item-bottom'><span>正片</span></div>
            <div class='v-item-footer'>
              <div class='v-item-title' style='display: none'>可可影视-kekys.com</div>
              <div class='v-item-title'>挽救计划</div>
            </div>
          </a>
        </div>
        """
        item = hhkan.parse_channel_sections(page)[0]["items"][0]
        self.assertEqual(item["title"], "挽救计划")
        self.assertTrue(item["cover"].endswith("/vod1/vod/cover/real.jpg"))
        self.assertEqual(item["score"], 8.5)

    def test_channel_sections_keep_product_order_and_source_items(self):
        page = """
        <div class='section-header-title'>最新上线</div>
        <div class='module-item'><a href='/detail/101.html'></a><div class='v-item-title'>上线片</div></div>
        <div class='section-header-title'>热门推荐</div>
        <div class='module-item'><a href='/detail/102.html'></a><div class='v-item-title'>热门片</div></div>
        <div class='section-header-title'>最近更新</div>
        <div class='module-item'><a href='/detail/103.html'></a><div class='v-item-title'>更新片</div></div>
        """
        sections = hhkan.complete_channel_sections(hhkan.parse_channel_sections(page))
        self.assertEqual([section["name"] for section in sections], ["最近热门", "最新上线", "最近更新"])
        self.assertEqual([[item["id"] for item in section["items"]] for section in sections], [[102], [101], [103]])

    def test_catalog_routes_cannot_reintroduce_mcms_cards(self):
        source = (BACKEND / "proxy.py").read_text(encoding="utf-8")
        routes = source[source.index('if p == "/hhkan/home":'):source.index('if p.startswith("/hhkan/play/"):')]
        forbidden = (
            "return respond(mcms_home())",
            "_mcms_paged(cid",
            "return respond(mcms_detail(vid))",
            "ex.submit(_mcms_list_site",
            "_mcms_channel_sections(cid)",
        )
        for expression in forbidden:
            self.assertNotIn(expression, routes, expression)

    def test_selfhosted_web_starts_snapshot_first_but_keeps_recovery_probe(self):
        compose = (ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8")
        backend = (BACKEND / "proxy.py").read_text(encoding="utf-8")
        self.assertIn('OTV_HHKAN_SNAPSHOT_FIRST: "1"', compose)
        self.assertIn('os.environ.get("OTV_HHKAN_SNAPSHOT_FIRST") == "1"', backend)
        self.assertIn('_HHKAN_ORIGIN_CIRCUIT_TTL = 300.0', backend)

    def test_supplement_pool_matches_approved_t1_sites(self):
        source = (BACKEND / "proxy.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments = {
            target.id: ast.literal_eval(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
            and target.id in {"MCMS_SITES", "MCMS_LEAVES_BY_SITE"}
        }
        expected = ["bfzy", "jszy", "jinying", "huya", "hhzy", "hongniu", "subo", "360zy"]
        self.assertEqual([site["name"] for site in assignments["MCMS_SITES"]], expected)
        self.assertEqual(set(assignments["MCMS_LEAVES_BY_SITE"]), set(expected))
        self.assertNotIn("fdzys", expected)

    def test_each_supplement_site_has_safe_four_channel_mapping(self):
        source = (BACKEND / "proxy.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        mapping = next(
            ast.literal_eval(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
            and target.id == "MCMS_LEAVES_BY_SITE"
        )
        for site, channels in mapping.items():
            self.assertEqual(set(channels), {1, 2, 3, 4}, site)
            self.assertTrue(
                all(ids and all(isinstance(cid, int) and cid > 0 for cid in ids)
                    for ids in channels.values()),
                site,
            )

    def test_all_detail_pages_hide_source_selection(self):
        detail_screens = [
            ROOT / "android/app/src/main/java/com/qiubo/optimaltv/ui/detail/DetailScreen.kt",
            ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/detail/DetailScreen.kt",
        ]
        for path in detail_screens:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("ui.lines.size", source, path)
            self.assertNotIn("ui.activeLineId", source, path)
            self.assertNotIn("private fun LineChip", source, path)
            self.assertIn("shouldShowEpisodePicker(item)", source, path)
            self.assertIn('"1" -> false', source, path)

        web = (BACKEND / "www/app.js").read_text(encoding="utf-8")
        detail_renderer = web[web.index("function renderDetail()"):
                              web.index("function renderDetailEps()")]
        self.assertIn('wrap.hidden = true;', detail_renderer)
        self.assertNotIn('wrap.hidden = false;', detail_renderer)
        self.assertIn("isDetailEpisodeSet(eps)", web)

    def test_web_hero_resolves_prefixed_douban_id_to_real_metadata(self):
        web = (BACKEND / "www/app.js").read_text(encoding="utf-8")
        brief = web[web.index("async function vodBrief(id)"):
                    web.index("async function initVod()")]
        identity = web[web.index("function vodIdentity(vid)"):
                       web.index("async function vodBrief(id)")]
        self.assertIn('value.replace(/^douban:/, "")', identity)
        self.assertIn("BASE + vodDetailPath(id)", brief)

    def test_web_hhkan_https_images_bypass_cloud_proxy(self):
        web = (BACKEND / "www/app.js").read_text(encoding="utf-8")
        helper = web[web.index("function img(u)"):web.index("function relay(u, ref)")]
        self.assertIn('if (/^https:\\/\\//i.test(u)) return u;', helper)
        self.assertNotIn("IMG_CDN_RE.test", helper)

    def test_hero_brief_only_fetches_douban_metadata(self):
        """hero 简介只取豆瓣元数据（defer_sources=1），不等全量片源解析。

        全量 /vod/detail 要等后端把 22 个站点的搜索+探测跑完（本机实测 7.5s，电视弱网
        远超客户端 45s 读超时）——简介会长时间停在「加载简介…」，超时后退化成通用占位文案。
        """
        sources = [
            ROOT / "android/app/src/main/java/com/qiubo/optimaltv/data/source/VodApiSource.kt",
            ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/source/VodApiSource.kt",
        ]
        for path in sources:
            src = path.read_text(encoding="utf-8")
            self.assertIn('if (deferSources) "?defer_sources=1"', src, path)

        repos = [
            ROOT / "android/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt",
            ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt",
        ]
        for path in repos:
            repo = path.read_text(encoding="utf-8")
            brief = repo[repo.index("hero 简介补全"):repo.index("companion object {")]
            self.assertIn("deferSources = true", brief, path)
            self.assertIn("readTimeout(20", brief, path)
            # defer_sources 的响应 sources 恒为空：写进共用缓存会让详情页显示「暂无片源」
            self.assertNotIn("fetchDetailCached(", brief, path)
            self.assertIn("briefDetailCache", brief, path)

        web = (BACKEND / "www/app.js").read_text(encoding="utf-8")
        brief_js = web[web.index("async function vodBrief(id)"):web.index("async function initVod()")]
        self.assertIn("defer_sources=1", brief_js)
        self.assertIn("d.summary", brief_js)
        self.assertIn("d.rating", brief_js)

    def test_player_requests_ranked_vod_pool_before_start(self):
        backend = (BACKEND / "proxy.py").read_text(encoding="utf-8")
        route = backend[backend.index('if p.startswith("/hhkan/detail/"):'):
                        backend.index('if p.startswith("/hhkan/play/"):')]
        self.assertIn('q("enrich", "0") == "1"', route)
        self.assertIn("enrich_detail_sources(d, vid)", route)

        repo = (ROOT / "android/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt").read_text(encoding="utf-8")
        source = (ROOT / "android/app/src/main/java/com/qiubo/optimaltv/data/source/HhkanSource.kt").read_text(encoding="utf-8")
        self.assertIn("VodApiSource.fetchDetail", repo)
        self.assertIn("d.lines.mapNotNull", repo)
        self.assertIn("PlaybackLineSelector", repo)

    def test_empty_detail_matches_are_not_cached_as_permanent_no_source(self):
        repos = [
            ROOT / "android/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt",
            ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt",
        ]
        for path in repos:
            source = path.read_text(encoding="utf-8")
            self.assertIn("if (d.lines.isNotEmpty()) detailInfoCache[ref] = d", source, path)
            self.assertIn("if (eps.isNotEmpty()) detailCache[vodId] = it", source, path)


if __name__ == "__main__":
    unittest.main()
