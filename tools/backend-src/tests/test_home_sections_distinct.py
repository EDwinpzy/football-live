"""影视首页三栏切分回归。

背景（2026-09-30 用户反馈"最近热门和最新上线内容都是一样的"）：
_mcms_channel_sections 原本写 `updates = items[12:24] or recent`。
上游不足 24 条时 items[12:24] 为空，`or recent` 会把同一批数据同时
铺到「最新上线」和「最近更新」两栏，字面重复。

三栏各 12 个格子，填满需要 36 条，上游给不满时必然有空栏或重合——
所以这里保证的是「每栏内部不重复」+「条目充足时跨栏互斥」，
而不是一个上游根本满足不了的强不变量。
"""

import pathlib
import sys
import unittest
from unittest import mock


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import proxy  # noqa: E402


EXPECTED_NAMES = ["最近热门", "最新上线", "最近更新"]


class HomeSectionsDistinctTest(unittest.TestCase):
    def _sections(self, count, with_score=True):
        items = []
        for i in range(count):
            item = {"name": f"影片{i:02d}", "vod_id": 1000 + i}
            if with_score:
                item["vod_score"] = 5 + (i % 5)
            items.append(item)
        with mock.patch.object(proxy, "_mcms_paged", return_value=(items, False)):
            return proxy._mcms_channel_sections(1)

    @staticmethod
    def _ids(sections):
        return {s["name"]: [i["vod_id"] for i in s["items"]] for s in sections}

    def test_always_returns_three_named_sections(self):
        for count in (1, 5, 12, 13, 24, 40):
            with self.subTest(count=count):
                sections = self._sections(count)
                self.assertEqual([s["name"] for s in sections], EXPECTED_NAMES)

    def test_no_section_contains_duplicates(self):
        for count in (1, 5, 12, 13, 24, 40, 60):
            with self.subTest(count=count):
                for name, ids in self._ids(self._sections(count)).items():
                    self.assertEqual(len(ids), len(set(ids)),
                                     f"count={count} 时「{name}」内部有重复")

    def test_updates_is_not_a_literal_copy_of_recent(self):
        # 这条是本次修复的核心断言：即使数据少，也不能拿 recent 冒充 updates。
        for count in (2, 5, 12, 13, 20, 40):
            with self.subTest(count=count):
                ids = self._ids(self._sections(count))
                self.assertNotEqual(
                    ids["最近更新"], ids["最新上线"],
                    f"count={count} 时「最近更新」与「最新上线」字面相同",
                )

    def test_hot_row_stays_populated(self):
        # 「最近热门」按评分取头部，与「最新上线」有交集是语义正常的
        # （同一批高分片既热门又新）。这里只保证热门栏不会被去重逻辑清空。
        for count in (13, 24, 40, 60):
            with self.subTest(count=count):
                ids = self._ids(self._sections(count))
                self.assertTrue(ids["最近热门"], "最近热门 不应为空")

    def test_empty_input_yields_no_sections(self):
        with mock.patch.object(proxy, "_mcms_paged", return_value=([], False)):
            self.assertEqual(proxy._mcms_channel_sections(1), [])

    def test_unknown_channel_is_skipped(self):
        self.assertEqual(proxy._mcms_channel_sections(999), [])


if __name__ == "__main__":
    unittest.main()
