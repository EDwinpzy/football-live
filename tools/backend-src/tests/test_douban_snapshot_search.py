"""随包豆瓣快照的离线搜索覆盖回归。

2026-09-30 线上事故：605 条快照里没有任何「声生不息」，`/api/search?q=声生`
返回空。这类条目只在豆瓣站内搜索里能搜到（首页三段/热门榜取不到），所以光
「重建快照」救不回来——重建照样会把它挤掉。这里同时钉住两件事：

1. 快照 index + details 覆盖已知的高价值站内搜索专属条目；
2. 重建快照时不能把上一版搜索池里、这次热门榜没抓到的条目连同详情一起丢掉，
   否则下一次发布会再次把这些数据清空。
"""
import json
import pathlib
import sys
import tempfile
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT / "tools"))

import build_douban_snapshot as builder  # noqa: E402
import douban_catalog  # noqa: E402
import media_index  # noqa: E402


SNAPSHOT = BACKEND / "douban_snapshot.json"

# 只能从豆瓣站内搜索（「热门榜三段」取不到）拿到的条目：真实豆瓣 ID，
# 快照必须长期携带它们，否则离线搜索永远搜不到。
PINNED_SUBJECTS = {
    "35634451": "声生不息",
    "35905181": "声生不息·宝岛季",
    "36219121": "声生不息·大湾区季",
    "36588224": "声生不息·家年华",
    "37116456": "声生不息·华流季",
    "37816820": "声生不息2026",
}

# 离线搜索必须命中的关键词（对应线上 `/api/search?q=` 的空结果事故）。
SEARCH_TERMS = ("声生", "声生不息")


def offline_fetch(*_args, **_kwargs):
    raise OSError("offline")


class SnapshotSearchCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        cls.pool = {str(item.get("douban_id") or ""): item
                    for item in cls.snapshot.get("index") or []}
        cls.details = cls.snapshot.get("details") or {}

    def test_snapshot_index_carries_site_search_only_subjects(self):
        missing = {key: title for key, title in PINNED_SUBJECTS.items()
                   if key not in self.pool}
        self.assertFalse(missing, "快照搜索池缺少站内搜索专属条目：%s" % missing)
        for key, title in PINNED_SUBJECTS.items():
            self.assertEqual(self.pool[key]["title"], title)
            self.assertTrue(str(self.pool[key]["douban_id"]).isdigit())

    def test_snapshot_details_cover_pinned_subjects(self):
        """详情页在云端只能读快照 `details`，缺了就只能显示空标题。"""
        for key, title in PINNED_SUBJECTS.items():
            with self.subTest(douban_id=key):
                self.assertIn(key, self.details)
                self.assertEqual(self.details[key]["title"], title)
                self.assertTrue(self.details[key].get("summary"))
                self.assertTrue(self.details[key].get("poster_url"))
                self.assertTrue(self.details[key].get("genres"))
                self.assertTrue(self.details[key].get("regions"))

    def test_pinned_details_match_the_pool_rows(self):
        """详情与列表行必须同源，避免搜索列表和详情页显示不同年份/类型。"""
        for key in PINNED_SUBJECTS:
            with self.subTest(douban_id=key):
                self.assertEqual(self.details[key]["title"], self.pool[key]["title"])
                self.assertEqual(self.details[key]["category"], self.pool[key]["category"])
                self.assertEqual(self.details[key]["year"], self.pool[key]["year"])

    def test_offline_search_finds_shengsheng_series(self):
        """云端出口到不了豆瓣，`warm_index` 后必须能纯本地搜到这一系列。"""
        with tempfile.TemporaryDirectory() as tmp:
            index = media_index.MediaIndex(pathlib.Path(tmp) / "mytv.db")
            try:
                catalog = douban_catalog.DoubanCatalog(
                    fetch=offline_fetch, snapshot=self.snapshot, media_index=index)
                catalog.warm_index()
                for term in SEARCH_TERMS:
                    with self.subTest(query=term):
                        result = catalog.search(term)
                        hits = {item["douban_id"] for item in result["items"]}
                        self.assertTrue(hits, "离线搜索「%s」没有任何结果" % term)
                        self.assertTrue(set(PINNED_SUBJECTS) <= hits,
                                        "离线搜索「%s」漏了 %s"
                                        % (term, sorted(set(PINNED_SUBJECTS) - hits)))
            finally:
                index.close()

    def test_offline_search_covers_several_real_titles_in_the_bundled_snapshot(self):
        """经典题名与连续剧搜索必须能由本地快照覆盖，不依赖豆瓣在线响应。"""
        expected = {
            "红楼梦": {"1864810"},
            "西游记": {"2156663"},
            "三国演义": {"1830528"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            index = media_index.MediaIndex(pathlib.Path(tmp) / "mytv.db")
            try:
                catalog = douban_catalog.DoubanCatalog(
                    fetch=offline_fetch, snapshot=self.snapshot, media_index=index)
                catalog.warm_index()
                for query, ids in expected.items():
                    with self.subTest(query=query):
                        result = catalog.search(query, limit=100)
                        found = {item["douban_id"] for item in result["items"]}
                        self.assertTrue(ids <= found, "离线搜索「%s」漏了 %s" %
                                        (query, sorted(ids - found)))
            finally:
                index.close()

    def test_warm_index_writes_every_snapshot_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = media_index.MediaIndex(pathlib.Path(tmp) / "mytv.db")
            try:
                catalog = douban_catalog.DoubanCatalog(
                    fetch=offline_fetch, snapshot=self.snapshot, media_index=index)
                self.assertEqual(catalog.warm_index(), len(self.snapshot["index"]))
                self.assertEqual(index.count(), len(self.snapshot["index"]))
            finally:
                index.close()


class SnapshotRebuildPreservesHistoryTest(unittest.TestCase):
    """重建快照时必须保留上一版搜索池的历史条目及其详情。"""

    def stale_entry(self, douban_id, title, summary="旧快照里的简介"):
        return {
            "id": "douban:" + douban_id,
            "douban_id": douban_id,
            "title": title,
            "original_title": "",
            "aliases": [],
            "year": "2022",
            "category": "variety",
            "genres": ["音乐", "真人秀"],
            "regions": ["中国大陆"],
            "tags": [],
            "durations": [],
            "episodes_count": 0,
            "season": 0,
            "rating": 7.3,
            "rating_count": 33623,
            "summary": summary,
            "directors": ["洪啸"],
            "actors": ["何炅"],
            "poster_url": "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p2885762590.jpg",
            "hero_url": "https://img3.doubanio.com/view/photo/large/public/p2885762590.jpg",
            "backdrop_url": "",
            "release_date": "2022-04-24(中国大陆)",
        }

    def setUp(self):
        self.stale_id = "35634451"
        self.kept = self.stale_entry(self.stale_id, "声生不息")
        self.also_kept = self.stale_entry("30000000", "只有旧快照有的片子")
        self.previous = {
            "index": [dict(self.kept), dict(self.also_kept)],
            "details": {self.stale_id: dict(self.kept)},
        }
        self.fresh_pool = [self.stale_entry("11111111", "这次热门榜抓到的片子")]

    def test_history_entries_and_details_survive_a_rebuild(self):
        index, details = builder.preserve_history(
            self.fresh_pool, {}, self.previous)
        by_id = {str(item.get("douban_id") or ""): item for item in index}
        self.assertIn("11111111", by_id)
        # 本次热门榜没有、但旧搜索池有的条目必须留着，且带上旧详情。
        self.assertEqual(by_id[self.stale_id]["title"], "声生不息")
        self.assertEqual(by_id["30000000"]["title"], "只有旧快照有的片子")
        self.assertIn(self.stale_id, details)
        self.assertEqual(details[self.stale_id]["title"], "声生不息")
        self.assertEqual(by_id[self.stale_id]["summary"], "旧快照里的简介")

    def test_history_never_overrides_freshly_fetched_data(self):
        fresh = self.stale_entry(self.stale_id, "声生不息", summary="本次抓到的简介")
        fresh["rating"] = 8.1
        fresh_details = {self.stale_id: fresh}
        index, details = builder.preserve_history([fresh], fresh_details, self.previous)
        by_id = {str(item.get("douban_id") or ""): item for item in index}
        self.assertEqual(by_id[self.stale_id]["summary"], "本次抓到的简介")
        self.assertEqual(details[self.stale_id]["rating"], 8.1)
        self.assertEqual(len(index), 2)  # 旧的 30000000 仍要保住
        self.assertIn("30000000", by_id)

    def test_history_merge_is_idempotent(self):
        first, first_details = builder.preserve_history(
            self.fresh_pool, {}, self.previous)
        second, second_details = builder.preserve_history(
            first, first_details, {"index": first, "details": first_details})
        self.assertEqual([item["douban_id"] for item in first],
                         [item["douban_id"] for item in second])
        self.assertEqual(sorted(first_details), sorted(second_details))

    def test_old_details_do_not_replace_fresh_list_metadata(self):
        fresh = dict(self.kept, rating=8.1, year="2026")
        index, details = builder.preserve_history([fresh], {}, self.previous)
        self.assertEqual(index[0]["rating"], 8.1)
        self.assertEqual(index[0]["year"], "2026")
        self.assertIn(self.stale_id, details)

    def test_broken_previous_snapshot_is_tolerated(self):
        index, details = builder.preserve_history(self.fresh_pool, {}, None)
        self.assertEqual([item["douban_id"] for item in index], ["11111111"])
        self.assertEqual(details, {})
        index, details = builder.preserve_history(
            self.fresh_pool, {}, {"index": "not-a-list", "details": []})
        self.assertEqual([item["douban_id"] for item in index], ["11111111"])
        self.assertEqual(details, {})

    def test_entries_without_id_are_not_carried_over(self):
        index, _details = builder.preserve_history(
            self.fresh_pool, {},
            {"index": [{"title": "没有豆瓣 ID"}, "junk", None], "details": {}})
        self.assertEqual([item["douban_id"] for item in index], ["11111111"])


class SnapshotSourceMergeTest(unittest.TestCase):
    def test_home_and_search_pool_merge_by_id_without_losing_search_fields(self):
        """首页卡片的同 ID 简略副本不能遮住搜索池的别名和分类。"""
        home_item = {
            "id": "douban:1864810", "douban_id": "1864810", "title": "红楼梦",
            "category": "movie", "aliases": [], "genres": [], "year": "1987",
            "rating": 0, "poster_url": "https://img.example/red-dream.jpg",
        }
        index_item = {
            "id": "douban:1864810", "douban_id": "1864810", "title": "红楼梦",
            "category": "tv", "aliases": ["87版红楼梦", "Dream of the Red Chamber"],
            "genres": ["剧情"], "year": "1987", "rating": 9.6,
        }
        snapshot = {
            "home": {"tv": {"sections": [{"title": "最近热门", "items": [home_item]}]}},
            "index": [index_item, {"id": "douban:2156663", "douban_id": "2156663",
                                   "title": "西游记", "category": "tv"}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            index = media_index.MediaIndex(pathlib.Path(tmp) / "mytv.db")
            try:
                catalog = douban_catalog.DoubanCatalog(
                    fetch=offline_fetch, snapshot=snapshot, media_index=index)
                subjects = catalog.snapshot_subjects()
                red_dream = next(item for item in subjects if item["douban_id"] == "1864810")
                self.assertEqual(len([item for item in subjects
                                      if item["douban_id"] == "1864810"]), 1)
                self.assertEqual(red_dream["category"], "tv")
                self.assertEqual(red_dream["aliases"],
                                 ["87版红楼梦", "Dream of the Red Chamber"])
                self.assertEqual(red_dream["genres"], ["剧情"])
                self.assertEqual(red_dream["poster_url"], home_item["poster_url"])
                self.assertEqual(red_dream["rating"], 9.6)

                self.assertEqual(catalog.warm_index(), 2)
                result = catalog.search("87版红楼梦")
                self.assertEqual([item["douban_id"] for item in result["items"]], ["1864810"])
                self.assertEqual(result["items"][0]["category"], "tv")
            finally:
                index.close()


if __name__ == "__main__":
    unittest.main()
