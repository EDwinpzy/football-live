"""v1.32 _football_logo_key「CJK+FC 连写」回归契约。

根因（2026-09-19 排查老板报障时发现）：旧实现用 \\b 词界剥离 fc/cf/ac/sc 通用
后缀，但 \\w 含 CJK——「奥克兰fc」剥不掉 fc，[^a-z0-9] 清洗后残留裸 "fc" 当
查询键，前缀匹配命中一切 FC* 俱乐部，最短键 FCV Dender EH（比利时）恒胜：
FC东京/安道尔FC/巴黎FC/邓迪FC/奥克兰FC 五队错挂同一枚错队徽。
"""
import pathlib
import sys
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class FootballLogoKeyTest(unittest.TestCase):
    def test_cjk_glued_fc_suffix_fully_dissolves(self):
        # CJK 与 FC 连写：整名归一化后必须为空（不拿裸 "fc" 去撞索引）
        for name in ("奥克兰FC", "FC东京", "巴黎FC", "安道尔FC", "邓迪FC", "FC首尔", "光州FC"):
            self.assertEqual(proxy._football_logo_key(name), "", name)

    def test_latin_names_keep_working(self):
        self.assertEqual(proxy._football_logo_key("FC Tokyo"), "tokyo")
        self.assertEqual(proxy._football_logo_key("Liverpool FC"), "liverpool")
        self.assertEqual(proxy._football_logo_key("AC Milan"), "milan")
        self.assertEqual(proxy._football_logo_key("SC Cambuur"), "cambuur")
        self.assertEqual(proxy._football_logo_key("Auckland FC"), "auckland")

    def test_club_acronym_prefix_preserved(self):
        # FCV/FCC 这类「FC+字母」俱乐部缩写不是通用后缀，必须保留可匹配
        self.assertEqual(proxy._football_logo_key("FCV Dender EH"), "fcvdendereh")

    def test_unknown_cjk_fc_team_does_not_match_any_club(self):
        # 端到端守卫：未收录的 CJK+FC 队不得再落到任何具体俱乐部（尤其 Dender）
        old_index = proxy.FOOTBALL_LOGOS_INDEX
        proxy.FOOTBALL_LOGOS_INDEX = [
            {"name": "FCV Dender EH", "categoryId": "belgium", "id": "fcv-dender-eh", "h": "abc"},
            {"name": "FC Tokyo", "categoryId": "japan", "id": "fc-tokyo", "h": "def"},
        ]
        try:
            self.assertIsNone(proxy._football_logos_search_team("虚构FC"))
            # 有 TEAM_EN 引导的正常队不受影响
            proxy.team_backdrop.TEAM_EN["测试东京"] = "FC_Tokyo"
            url = proxy._football_logos_search_team("测试东京")
            self.assertIsNotNone(url)
            self.assertIn("fc-tokyo", url)
        finally:
            proxy.FOOTBALL_LOGOS_INDEX = old_index
            proxy.team_backdrop.TEAM_EN.pop("测试东京", None)


if __name__ == "__main__":
    unittest.main()
