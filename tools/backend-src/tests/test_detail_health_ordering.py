"""detail() 返回给 App 的线路必须已按健康度排序。

这是 2026-09-29 实测 78.8s 起播的修复点：rank_lines_by_health 本身有
单元测试，但真正的收益取决于 detail() 有没有调用它——中间任何一层漏接，
排序就等于没做。这里守住接线。
"""

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vod_api  # noqa: E402


class DetailHealthOrderingTests(unittest.TestCase):
    def test_detail_calls_the_health_ranker(self):
        source = inspect.getsource(vod_api.VodService.detail)
        self.assertIn(
            "rank_lines_by_health", source,
            "detail() 必须调用 rank_lines_by_health，否则起播线路仍是原始顺序")

    def test_ranking_happens_before_lines_are_published(self):
        """排序必须早于 subject["sources"] 的赋值，否则 App 拿到的还是未排序列表。"""
        source = inspect.getsource(vod_api.VodService.detail)
        rank_at = source.index("rank_lines_by_health")
        publish_at = source.index('subject["sources"] = sources')
        self.assertLess(
            rank_at, publish_at,
            "排序必须在写入 subject sources 之前，否则对 App 无效")

    def test_resolved_lines_keeps_the_ranked_order(self):
        """_resolved_lines 是 /vod/play 查表用的，顺序也必须与 App 看到的一致。"""
        source = inspect.getsource(vod_api.VodService.detail)
        rank_at = source.index("rank_lines_by_health")
        cache_at = source.index("self._resolved_lines[")
        self.assertLess(rank_at, cache_at)


if __name__ == "__main__":
    unittest.main()
