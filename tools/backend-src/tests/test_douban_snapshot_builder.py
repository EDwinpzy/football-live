"""离线契约测试：快照首页必须跟随在线目录取数与过滤。"""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse


ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

import build_douban_snapshot as builder  # noqa: E402
import douban_catalog  # noqa: E402


class DoubanSnapshotBuilderTest(unittest.TestCase):
    def test_home_uses_catalog_routes_filters_and_reuses_details(self):
        calls = []

        def fake_get(url, _sleep, attempts=3):
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            calls.append((parsed.path, query))
            if parsed.path == douban_catalog.HOME_API.removeprefix(
                    "https://movie.douban.com"):
                category = query["type"][0]
                tag = query["tag"][0]
                order = query["sort"][0]
                if query.get("page_start", ["0"])[0] != "0":
                    return {"subjects": []}
                if category == "movie":
                    subject_id = {"热门": "101", "最新": "102", "豆瓣高分": "103"}[tag]
                    title = "电影" + tag
                elif tag == "综艺":
                    subject_id, title = "401", "综艺节目"
                else:
                    subject_id = {"recommend": "201", "time": "202", "rank": "203"}[order]
                    title = "剧集" + order
                return {"subjects": [{
                    "id": subject_id,
                    "title": title,
                    "rate": "8.6",
                    "cover": "https://img.doubanio.com/view/photo/s_ratio_poster/public/p%s.jpg" % subject_id,
                    "card_subtitle": "2025 / 中国大陆 / 剧情",
                }]}

            if parsed.path == douban_catalog.EXPLORE_API.removeprefix(
                    "https://movie.douban.com"):
                tag = query.get("tags", [""])[0]
                if query.get("start", ["0"])[0] != "0":
                    return {"data": []}
                if tag == "动漫":
                    return {"data": [
                        {"id": "301", "title": "凡人修仙传 年番4", "rate": "8.8",
                         "cover": "https://img.doubanio.com/view/photo/s_ratio_poster/public/p301.jpg",
                         "card_subtitle": "2025 / 中国大陆 / 动画"},
                        {"id": "302", "title": "药屋少女", "rate": "8.3",
                         "cover": "https://img.doubanio.com/view/photo/s_ratio_poster/public/p302.jpg",
                         "card_subtitle": "2024 / 日本 / 动画"},
                        {"id": "303", "title": "伍六七", "rate": "8.1",
                         "cover": "https://img.doubanio.com/view/photo/s_ratio_poster/public/p303.jpg",
                         "card_subtitle": "2024 / 中国大陆 / 动画"},
                    ]}
                return {"data": [{"id": "501", "title": "短剧节目", "rate": "7.1",
                                  "cover": "https://img.doubanio.com/view/photo/s_ratio_poster/public/p501.jpg"}]}

            if parsed.path.startswith("/rexxar/api/v2/subject/"):
                subject_id = parsed.path.rsplit("/", 1)[-1]
                if subject_id == "301":
                    return {"id": subject_id, "title": "凡人修仙传 年番4",
                            "regions": ["中国大陆"], "genres": ["动画"]}
                if subject_id == "302":
                    return {"id": subject_id, "title": "药屋少女", "regions": ["日本"],
                            "genres": ["动画"]}
                if subject_id == "303":
                    return {"id": subject_id, "title": "伍六七", "regions": ["中国大陆"],
                            "genres": ["动画"]}
                if subject_id.startswith("2"):
                    return {"id": subject_id, "title": "剧集", "regions": ["中国大陆"],
                            "genres": ["剧情"], "durations": ["45分钟"], "episodes_count": 24}
                return {"id": subject_id, "title": "电影", "regions": ["中国大陆"],
                        "genres": ["剧情"]}
            self.fail("unexpected URL: %s (attempts=%d)" % (url, attempts))

        cached_movie = {"douban_id": "101", "title": "旧详情标题",
                        "genres": ["剧情"], "regions": ["中国大陆"]}
        previous = {"details": {"101": cached_movie}}
        with patch.object(builder, "get", side_effect=fake_get), \
                patch.object(douban_catalog.time, "sleep"):
            fetched_details = {}
            home_sources = {}
            home = builder.collect_home(0, previous, details_out=fetched_details,
                                        source_out=home_sources)

        movie_calls = [(path, query) for path, query in calls
                       if path == "/j/search_subjects" and query.get("type") == ["movie"]]
        self.assertTrue(movie_calls)
        self.assertEqual({query["tag"][0] for _path, query in movie_calls},
                         {"热门", "最新", "豆瓣高分"})
        self.assertEqual({query["sort"][0] for _path, query in movie_calls}, {"recommend"})
        tv_home_calls = [(path, query) for path, query in calls
                         if path == "/j/search_subjects" and query.get("type") == ["tv"]
                         and query.get("tag") == ["热门"]]
        self.assertEqual({query["sort"][0] for _path, query in tv_home_calls},
                         {"recommend", "time", "rank"})
        self.assertEqual([[item["douban_id"] for item in section["items"]]
                          for section in home["anime"]["sections"]], [["301"]] * 3)
        self.assertFalse(any(path.startswith("/rexxar/api/v2/subject/101")
                             for path, _query in calls))
        self.assertFalse(any(path.endswith("/301") for path, _query in calls))
        self.assertNotIn("101", fetched_details)
        self.assertNotIn("301", fetched_details)  # 列表行已有产地/类型，避免为全量 3D 候选抓详情
        self.assertEqual(set(home_sources["movie"].values()), {"douban-home-api"})
        self.assertEqual(set(home_sources["tv"].values()), {"douban-home-api"})
        self.assertEqual(set(home_sources["anime"].values()), {"douban-explore"})

    def test_home_only_rejects_stale_movie_fallback(self):
        previous = {"home": {"movie": {"sections": [
            {"title": "最近热门", "items": [{"douban_id": "1", "title": "旧电影"}]}]}}}
        with patch.object(builder, "get", return_value=None), \
                patch.object(douban_catalog.time, "sleep"):
            with self.assertRaisesRegex(SystemExit, "拒绝使用旧栏"):
                builder.collect_home(0, previous, require_fresh_movie_tv=True)

    def test_anime_snapshot_index_fills_sparse_home_sections(self):
        titles = douban_catalog.CN3D_SERIES[:20]
        previous = {
            "home": {category: {"sections": [
                {"title": title, "items": [{"douban_id": "seed-0", "category": category,
                                             "title": titles[0], "regions": ["中国大陆"],
                                             "genres": ["动画"]}]}
                for title in douban_catalog.HOME_SECTION_NAMES]}
                for category, _label in builder.CATEGORIES},
            "index": [{"douban_id": "seed-%d" % i, "category": "anime",
                       "title": name + " 第1季", "regions": ["中国大陆"],
                       "genres": ["动画"], "year": "2026", "rating": 9 - i / 10}
                      for i, name in enumerate(titles)],
        }
        sources = {}
        with patch.object(builder, "get", return_value=None), \
                patch.object(douban_catalog.time, "sleep"):
            home = builder.collect_home(0, previous, source_out=sources)

        self.assertEqual([len(section["items"]) for section in home["anime"]["sections"]],
                         [20, 20, 20])
        for section in home["anime"]["sections"]:
            self.assertEqual(len({douban_catalog.cn3d_series_key(item)
                                  for item in section["items"]}), 20)
        self.assertEqual(set(sources["anime"].values()),
                         {"douban-explore+snapshot-index"})

    def test_full_refresh_marks_each_snapshot_fallback(self):
        previous = {"home": {
            category: {"sections": [{"title": title,
                                     "items": [{"douban_id": "1", "title": "旧条目"}]}
                                    for title in douban_catalog.HOME_SECTION_NAMES]}
            for category, _tag in builder.CATEGORIES
        }}
        sources = {}
        with patch.object(builder, "get", return_value=None), \
                patch.object(douban_catalog.time, "sleep"):
            builder.collect_home(0, previous, source_out=sources)
        self.assertEqual(set(sources["movie"].values()), {"snapshot-fallback"})
        self.assertEqual(set(sources["tv"].values()), {"snapshot-fallback"})
        self.assertEqual(set(sources["anime"].values()), {"snapshot-fallback"})

    def test_apply_details_keeps_fresh_list_metadata_and_fills_missing_fields(self):
        fresh_poster = "https://img.doubanio.com/view/photo/s_ratio_poster/public/p-new.jpg"
        fresh = {"douban_id": "7", "title": "最新首页标题", "year": "2026",
                 "rating": 8.7, "rating_count": 0, "poster_url": fresh_poster,
                 "hero_url": "https://img.doubanio.com/view/photo/large/public/p-new.jpg"}
        detail = {"douban_id": "7", "title": "旧详情标题", "year": "2024",
                  "rating": 7.9, "rating_count": 120, "poster_url": "old-poster",
                  "hero_url": "old-hero", "genres": ["剧情"], "summary": "详情简介"}

        result = builder.apply_details([fresh], {"7": detail})[0]

        self.assertEqual(result["title"], "最新首页标题")
        self.assertEqual(result["year"], "2026")
        self.assertEqual(result["rating"], 8.7)
        self.assertEqual(result["rating_count"], 120)  # Explore 行不提供有效评分人数
        self.assertEqual(result["poster_url"], fresh_poster)
        self.assertIn("p-new.jpg", result["hero_url"])
        self.assertEqual(result["genres"], ["剧情"])
        self.assertEqual(result["summary"], "详情简介")

    def test_home_only_preserves_the_existing_search_pool_and_details(self):
        old_home = {"movie": {"category": "movie", "sections": []}}
        new_home = {"movie": {"category": "movie", "sections": [
            {"title": "最近热门", "items": [{"douban_id": "9", "title": "新首页条目"}]}]}}
        previous = {"schema": 1, "source": "douban", "generatedAt": "2026-01-01T00:00:00+00:00",
                    "home": old_home, "index": [{"douban_id": "1", "title": "历史条目"}],
                    "details": {"1": {"douban_id": "1", "title": "历史详情"}},
                    "stats": {"home": {}, "index": 1, "details": 1}}
        new_detail = {"douban_id": "9", "title": "详情标题可能滞后", "summary": "新详情简介"}

        def collect_home(_sleep, _previous, details_out=None, source_out=None,
                         require_fresh_movie_tv=False):
            details_out["9"] = new_detail
            source_out.update({"movie": {"最近热门": "douban-home-api"}})
            return new_home

        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "snapshot.json"
            with patch.object(builder, "OUT", output), \
                    patch.object(builder, "collect_home", side_effect=collect_home):
                result = builder.refresh_home_only(previous, 0)
                written = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result["index"][0], previous["index"][0])
        self.assertEqual([item["douban_id"] for item in result["index"]], ["1", "9"])
        self.assertEqual(result["index"][1]["title"], "新首页条目")
        self.assertEqual(result["index"][1]["summary"], "新详情简介")
        self.assertEqual(result["details"]["1"], previous["details"]["1"])
        self.assertEqual(result["details"]["9"], new_detail)
        self.assertEqual(written["index"][0], previous["index"][0])
        self.assertEqual([item["douban_id"] for item in written["index"]], ["1", "9"])
        self.assertEqual(written["index"][1]["summary"], "新详情简介")
        self.assertEqual(written["details"]["1"], previous["details"]["1"])
        self.assertEqual(written["details"]["9"], new_detail)
        self.assertEqual(written["generatedAt"], previous["generatedAt"])
        self.assertNotEqual(written["homeGeneratedAt"], None)
        self.assertEqual(written["home"], new_home)
        self.assertEqual(written["homeSource"]["movie"]["最近热门"], "douban-home-api")
        self.assertEqual(written["stats"]["index"], 2)
        self.assertEqual(written["stats"]["details"], 2)


if __name__ == "__main__":
    unittest.main()
