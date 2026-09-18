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
        snapshot = {"home": {"movie": {"sections": [{"title": "最近热门", "items": []}]}}}
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
            snapshot=snapshot,
        )
        result = catalog.home("movie")
        self.assertTrue(result["stale"])
        self.assertEqual(result["source"], "douban-snapshot")

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
        self.assertEqual(query["tags"], ["电影,美国,1994"])
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

    def test_search_reads_local_index_without_network(self):
        class Index:
            def search(self, query, limit=30):
                return [{"id": "douban:1292052", "douban_id": "1292052", "title": query}]

        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda _url: self.fail("local search must not fetch the network"), media_index=Index())
        result = catalog.search("肖申克", limit=7)
        self.assertTrue(result["local"])
        self.assertEqual(result["items"][0]["id"], "douban:1292052")

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
        """类型筛选：命中行保留、非命中行剔除（上游对 genres= 支持因分类而异）。"""
        rows = [
            {"id": "1", "title": "悬疑片", "rate": "8.0", "card_subtitle": "2024 / 中国大陆 / 悬疑"},
            {"id": "2", "title": "喜剧片", "rate": "8.0", "card_subtitle": "2024 / 中国大陆 / 喜剧"},
        ]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows))
        result = catalog.show("movie", genre="悬疑", page=1)
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

    def test_hero_poster_url_upgrades_to_the_largest_douban_size(self):
        """影视页顶部 hero 全宽铺图：列表给的最小号会把 1920 宽拉糊，统一换最大尺寸前缀。"""
        self.assertEqual(
            douban_catalog.hero_poster_url(
                "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2935109312.jpg"),
            "https://img3.doubanio.com/view/photo/l/public/p2935109312.jpg")
        self.assertEqual(
            douban_catalog.hero_poster_url(
                "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p457760035.jpg"),
            "https://img9.doubanio.com/view/photo/l/public/p457760035.jpg")
        # 非豆瓣图床原样不认（返回空串，由调用方回落到原字段）
        self.assertEqual(douban_catalog.hero_poster_url("https://img.example/1292052.webp"), "")
        self.assertEqual(douban_catalog.hero_poster_url(""), "")

    def test_normalized_subject_carries_hero_url(self):
        item = douban_catalog.normalize_subject({
            "id": "1292052", "title": "肖申克的救赎",
            "pic": {"large": "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p480747492.jpg"},
        })
        self.assertEqual(item["hero_url"],
                         "https://img3.doubanio.com/view/photo/l/public/p480747492.jpg")
        # 拿不到大图时退回原海报，hero 不能因此变空
        plain = douban_catalog.normalize_subject({"id": "1", "title": "片", "poster_url": "https://img.example/a.jpg"})
        self.assertEqual(plain["hero_url"], "https://img.example/a.jpg")


if __name__ == "__main__":
    unittest.main()
