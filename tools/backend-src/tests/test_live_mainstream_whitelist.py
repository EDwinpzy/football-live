"""足球「今日/明日比赛」主流白名单契约。

2026-09-15 实测事故：当天 4 场全是亚冠（2 场正在直播、含北京国安），但收紧白名单只认
五大联赛/欧战/国家队大赛，把亚冠整类滤掉 → 「今日比赛」显示「暂无比赛」。这里锁住
亚洲俱乐部赛事（亚冠）与亚运足球必须保留，同时确认篮球类仍然剔除。
"""
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class LiveMainstreamWhitelistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "android/app/src/main/java/com/qiubo/optimaltv/data/repo/LiveRepository.kt").read_text(encoding="utf-8")

    def test_asian_club_cup_kept(self):
        match = re.search(r'private val ASIA_CLUB_CUP_RE = Regex\("([^"]+)"\)', self.src)
        self.assertIsNotNone(match, "缺少亚冠白名单常量")
        pat = re.compile(match.group(1))
        for league in ("亚冠精英赛", "亚冠联2"):
            self.assertTrue(pat.search(league), f"{league} 应被保留")
        self.assertIn("ASIA_CLUB_CUP_RE.containsMatchIn(L)", self.src)

    def test_asian_games_football_kept(self):
        match = re.search(r'private val NAT_COMP_RE = Regex\("([^"]+)"\)', self.src)
        self.assertIsNotNone(match)
        pat = re.compile(match.group(1))
        for league in ("亚运男足", "亚运会", "亚洲杯"):
            self.assertTrue(pat.search(league), f"{league} 应被保留")

    def test_non_football_still_filtered(self):
        match = re.search(r'private val NON_FOOTBALL_RE = Regex\("([^"]+)"\)', self.src)
        self.assertIsNotNone(match)
        pat = re.compile(match.group(1))
        for league in ("亚运男篮", "NBA", "CBA", "菲NCAA"):
            self.assertTrue(pat.search(league), f"{league} 应被剔除")

    def test_current_live_football_is_not_hidden_by_league_whitelist(self):
        self.assertIn("val liveFootballIds = list.asSequence()", self.src)
        self.assertIn(".filter { it.isLive }", self.src)
        self.assertIn("it.matchId in liveFootballIds", self.src)

    def test_mobile_tree_in_sync(self):
        mb = (ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/LiveRepository.kt").read_text(encoding="utf-8")
        self.assertEqual(self.src, mb, "两工程不一致，请跑 tools/sync_shared.py")


if __name__ == "__main__":
    unittest.main()
