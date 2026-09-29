"""起播线路必须按实测可用性排序，而不是索引里的原始顺序。

问题现场（2026-09-29 实测）：《肖申克的救赎》详情返回 12 条线路，
App 从 line 0 逐条试到 line 12，每条精确等满 5s 首帧看门狗才换线，
点播放到出画耗时 78.8 秒。而静态索引里这条片子只有 1 条线路（猫眼），
其余 11 条来自实时匹配，顺序完全是匹配出来的先后，没有可用性权重。

LineHistory 早就在 record(线路, 成功/失败)，也有 success_rate()，
但全仓库没有任何地方读它来排序——数据在攒，排序没用上。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vod_sources  # noqa: E402


def _line(line_id, name="line"):
    return {"id": line_id, "name": name, "episodes": [
        {"index": 0, "name": "正片", "url": "https://example.com/%s.m3u8" % line_id}]}


class LineHealthRankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = vod_sources.LineHistory(Path(self.tmp.name) / "h.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_rate_defaults_to_neutral_when_unknown(self):
        self.assertEqual(self.history.success_rate("never-seen"), 0.5)

    def test_success_rate_reflects_recorded_outcomes(self):
        for _ in range(3):
            self.history.record("good", True)
        for _ in range(3):
            self.history.record("bad", False)
        self.assertGreater(self.history.success_rate("good"), 0.9)
        self.assertLess(self.history.success_rate("bad"), 0.1)

    def test_rank_puts_proven_lines_first(self):
        lines = [_line("bad"), _line("mid"), _line("good")]
        for _ in range(4):
            self.history.record("bad", False)
        self.history.record("mid", True)
        self.history.record("mid", False)
        for _ in range(4):
            self.history.record("good", True)

        ranked = vod_sources.rank_lines_by_health(lines, self.history)
        self.assertEqual(
            [line["id"] for line in ranked], ["good", "mid", "bad"],
            "起播顺序必须按实测成功率排，而不是索引原始顺序")

    def test_rank_is_stable_for_equally_healthy_lines(self):
        """成功率相同的线路保持原有相对顺序，避免每次刷新顺序抖动。"""
        lines = [_line("a"), _line("b"), _line("c")]
        ranked = vod_sources.rank_lines_by_health(lines, self.history)
        self.assertEqual([line["id"] for line in ranked], ["a", "b", "c"])

    def test_rank_never_reorders_when_history_is_absent(self):
        lines = [_line("x"), _line("y")]
        self.assertEqual(
            [line["id"] for line in vod_sources.rank_lines_by_health(lines, None)],
            ["x", "y"])

    def test_recent_failures_demote_a_previously_good_line(self):
        """线路会坏。历史成功率高但最近连续失败时，必须被降到后面。"""
        for _ in range(10):
            self.history.record("flappy", True)
        for _ in range(3):
            self.history.record("flappy", False)
        lines = [_line("flappy"), _line("fresh")]
        ranked = vod_sources.rank_lines_by_health(lines, self.history)
        self.assertEqual(
            [line["id"] for line in ranked], ["fresh", "flappy"],
            "最近连续失败的线路应被降权，哪怕历史成功率很高")

    def test_rank_annotates_lines_for_debugging(self):
        lines = [_line("a")]
        self.history.record("a", True)
        ranked = vod_sources.rank_lines_by_health(lines, self.history)
        self.assertIn("health_score", ranked[0])
        self.assertIn("health_samples", ranked[0])

    def test_rank_does_not_mutate_the_input_list(self):
        lines = [_line("bad"), _line("good")]
        self.history.record("bad", False)
        self.history.record("good", True)
        before = [line["id"] for line in lines]
        vod_sources.rank_lines_by_health(lines, self.history)
        self.assertEqual([line["id"] for line in lines], before)


if __name__ == "__main__":
    unittest.main()
