import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse


BACKEND = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "douban"
sys.path.insert(0, str(BACKEND))

import douban_catalog  # noqa: E402
import media_index  # noqa: E402


def fixture_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def search_payload(rows):
    """豆瓣 Explore 搜索接口的响应形状（与 `recent_hot` 的 items 不同）。"""
    return {"data": rows}


def explore_row(sid, title, rate="8.0"):
    return {"id": str(sid), "title": title, "rate": rate, "cover": "https://img.example/%s.jpg" % sid}


def home_fetch(rows_by_sort, details):
    """假豆瓣：Explore 列表按 `sort` 分页返回，`/subject/<id>` 返回详情。"""
    def fetch(url):
        if "/subject/" in url:
            sid = url.rstrip("/").rsplit("/", 1)[-1]
            return details.get(sid, {})
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        rows = rows_by_sort.get((query.get("sort") or ["U"])[0]) or []
        start = int((query.get("start") or ["0"])[0])
        return search_payload(rows[start:start + douban_catalog.EXPLORE_PAGE_SIZE])
    return fetch


class DoubanCatalogTest(unittest.TestCase):
    def setUp(self):
        # 限流退避会让用例白等几秒；用例只关心调用次数与回落结果。
        original = douban_catalog.time.sleep
        douban_catalog.time.sleep = lambda _seconds: None
        self.addCleanup(setattr, douban_catalog.time, "sleep", original)

    def test_movie_explore_maps_to_stable_subject(self):
        item = douban_catalog.parse_explore(fixture_json("explore_movie.json"))[0]
        self.assertEqual(item["id"], "douban:1292052")
        self.assertEqual(item["title"], "肖申克的救赎")
        self.assertEqual(item["category"], "movie")

    def test_short_category_rejects_plain_animated_short(self):
        self.assertFalse(douban_catalog.is_short_drama({"genres": ["动画", "短片"], "tags": []}))
        self.assertTrue(douban_catalog.is_short_drama({"genres": ["剧情"], "tags": ["网络短剧"]}))

    def test_detail_uses_douban_metadata(self):
        item = douban_catalog.parse_subject_html(fixture_text("subject_1292052.html"), "1292052")
        self.assertEqual(item["year"], "1994")
        self.assertEqual(item["rating"], 9.7)
        self.assertIn("弗兰克·德拉邦特", item["directors"])
        self.assertIn("月黑高飞", item["aliases"])

    def test_failed_refresh_keeps_last_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = douban_catalog.JsonDiskCache(tmp)
            cache.put("home:movie", {"sections": [1]}, fetched_at=100)
            self.assertEqual(cache.get_stale("home:movie", now=999999)["sections"], [1])

    def test_first_run_uses_bundled_snapshot(self):
        """首装 + 上游离线：随包快照必须顶上来（有内容才算「用了快照」，空快照是
        `douban-unavailable`，客户端据此显示「暂时获取不到」而不是「没有内容」）。"""
        snapshot = {"home": {"movie": {"sections": [
            {"title": "最近热门", "items": [{"douban_id": "1", "title": "快照片"}]}]}}}
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
            snapshot=snapshot,
        )
        result = catalog.home("movie")
        self.assertTrue(result["stale"])
        self.assertEqual(result["source"], "douban-snapshot")
        self.assertFalse(result["upstream_available"])
        titles = [item["title"] for section in result["sections"] for item in section["items"]]
        self.assertEqual(titles, ["快照片"])

    def test_first_run_scrubs_legacy_snapshot_posters_before_serving(self):
        """旧 APK 随包快照可能带默认图；首装读取要清理返回副本而不改源数据。"""
        placeholder = "https://img2.doubanio.com/f/frodo/753fa1e93c945e533e52dddab73b2b55a1083083/pics/subject/movie_large.jpg"
        actual = "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p2885762590.jpg"
        snapshot = {"home": {"movie": {"category": "movie", "sections": [
            {"title": title, "items": [
                {"douban_id": "1", "title": "旧快照占位图", "poster_url": placeholder,
                 "hero_url": placeholder, "backdrop_url": placeholder},
                {"douban_id": "2", "title": "旧快照真海报", "poster_url": actual, "hero_url": actual},
            ]} for title in douban_catalog.HOME_SECTION_NAMES]}}}
        with tempfile.TemporaryDirectory() as tmp:
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda *_args: (_ for _ in ()).throw(OSError("offline")),
                cache=douban_catalog.JsonDiskCache(tmp), snapshot=snapshot,
            )
            catalog._refresh_async = lambda *_args: None

            result = catalog.home("movie")

        first_items = result["sections"][0]["items"]
        self.assertEqual(first_items[0]["poster_url"], "")
        self.assertEqual(first_items[0]["hero_url"], "")
        self.assertEqual(first_items[0]["backdrop_url"], "")
        self.assertEqual(first_items[1]["poster_url"], actual)
        self.assertEqual(snapshot["home"]["movie"]["sections"][0]["items"][0]["poster_url"], placeholder)

    def test_empty_snapshot_with_dead_upstream_reports_unavailable(self):
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
            snapshot={"home": {"movie": {"sections": [{"title": "最近热门", "items": []}]}}},
        )
        result = catalog.home("movie")
        self.assertEqual(result["source"], "douban-unavailable")
        self.assertFalse(result["upstream_available"])

    def test_filters_match_product_rows_and_remove_language(self):
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {})
        result = catalog.filters("movie")
        self.assertEqual([row["name"] for row in result["rows"]],
                         ["类别", "类型", "地区", "年份", "评分", "排序"])
        self.assertEqual(result["rows"][4]["options"], ["全部", "9分以上", "8分以上", "7分以上", "暂无评分"])

    def test_show_passes_sort_and_filters_to_douban_search(self):
        """`recent_hot` 会忽略 category/type；只有 Explore 搜索接口真正支持排序与筛选。"""
        calls = []

        def fetch(url):
            calls.append(url)
            return search_payload([{"id": "1292052", "title": "肖申克的救赎", "rate": "9.7",
                                    "cover": "https://img.example/1292052.jpg",
                                    "casts": ["蒂姆·罗宾斯"], "directors": ["弗兰克·德拉邦特"]}])

        result = douban_catalog.DoubanCatalog(fetch=fetch).show(
            "movie", genre="剧情", region="美国", year="1994", rating="9分以上", sort="rating", page=2)
        self.assertEqual([item["title"] for item in result["items"]], ["肖申克的救赎"])
        self.assertEqual(result["items"][0]["id"], "douban:1292052")
        self.assertEqual(result["items"][0]["rating"], 9.7)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        self.assertEqual(query["sort"], ["S"])
        self.assertEqual(query["range"], ["9,10"])
        self.assertEqual(query["tags"], ["电影"])
        self.assertEqual(query["countries"], ["美国"])
        self.assertEqual(query["year_range"], ["1994,1994"])
        self.assertEqual(query["genres"], ["剧情"])
        self.assertEqual(query["start"], ["20"])

    def test_detail_returns_douban_subject(self):
        def fetch(url):
            if "/subject/1292052" in url:
                raw = fixture_json("explore_movie.json")["items"][0]
                raw.update({"intro": "简介", "countries": ["美国"], "aka": ["月黑高飞"]})
                return raw
            return fixture_json("explore_movie.json")

        catalog = douban_catalog.DoubanCatalog(fetch=fetch)
        self.assertEqual(catalog.detail("1292052")["title"], "肖申克的救赎")

    def test_search_keeps_local_results_when_network_is_unavailable(self):
        class Index:
            def search(self, query, limit=30):
                return [{"id": "douban:1292052", "douban_id": "1292052", "title": query}]

        def offline(_url):
            raise OSError("offline")

        catalog = douban_catalog.DoubanCatalog(fetch=offline, media_index=Index())
        result = catalog.search("肖申克", limit=7)
        self.assertTrue(result["local"])
        self.assertEqual(result["items"][0]["id"], "douban:1292052")

    def test_search_cleans_legacy_placeholder_from_media_index(self):
        """历史 SQLite 索引里的默认图不能在后续离线搜索中继续显示。"""
        placeholder = "https://img2.doubanio.com/f/frodo/753fa1e93c945e533e52dddab73b2b55a1083083/pics/subject/movie_large.jpg"
        with tempfile.TemporaryDirectory() as tmp:
            index = media_index.MediaIndex(pathlib.Path(tmp) / "media.db")
            try:
                index.upsert_many([{
                    "id": "douban:123456", "douban_id": "123456", "title": "旧缓存电影",
                    "category": "movie", "poster_url": placeholder, "hero_url": placeholder,
                }])
                catalog = douban_catalog.DoubanCatalog(
                    fetch=lambda _url: (_ for _ in ()).throw(OSError("offline")), media_index=index)

                result = catalog.search("旧缓存电影")

                self.assertEqual(len(result["items"]), 1)
                self.assertEqual(result["items"][0]["poster_url"], "")
                self.assertEqual(result["items"][0]["hero_url"], "")
                self.assertEqual(index.get("123456")["poster_url"], "")
            finally:
                index.close()

    def test_successful_scrapes_are_ingested(self):
        class Index:
            def __init__(self):
                self.ids = []

            def upsert_many(self, items):
                self.ids.extend(item["douban_id"] for item in items)

        index = Index()
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda _url: search_payload([{"id": "1292052", "title": "肖申克的救赎", "rate": "9.7"}]),
            media_index=index)
        catalog.home("movie")
        catalog.show("movie")
        self.assertIn("1292052", index.ids)

    def test_throttled_home_keeps_previous_cache_instead_of_empty_sections(self):
        """豆瓣限流会回 200 + 空 data：空栏必须当失败，不能当成功结果缓存。

        旧实现把「最近热门有内容、其余两栏空」的结果写进 6 小时缓存，
        用户看到的分组就一直错到缓存过期。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache = douban_catalog.JsonDiskCache(tmp)
            cache.put("home:variety", {
                "category": "variety", "stale": False, "source": "douban",
                "sections": [
                    {"title": "最近热门", "items": [{"douban_id": "1", "title": "上一版热门"}]},
                    {"title": "最新上映", "items": [{"douban_id": "2", "title": "上一版最新"}]},
                    {"title": "豆瓣高分", "items": [{"douban_id": "3", "title": "上一版高分"}]},
                ]}, fetched_at=time.time() - 86400)
            catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {"data": []}, cache=cache)
            result = catalog.home("variety")
            self.assertTrue(result["stale"])
            self.assertEqual([len(section["items"]) for section in result["sections"]], [1, 1, 1])
            cached = cache.get_stale("home:variety")
            self.assertEqual([len(section["items"]) for section in cached["sections"]], [1, 1, 1])

    def test_throttled_home_without_cache_falls_back_to_snapshot(self):
        snapshot = {"home": {"variety": {"category": "variety", "sections": [
            {"title": "最近热门", "items": [{"douban_id": "1", "title": "快照热门"}]},
            {"title": "最新上映", "items": [{"douban_id": "2", "title": "快照最新"}]},
            {"title": "豆瓣高分", "items": [{"douban_id": "3", "title": "快照高分"}]}]}}}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {"data": []}, snapshot=snapshot)
        result = catalog.home("variety")
        self.assertEqual(result["source"], "douban-snapshot")
        self.assertEqual([section["items"][0]["title"] for section in result["sections"]],
                         ["快照热门", "快照最新", "快照高分"])

    def test_partial_throttle_only_backfills_the_missing_section(self):
        snapshot = {"home": {"movie": {"sections": [
            {"title": "最近热门", "items": [{"douban_id": "1", "title": "快照热门"}]},
            {"title": "最新上映", "items": [{"douban_id": "2", "title": "快照最新"}]},
            {"title": "豆瓣高分", "items": [{"douban_id": "3", "title": "快照高分"}]}]}}}

        def fetch(url):
            if "sort=U" in url:
                return search_payload([{"id": "9", "title": "新鲜热门", "rate": "9.0"}])
            return {"data": []}

        catalog = douban_catalog.DoubanCatalog(fetch=fetch, snapshot=snapshot)
        result = catalog.home("movie")
        self.assertEqual(result["source"], "douban-partial")
        self.assertTrue(result["stale"])
        self.assertEqual([section["items"][0]["title"] for section in result["sections"]],
                         ["新鲜热门", "快照最新", "快照高分"])

    def test_throttled_home_sections_are_retried_before_giving_up(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return {"data": []}

        douban_catalog.DoubanCatalog(fetch=fetch, snapshot={}).home("movie")
        self.assertEqual(len(calls), 3 * (1 + len(douban_catalog.EXPLORE_RETRY_DELAYS)))

    def test_filtered_show_empty_result_is_not_treated_as_throttling(self):
        """「全部」页的筛选空结果是合法结果：既不回落快照，也不抛限流错误。"""
        snapshot = {"home": {"variety": {"sections": [
            {"title": "最近热门", "items": [{"douban_id": "1", "title": "快照综艺"}]}]}}}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {"data": []}, snapshot=snapshot)
        result = catalog.show("variety", genre="真人秀")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["source"], "douban")

    def test_explore_rows_keep_card_subtitle_so_area_filter_works(self):
        """2026-09-18 用户报障「片库筛选不生效（例如地区）」：上游 series/variety 分类不认
        genres=，所以列表行必须带 card_subtitle 解析出的 regions/genres，本地兜底过滤才有效。"""
        rows = [
            {"id": "1", "title": "美剧甲", "rate": "8.0", "card_subtitle": "2024 / 美国 / 剧情 犯罪"},
            {"id": "2", "title": "国产剧乙", "rate": "8.0", "card_subtitle": "2024 / 中国大陆 / 剧情"},
        ]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows))
        result = catalog.show("tv", region="美国", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["美剧甲"])
        self.assertEqual(result["items"][0]["regions"], ["美国"])
        self.assertEqual(result["items"][0]["genres"], ["剧情", "犯罪"])

    def test_type_filter_drops_non_matching_rows_locally(self):
        """上游不可信该维度时（此处电视剧的类型，见 `_filter_matrix`）：命中行保留、
        非命中行剔除。电影类型交给上游、本地不再判，另由
        `test_upstream_filtered_dimension_is_not_judged_again_locally` 覆盖。"""
        rows = [
            {"id": "1", "title": "悬疑片", "rate": "8.0", "card_subtitle": "2024 / 中国大陆 / 悬疑"},
            {"id": "2", "title": "喜剧片", "rate": "8.0", "card_subtitle": "2024 / 中国大陆 / 喜剧"},
        ]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows))
        result = catalog.show("tv", genre="悬疑", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["悬疑片"])

    def test_filtered_show_enriches_rows_when_list_has_no_regions(self):
        """线上实测：Explore 列表行不含地区/类型（genres/regions 全空），带筛选时要补详情
        再判断，否则「选了地区」等于没筛（用户 2026-09-18 报障）。"""
        def fetch(url):
            if "/subject/1" in url:
                return {"title": "美剧甲", "year": "2024", "countries": ["美国"],
                        "genres": ["剧情"], "rating": {"value": 8.0}}
            if "/subject/2" in url:
                return {"title": "国产剧乙", "year": "2024", "countries": ["中国大陆"],
                        "genres": ["剧情"], "rating": {"value": 8.0}}
            return search_payload([
                {"id": "1", "title": "美剧甲", "rate": "8.5", "cover": "https://img.example/1.jpg"},
                {"id": "2", "title": "国产剧乙", "rate": "8.5", "cover": "https://img.example/2.jpg"},
            ])

        catalog = douban_catalog.DoubanCatalog(fetch=fetch)
        result = catalog.show("tv", region="美国", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["美剧甲"])
        self.assertEqual(result["items"][0]["regions"], ["美国"])

    def test_filters_offer_the_full_douban_type_vocabulary(self):
        """用户报障「类型条件太少了」：类型表要覆盖豆瓣真实类型，而不是 10 个以内。"""
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {})
        movie_types = catalog.filters("movie")["types"]
        self.assertGreaterEqual(len(movie_types) - 1, 20)
        for name in ("惊悚", "恐怖", "纪录片", "歌舞", "传记"):
            self.assertIn(name, movie_types)
        for name in ("古装", "短片", "惊悚"):
            self.assertIn(name, catalog.filters("tv")["types"])
        self.assertIn("中国台湾", catalog.filters("tv")["areas"])

    def test_disk_cache_filenames_never_collide_on_chinese_filters(self):
        """2026-09-18「片库筛选不生效」根因回归：缓存文件名旧实现把非 ASCII 字符整段换成
        下划线，导致 `show:movie:::美国::U:1` 与 `show:movie:::::U:1` 撞成同一个文件，
        带中文的地区/类型筛选就命中了未筛选的缓存结果。"""
        cache = douban_catalog.JsonDiskCache(tempfile.mkdtemp())
        plain = cache._path("show:movie:::::U:1")
        us = cache._path("show:movie:::美国::U:1")
        jp = cache._path("show:movie:::日本::U:1")
        self.assertNotEqual(plain, us)
        self.assertNotEqual(us, jp)
        self.assertEqual(len({plain.name, us.name, jp.name}), 3)

    def test_blocked_upstream_payload_raises_instead_of_looking_empty(self):
        """豆瓣拦 IP 时回 HTTP 200 + `{"msg": ...}`（**没有 `data` 键**）。旧实现把它当成
        「这个筛选没有结果」，空结果于是被写进 6 小时缓存：用户看到假空、恢复后也不刷新。"""
        blocked = {"msg": "检测到有异常请求从您的IP发出，请登录再试", "r": 1}
        with self.assertRaises(douban_catalog.DoubanUnavailable):
            douban_catalog.explore_rows(blocked)
        with self.assertRaises(douban_catalog.DoubanUnavailable):
            douban_catalog.explore_rows("<html>502</html>")
        # 合法的空结果是 `{"data": []}`，必须照常返回空列表。
        self.assertEqual(douban_catalog.explore_rows({"data": []}), [])

    def test_blocked_upstream_never_writes_a_cache_entry(self):
        cache = douban_catalog.JsonDiskCache(tempfile.mkdtemp())
        blocked = {"msg": "检测到有异常请求从您的IP发出，请登录再试", "r": 1}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: blocked, cache=cache, snapshot={})
        result = catalog.show("movie", region="日本", page=1)
        self.assertFalse(result["upstream_available"])
        self.assertEqual(result["source"], "douban-unavailable")
        self.assertEqual(result["items"], [])
        self.assertEqual(list(pathlib.Path(cache.directory).glob("*.json")), [])

    def test_expired_cache_is_served_while_upstream_is_blocked(self):
        """同筛选条件的最近一次成功结果必须能顶上来，而不是给用户一个假空。"""
        cache = douban_catalog.JsonDiskCache(tempfile.mkdtemp())
        key = "show:movie:::::U:1"
        cache.put(key, {"category": "movie", "page": 1, "has_more": False, "source": "douban",
                        "stale": False, "items": [{"id": "douban:1", "title": "好结果"}]},
                  fetched_at=time.time() - douban_catalog.TTL["show"] - 60)
        blocked = {"msg": "检测到有异常请求从您的IP发出，请登录再试", "r": 1}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: blocked, cache=cache, snapshot={})
        result = catalog.show("movie", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["好结果"])
        self.assertEqual(result["source"], "douban-cache")
        self.assertTrue(result["stale"])

    def test_detail_fetch_failure_does_not_put_catalogue_upstream_into_cooldown(self):
        """详情走另一个域（m.douban.com），实测被拦时仍可用 —— 详情失败绝不能把
        目录检索一起打进冷却，否则一次补详情失败会让整页筛选退化成快照。"""
        def fetch(url):
            if "/subject/" in url:
                raise OSError("detail boom")
            return search_payload([explore_row(1, "甲"), explore_row(2, "乙")])

        catalog = douban_catalog.DoubanCatalog(fetch=fetch)
        self.assertEqual([item["title"] for item in catalog._section_or_empty("movie", "U")],
                         ["甲", "乙"])
        self.assertFalse(catalog._upstream_cooling())

    def test_valid_empty_page_keeps_upstream_available_true(self):
        """`{"data": []}` 是合法空结果，不能被降级链当成上游故障。"""
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload([]), snapshot={})
        result = catalog.show("movie", genre="灾难", page=1)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["upstream_available"])
        self.assertEqual(result["source"], "douban")

    def test_filtered_show_does_not_serve_snapshot_first(self):
        """带筛选是用户显式查询：冷缓存不能先回快照就结束（快照里能匹配的条目极少，
        用户会再次看到「筛选没内容」）。无筛选仍保留「秒回快照 + 后台刷新」。"""
        snapshot = {"home": {"movie": {"sections": [
            {"title": "最近热门", "items": [{"douban_id": "9", "title": "快照片",
                                             "genres": ["剧情"], "regions": ["日本"]}]}]}}}
        live = [{"id": "1", "title": "线上片", "rate": "8.0", "card_subtitle": "2024 / 日本 / 剧情"}]
        cache = douban_catalog.JsonDiskCache(tempfile.mkdtemp())
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(live),
                                               cache=cache, snapshot=snapshot)
        filtered = catalog.show("movie", region="日本", page=1)
        self.assertEqual([item["title"] for item in filtered["items"]], ["线上片"])
        self.assertTrue(filtered["upstream_available"])

    def test_explore_uses_dedicated_country_and_year_params(self):
        """地区/年份必须走 `countries=` / `year_range=`，不能塞进 `tags=` —— 旧实现把
        `tags=电影,日本` 交给豆瓣，上游根本没按地区筛，「选了地区像没选」就是这么来的。"""
        calls = []
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda url: calls.append(url) or search_payload([]), snapshot={})
        catalog.show("movie", region="日本", year="1994", page=1)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        self.assertEqual(query["tags"], ["电影"])
        self.assertEqual(query["countries"], ["日本"])
        self.assertEqual(query["year_range"], ["1994,1994"])

    def test_early_year_uses_a_range_instead_of_being_ignored(self):
        """「更早」旧实现等价于不筛选（网页端直接发空年份，后端也忽略它）。"""
        current = time.localtime().tm_year
        self.assertEqual(douban_catalog.year_range_value("更早"), "1,%d" % (current - 12))
        self.assertEqual(douban_catalog.year_range_value("2024"), "2024,2024")
        self.assertEqual(douban_catalog.year_range_value("全部"), "")
        calls = []
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda url: calls.append(url) or search_payload([]), snapshot={})
        catalog.show("movie", year="更早", page=1)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        self.assertEqual(query["year_range"], ["1,%d" % (current - 12)])
        self.assertNotIn("更早", query["tags"][0])

    def test_unknown_metadata_is_dropped_instead_of_passed_through(self):
        """旧实现「字段未知就放行」：综艺/短剧的条目录从未补过类型/地区，于是筛选等于
        没筛（实测 `show(variety, 任意筛选)` 返回 20 条、真实匹配 0 条）。"""
        rows = [
            {"id": "1", "title": "无元数据", "rate": "8.0"},
            {"id": "2", "title": "真日本片", "rate": "8.0", "card_subtitle": "2024 / 日本 / 剧情"},
        ]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows), snapshot={})
        result = catalog.show("variety", region="日本", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["真日本片"])

    def test_upstream_filtered_dimension_is_not_judged_again_locally(self):
        """电影类型由豆瓣服务端筛（`genre=` 实测有效）。本地不得再判一次，否则列表行
        没带 genres 的条目会被误杀。"""
        rows = [{"id": "1", "title": "科幻片", "rate": "8.0"}]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows), snapshot={})
        self.assertEqual([item["title"] for item in catalog.show("movie", genre="科幻")["items"]],
                         ["科幻片"])

    def test_untrusted_genre_is_not_sent_upstream(self):
        """矩阵说不信任类型时，连 `genres=` 都不发 —— 上游对某些分类传该参数会直接回空页，
        那样用户会看到「一片空白」而不是「筛出的少数准确结果」。"""
        calls = []
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda url: calls.append(url) or search_payload([]), snapshot={})
        catalog.show("variety", genre="真人秀", page=1)
        self.assertNotIn("genres", urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query))

    def test_detail_is_fetched_only_for_rows_missing_the_needed_field(self):
        """带筛选不再逐条补详情：只有缺判据字段的行才补（这就是性能热点）。"""
        fetched = []

        def fetch(url):
            if "/subject/" in url:
                fetched.append(url.rsplit("/", 1)[-1])
                return {"title": "补详情", "countries": ["日本"], "genres": ["剧情"]}
            return search_payload([
                {"id": "1", "title": "有元数据", "rate": "8.0", "card_subtitle": "2024 / 日本 / 剧情"},
                {"id": "2", "title": "缺元数据", "rate": "8.0"},
            ])

        catalog = douban_catalog.DoubanCatalog(fetch=fetch, snapshot={})
        result = catalog.show("variety", region="日本", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["有元数据", "缺元数据"])
        self.assertEqual([item["regions"] for item in result["items"]], [["日本"], ["日本"]])
        self.assertEqual(fetched, ["2"])

    def test_matrix_file_makes_upstream_trusted_for_a_dimension(self):
        """矩阵数据说了算：实测某分类支持某参数，就把该维度交给上游、本地不再复判。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "douban_filter_matrix.json"
            path.write_text(json.dumps({"movie": {"genre": True}}), encoding="utf-8")
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda _url: search_payload([{"id": "1", "title": "无元数据", "rate": "8"}]),
                snapshot={}, matrix_path=path)
            self.assertTrue(catalog._filter_matrix()["movie"]["genre"])
            self.assertEqual([i["title"] for i in catalog.show("movie", genre="科幻")["items"]],
                             ["无元数据"])

    def test_missing_matrix_file_falls_back_to_conservative_defaults(self):
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda _url: search_payload([]), snapshot={},
            matrix_path=pathlib.Path(tempfile.mkdtemp()) / "absent.json")
        matrix = catalog._filter_matrix()
        self.assertTrue(matrix["movie"]["genre"])
        self.assertFalse(matrix["tv"]["genre"])

    def test_bundled_matrix_file_covers_every_category(self):
        """随包矩阵必须覆盖全部分类与维度：缺项会让 `_filter_matrix` 静默用缺省，
        这种「配置写漏了」必须被测试挡住。"""
        raw = json.loads(douban_catalog.FILTER_MATRIX_PATH.read_text(encoding="utf-8"))
        for category in douban_catalog.CATEGORY_VALUES:
            self.assertIn(category, raw)
            for dimension in douban_catalog.FILTER_DIMENSIONS:
                self.assertIsInstance(raw[category][dimension], bool)

    def test_has_more_is_false_when_upstream_runs_out(self):
        """旧实现带筛选时用 `bool(rows)` 判断，用户会看到「加载更多」点下去是空的。"""
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda _url: search_payload([{"id": "1", "title": "唯一一条", "rate": "8"}]),
            snapshot={})
        self.assertFalse(catalog.show("movie", region="日本", page=1)["has_more"])

    def test_has_more_is_true_when_last_upstream_page_is_full(self):
        rows = [{"id": str(index), "title": "片%d" % index, "rate": "8.0",
                 "card_subtitle": "2024 / 日本 / 剧情"}
                for index in range(douban_catalog.EXPLORE_PAGE_SIZE)]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows), snapshot={})
        self.assertTrue(catalog.show("movie", region="日本", page=1)["has_more"])

    def test_filter_options_merge_values_seen_in_snapshot(self):
        """筛选项要覆盖随包快照里真实出现过的值，否则会出现「点了没内容」。"""
        snapshot = {"index": [{"douban_id": "1", "title": "片", "category": "movie",
                               "genres": ["西部"], "regions": ["爱尔兰"]}]}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {}, snapshot=snapshot)
        result = catalog.filters("movie")
        self.assertIn("西部", result["types"])
        self.assertIn("爱尔兰", result["areas"])
        self.assertEqual(result["types"][0], "全部")
        self.assertEqual(result["areas"][0], "全部")
        self.assertTrue(result["upstream_available"])

    def test_filter_options_never_show_summary_fragments_or_latin_regions(self):
        """2026-09-19 用户报障「地区筛选候选词有问题」：card_subtitle 为「年份 / 剧情简介」
        形态时简介残段被按位置切进 regions 槽，地区候选词里出现「热爱音乐的米格尔（安东尼·
        冈萨雷兹」「Anthony」「配音）不幸地出生在…」这类脏词；豆瓣偶尔还给英文国名
        （「Austria」），countries= 参数不认、选了必空。两类脏词都不许进候选表。"""
        snapshot = {"index": [
            {"douban_id": "1", "title": "寻梦环游记", "category": "movie",
             "regions": ["热爱音乐的米格尔（安东尼·冈萨雷兹", "Anthony", "Gonzalez",
                         "配音）不幸地出生在一个视音乐为洪水猛兽的大家庭之中，好继..."],
             "genres": []},
            {"douban_id": "2", "title": "伊丽莎白", "category": "movie",
             "regions": ["Austria"], "genres": ["剧情", "歌舞"]},
            {"douban_id": "3", "title": "正常片", "category": "movie",
             "regions": ["爱尔兰"], "genres": ["西部"]},
        ]}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {}, snapshot=snapshot)
        result = catalog.filters("movie")
        for area in result["areas"]:
            self.assertRegex(area, r"^(全部|[一-鿿]{2,12})$", area)
        self.assertIn("爱尔兰", result["areas"])
        self.assertNotIn("Austria", result["areas"])
        for genre in result["types"]:
            self.assertRegex(genre, r"^(全部|[一-鿿]{2,12})$", genre)
        self.assertIn("西部", result["types"])
        self.assertIn("歌舞", result["types"])

    def test_card_subtitle_with_summary_only_does_not_pollute_regions(self):
        """摘要只有「年份 / 剧情简介」两段时，简介不许落进地区/类型槽（防新增条目再脏）。"""
        parsed = douban_catalog.parse_card_subtitle(
            "2017 / 热爱音乐的米格尔（安东尼·冈萨雷兹 Anthony Gonzalez 配音）不幸地出生在一个大家庭")
        self.assertEqual(parsed.get("year"), "2017")
        self.assertEqual(parsed.get("regions"), [])
        # 正常形态不受影响：地区段是中文词就保留
        parsed = douban_catalog.parse_card_subtitle("2023 / Austria / 剧情 歌舞")
        self.assertEqual(parsed.get("regions"), [])
        self.assertEqual(parsed.get("genres"), ["剧情", "歌舞"])
        parsed = douban_catalog.parse_card_subtitle("2024 / 中国大陆 / 悬疑 剧情 / 张某某 / 李某某")
        self.assertEqual(parsed.get("regions"), ["中国大陆"])
        self.assertEqual(parsed.get("genres"), ["悬疑", "剧情"])
        self.assertEqual(parsed.get("directors"), ["张某某"])

    def test_movie_rule_drops_concerts_and_stage_but_keeps_musical_films(self):
        """电影栏不许出现演唱会/舞台实录：豆瓣把这些也标成 movie，只能靠标题/又名认。"""
        rows = [explore_row(111, "陈奕迅DUO演唱会"), explore_row(222, "汉密尔顿"),
                explore_row(333, "悲惨世界"), explore_row(444, "肖申克的救赎"),
                explore_row(555, "秋前算账"), explore_row(666, "2008年第29届北京奥运会开幕式"),
                explore_row(777, "雷曼兄弟三部曲")]
        details = {
            "111": {"title": "陈奕迅DUO演唱会", "genres": ["音乐", "歌舞"], "aka": ["DUO:陈奕迅2010演唱会"]},
            "222": {"title": "汉密尔顿", "aka": ["汉密尔顿音乐剧"]},
            # 现场演出品牌只在原名里：判定必须用到详情补进来的 original_title
            "777": {"title": "雷曼兄弟三部曲", "original_title": "National Theatre Live: The Lehman Trilogy",
                    "genres": ["剧情"]},
            # 标题看不出是现场演出，靠「类型」判定（脱口秀专场 / 大型开幕式）
            "555": {"title": "秋前算账", "genres": ["脱口秀"]},
            "666": {"title": "2008年第29届北京奥运会开幕式", "genres": ["运动"]},
            # 又名里带「音乐剧」但是电影版：必须留下
            "333": {"title": "悲惨世界", "aka": ["音乐剧《悲惨世界》电影版"], "genres": ["剧情", "爱情", "歌舞"]},
            "444": {"title": "肖申克的救赎", "genres": ["剧情", "犯罪"], "durations": ["142分钟"]},
        }
        catalog = douban_catalog.DoubanCatalog(fetch=home_fetch({"U": rows, "R": rows, "S": rows}, details))
        result = catalog.home("movie")
        titles = [[item["title"] for item in section["items"]] for section in result["sections"]]
        self.assertEqual(titles, [["悲惨世界", "肖申克的救赎"]] * 3)

    def test_movie_home_backfills_next_page_when_a_section_is_mostly_stage(self):
        """高分栏里演唱会占了大半时，要继续翻页补足，而不是只留几部片子。"""
        concerts = [explore_row(1000 + i, "某演唱会 %d" % i) for i in range(20)]
        rows = concerts + [explore_row(444, "肖申克的救赎")]
        details = {str(1000 + i): {"title": "某演唱会 %d" % i, "aka": ["%d 巡回演唱会" % i]} for i in range(20)}
        details["444"] = {"title": "肖申克的救赎", "genres": ["剧情", "犯罪"]}
        catalog = douban_catalog.DoubanCatalog(fetch=home_fetch({"U": rows, "R": rows, "S": rows}, details))
        section = catalog.home("movie")["sections"][0]
        self.assertEqual([item["title"] for item in section["items"]], ["肖申克的救赎"])

    def test_tv_rule_drops_short_dramas_but_keeps_normal_series(self):
        """电视剧栏不许出现短剧：短片类型、单集 ≤5 分钟、无剧集元数据三种都要剔掉。"""
        rows = [explore_row(1, "婚后引力"), explore_row(2, "糯糯下山"),
                explore_row(3, "妾本草芥"), explore_row(4, "早春晴朗")]
        details = {
            "1": {"title": "婚后引力", "genres": ["剧情", "爱情", "短片"]},
            "2": {"title": "糯糯下山", "genres": ["剧情"], "durations": ["2分钟"], "episodes_count": 152},
            "3": {"title": "妾本草芥", "genres": ["古装"]},
            "4": {"title": "早春晴朗", "genres": ["剧情", "爱情"], "durations": ["45分钟"], "episodes_count": 24},
        }
        catalog = douban_catalog.DoubanCatalog(fetch=home_fetch({"U": rows, "R": rows, "S": rows}, details))
        titles = [[item["title"] for item in section["items"]]
                  for section in catalog.home("tv")["sections"]]
        self.assertEqual(titles, [["早春晴朗"]] * 3)

    def test_unknown_details_never_drop_items(self):
        """详情拿不到时必须原样保留：判据缺失 ≠ 命中规则。"""
        rows = [explore_row(1, "婚后引力"), explore_row(2, "早春晴朗")]
        catalog = douban_catalog.DoubanCatalog(fetch=home_fetch({"U": rows, "R": rows, "S": rows}, {}))
        for category in ("movie", "tv"):
            titles = [[item["title"] for item in section["items"]]
                      for section in catalog.home(category)["sections"]]
            self.assertEqual(titles, [["婚后引力", "早春晴朗"]] * 3)

    def test_snapshot_fallback_sections_are_filtered_too(self):
        """云端/离线只能走快照：快照条目带类型时同样要应用分组规则。"""
        snapshot = {"home": {"tv": {"sections": [
            {"title": "最近热门", "items": [
                {"douban_id": "1", "title": "婚后引力", "genres": ["剧情", "短片"]},
                {"douban_id": "2", "title": "早春晴朗", "genres": ["剧情"], "durations": ["45分钟"]}]},
            {"title": "最新上映", "items": [
                {"douban_id": "3", "title": "妾本草芥", "genres": ["古装"]}]},
            {"title": "豆瓣高分", "items": [
                {"douban_id": "4", "title": "繁花", "genres": ["剧情"], "durations": ["45分钟"]}]}]}}}
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")), snapshot=snapshot)
        result = catalog.home("tv")
        self.assertEqual(result["source"], "douban-snapshot")
        self.assertEqual([[item["title"] for item in section["items"]] for section in result["sections"]],
                         [["早春晴朗"], ["妾本草芥"], ["繁花"]])

    def test_home_fetches_the_three_sections_in_parallel(self):
        """三栏并行：串行时单次 /vod/home 至少 1.8s 上游耗时（实测 1.80s → 0.63s）。

        用 Barrier 而不是计时断言——串行执行会让第 1 栏等到超时，测试确定性失败。
        """
        barrier = threading.Barrier(3, timeout=5)

        def fetch(url):
            if "/subject/" in url:
                return {}
            barrier.wait()
            return search_payload([{"id": "1", "title": "节目", "rate": "8.0"}])

        result = douban_catalog.DoubanCatalog(fetch=fetch).home("variety")
        self.assertEqual([len(section["items"]) for section in result["sections"]], [1, 1, 1])

    def test_expired_cache_is_served_at_once_and_refreshed_in_background(self):
        """TTL 过期不能把用户扣在请求上：旧值先上屏，抓取转后台，下一次读到新值。"""
        with tempfile.TemporaryDirectory() as tmp:
            cache = douban_catalog.JsonDiskCache(tmp)
            cache.put("home:variety", {
                "category": "variety", "stale": False, "source": "douban",
                "sections": [{"title": title, "items": [{"douban_id": "1", "title": "上一版"}]}
                             for title in douban_catalog.HOME_SECTION_NAMES],
            }, fetched_at=time.time() - 86400)
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda _url: search_payload([{"id": "2", "title": "新一版", "rate": "8.0"}]),
                cache=cache)

            result = catalog.home("variety")
            self.assertTrue(result["stale"])
            self.assertEqual(result["sections"][0]["items"][0]["title"], "上一版")

            # 后台刷新不受 time.sleep 补丁影响：借 Event.wait 当真实等待。
            tick = threading.Event()
            for _ in range(250):
                refreshed = cache.get_stale("home:variety")
                if refreshed["sections"][0]["items"][0]["title"] == "新一版":
                    break
                tick.wait(0.02)
            self.assertEqual(cache.get_stale("home:variety")["sections"][0]["items"][0]["title"], "新一版")

    def test_explicit_home_refresh_bypasses_still_fresh_cache(self):
        """用户再次打开影视页时，即使六小时 TTL 未到也必须检查新增影片。"""
        with tempfile.TemporaryDirectory() as tmp:
            cache = douban_catalog.JsonDiskCache(tmp)
            cache.put("home:variety", {
                "category": "variety", "stale": False, "source": "douban",
                "sections": [{"title": title, "items": [{"douban_id": "1", "title": "缓存旧片"}]}
                             for title in douban_catalog.HOME_SECTION_NAMES],
            }, fetched_at=time.time())
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda _url: search_payload([{"id": "2", "title": "源站新增片", "rate": "8.0"}]),
                cache=cache)

            refresh_home = getattr(catalog, "refresh_home", catalog.home)
            result = refresh_home("variety")

            self.assertEqual(result["sections"][0]["items"][0]["title"], "源站新增片")

    def test_explicit_detail_refresh_bypasses_still_fresh_cache(self):
        """用户再次打开详情时，七天 TTL 未到也必须取得源站更新后的元数据。"""
        with tempfile.TemporaryDirectory() as tmp:
            cache = douban_catalog.JsonDiskCache(tmp)
            cache.put("detail:1292052", {
                "id": "douban:1292052", "douban_id": "1292052", "title": "旧详情",
                "source": "douban", "stale": False,
            }, fetched_at=time.time())
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda _url: {"id": "1292052", "title": "更新后详情"}, cache=cache)

            refresh_detail = getattr(catalog, "refresh_detail", catalog.detail)
            result = refresh_detail("1292052")

            self.assertEqual(result["title"], "更新后详情")

    def test_cold_cache_serves_snapshot_first_and_refreshes_in_background(self):
        """冷缓存（首装 / 热更换了缓存文件名后的第一次运行）不许同步等源站。

        旧实现：没有 fresh、也没有 stale 就直接跑 produce —— 影视页 5 个分类并发冷抓
        （每类 3 栏 × 最多 3 页 + 每条补详情），手机上整轮跑不完，客户端 45s 读超时
        一到 5 路全失败，整页影视变空（2026-09-18 用户报障）。
        现在：内置快照先秒回（stale），抓取转后台，下一次请求读到新值。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache = douban_catalog.JsonDiskCache(tmp)
            snapshot = {"home": {"variety": {"category": "variety", "sections": [
                {"title": title, "items": [{"douban_id": "1", "title": "快照"}]}
                for title in douban_catalog.HOME_SECTION_NAMES]}}}
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda _url: search_payload([{"id": "2", "title": "新一版", "rate": "8.0"}]),
                cache=cache, snapshot=snapshot)

            result = catalog.home("variety")
            self.assertEqual(result["source"], "douban-snapshot")
            self.assertTrue(result["stale"])
            self.assertEqual(result["sections"][0]["items"][0]["title"], "快照")

            # 后台刷新（真实等待：time.sleep 已被本用例补丁成空操作）
            tick = threading.Event()
            for _ in range(250):
                refreshed = cache.get_stale("home:variety")
                if refreshed and refreshed["sections"][0]["items"][0]["title"] == "新一版":
                    break
                tick.wait(0.02)
            self.assertEqual(cache.get_stale("home:variety")["sections"][0]["items"][0]["title"], "新一版")
            # 后台写入后，后续请求直接吃新值（源站结果，不再是快照）
            self.assertEqual(catalog.home("variety")["source"], "douban")

    def test_home_without_snapshot_still_returns_three_sections(self):
        """快照缺失时也要给出标准三栏形状，客户端不必再处理「没有 sections」。"""
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {"data": []}, snapshot={})
        result = catalog.home("variety")
        self.assertEqual([section["title"] for section in result["sections"]],
                         list(douban_catalog.HOME_SECTION_NAMES))
        self.assertEqual([section["items"] for section in result["sections"]], [[], [], []])

    def test_hero_poster_url_upgrades_to_the_largest_douban_size(self):
        """影视页顶部 hero 全宽铺图：列表给的最小号会把 1920 宽拉糊，统一换最大尺寸前缀。"""
        self.assertEqual(
            douban_catalog.hero_poster_url(
                "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2935109312.jpg"),
            "https://img3.doubanio.com/view/photo/large/public/p2935109312.jpg")
        self.assertEqual(
            douban_catalog.hero_poster_url(
                "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p457760035.jpg"),
            "https://img9.doubanio.com/view/photo/large/public/p457760035.jpg")
        # 非豆瓣图床原样不认（返回空串，由调用方回落到原字段）
        self.assertEqual(douban_catalog.hero_poster_url("https://img.example/1292052.webp"), "")
        self.assertEqual(douban_catalog.hero_poster_url(""), "")

    def test_normalized_subject_carries_hero_url(self):
        item = douban_catalog.normalize_subject({
            "id": "1292052", "title": "肖申克的救赎",
            "pic": {"large": "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p480747492.jpg"},
        })
        self.assertEqual(item["hero_url"],
                         "https://img3.doubanio.com/view/photo/large/public/p480747492.jpg")
        # 拿不到大图时退回原海报，hero 不能因此变空
        plain = douban_catalog.normalize_subject({"id": "1", "title": "片", "poster_url": "https://img.example/a.jpg"})
        self.assertEqual(plain["hero_url"], "https://img.example/a.jpg")

    def test_douban_default_poster_is_not_stored_as_a_real_poster(self):
        """豆瓣无剧照时回默认占位图（HTTP 200 + 真图），客户端分不出 → 卡片长期显示灰图。

        实测两种形态（2026-10-01，线上图片代理）：占位图 69B~6591B 且全部 200，
        真海报 100KB+，所以必须在**入库前**识别成「无海报」存空串。
        """
        placeholders = (
            "https://asset.doubanio.com/cuphead/movie-static/pics/movie_default_large.png",
            "https://asset.doubanio.com/cuphead/tv-static/pics/tv_default_large.png",
            "https://img2.doubanio.com/f/frodo/753fa1e93c945e533e52dddab73b2b55a1083083"
            "/pics/subject/movie_large.jpg",
        )
        for url in placeholders:
            with self.subTest(url=url):
                self.assertTrue(douban_catalog.is_placeholder_poster(url))
                self.assertEqual(douban_catalog.real_poster_url(url), "")
                item = douban_catalog.normalize_subject(
                    {"id": "1", "title": "片", "poster_url": url})
                # 占位图不能出现在 poster/hero 任何一个字段里，否则端侧照样渲染灰卡片。
                self.assertEqual(item["poster_url"], "")
                self.assertEqual(item["hero_url"], "")
                # 空串同样是「没有海报」
                self.assertTrue(douban_catalog.is_placeholder_poster(""))
        # 快照抓取走 normalize_explore_row（cover 字段），同样要拦住
        self.assertEqual(
            douban_catalog.normalize_explore_row(
                {"id": "1", "title": "片", "cover": placeholders[0]}, "movie")["poster_url"],
            "")

    def test_legacy_disk_cache_does_not_serve_douban_default_posters(self):
        """修复前的 detail/home 磁盘缓存可能保留豆瓣默认图，读取时也必须清掉。"""
        placeholder = "https://asset.doubanio.com/cuphead/movie-static/pics/movie_default_large.png"
        actual = "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p2885762590.jpg"
        with tempfile.TemporaryDirectory() as tmp:
            cache = douban_catalog.JsonDiskCache(tmp)
            cached_home = {
                "category": "movie",
                "sections": [{
                    "title": title,
                    "items": [
                        {"id": "douban:1", "title": "旧缓存占位图", "poster_url": placeholder,
                         "hero_url": placeholder, "backdrop_url": placeholder},
                        {"id": "douban:2", "title": "旧缓存真海报", "poster_url": actual,
                         "hero_url": actual, "backdrop_url": ""},
                    ],
                } for title in douban_catalog.HOME_SECTION_NAMES],
            }
            cached_detail = {
                "id": "douban:1", "douban_id": "1", "title": "旧详情占位图",
                "poster_url": placeholder, "hero_url": placeholder, "backdrop_url": placeholder,
            }
            cache.put("home:movie", cached_home)
            cache.put("detail:1", cached_detail)
            home_fetched_at = cache.envelope("home:movie")["fetched_at"]
            detail_fetched_at = cache.envelope("detail:1")["fetched_at"]
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda *_args: (_ for _ in ()).throw(AssertionError("fresh cache should not fetch")),
                cache=cache, snapshot={},
            )

            home = catalog.home("movie")
            detail = catalog.detail("1")
            self.assertEqual(cache.get_stale("home:movie")["sections"][0]["items"][0]["poster_url"], "")
            self.assertEqual(cache.get_stale("detail:1")["poster_url"], "")
            self.assertEqual(cache.envelope("home:movie")["fetched_at"], home_fetched_at)
            self.assertEqual(cache.envelope("detail:1")["fetched_at"], detail_fetched_at)
            stale_fetched_at = time.time() - douban_catalog.TTL["home"] - 60
            cache.put("home:tv", {
                "category": "tv",
                "sections": [{"title": title, "items": [
                    {"id": "douban:3", "title": "过期缓存占位图", "poster_url": placeholder,
                     "hero_url": placeholder}
                ]} for title in douban_catalog.HOME_SECTION_NAMES],
            }, fetched_at=stale_fetched_at)
            catalog._refresh_async = lambda *_args: None
            stale_home = catalog.home("tv")
            self.assertEqual(stale_home["source"], "douban-cache")
            self.assertEqual(stale_home["sections"][0]["items"][0]["poster_url"], "")
            self.assertEqual(cache.get_stale("home:tv")["sections"][0]["items"][0]["poster_url"], "")
            self.assertEqual(cache.envelope("home:tv")["fetched_at"], stale_fetched_at)

        home_items = home["sections"][0]["items"]
        self.assertEqual(home_items[0]["poster_url"], "")
        self.assertEqual(home_items[0]["hero_url"], "")
        self.assertEqual(home_items[0]["backdrop_url"], "")
        self.assertEqual(home_items[1]["poster_url"], actual)
        self.assertEqual(detail["poster_url"], "")
        self.assertEqual(detail["hero_url"], "")
        self.assertEqual(detail["backdrop_url"], "")

    def test_real_douban_posters_survive_placeholder_filtering(self):
        """反例：真海报不能被误判成占位图（占位图判定过宽会把整片墙变成空卡）。"""
        for url in (
            "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p2934583425.jpg",
            "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2935109312.jpg",
            "https://img2.doubanio.com/view/photo/l_ratio_poster/public/p123.jpg",
            "https://img.example/cover.webp",
        ):
            with self.subTest(url=url):
                self.assertFalse(douban_catalog.is_placeholder_poster(url))
                self.assertEqual(douban_catalog.real_poster_url(url), url)
        item = douban_catalog.normalize_subject(
            {"id": "2", "title": "年会不能停！2",
             "poster_url": "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p2934583425.jpg"})
        self.assertEqual(item["poster_url"],
                         "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p2934583425.jpg")
        self.assertEqual(item["hero_url"],
                         "https://img9.doubanio.com/view/photo/large/public/p2934583425.jpg")


if __name__ == "__main__":
    unittest.main()
