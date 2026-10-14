"""静态队徽表覆盖后端映射，并保持 TV / mobile 两端一致。"""
import ast
import json
import pathlib
import sys
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


ASSET_PATHS = (
    ROOT / "android/app/src/main/assets/team_icons.json",
    ROOT / "internal/mobile/app/src/main/assets/team_icons.json",
)
AMBIGUOUS_OR_UNMAPPED_ALIASES = {"巴黎", "红牛", "恒大"}


def _known_map():
    source = (ROOT / "tools/backend-src/proxy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    resolver = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_team_icon"
    )
    assignment = next(
        node for node in resolver.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "KNOWN_MAP" for target in node.targets)
    )
    return ast.literal_eval(assignment.value)


class TeamIconStaticCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = [json.loads(path.read_text(encoding="utf-8")) for path in ASSET_PATHS]
        cls.known_map = _known_map()

    def test_tv_and_mobile_assets_are_identical(self):
        self.assertEqual(self.assets[0], self.assets[1])

    def test_bundle_covers_every_backend_static_mapping(self):
        names = set(proxy.FB_CREST_IDS) | set(self.known_map)
        for icons in self.assets:
            self.assertFalse(names - icons.keys(), sorted(names - icons.keys()))

    def test_bundle_covers_unambiguous_aliases_and_reuses_canonical_crests(self):
        for icons in self.assets:
            for alias, canonical in proxy.TEAM_ALIAS.items():
                if alias in AMBIGUOUS_OR_UNMAPPED_ALIASES or canonical not in icons:
                    continue
                self.assertIn(alias, icons, alias)
                self.assertEqual(icons[canonical], icons[alias], alias)

    def test_new_mappings_point_to_expected_crests(self):
        expected = {
            "马竞": "https://crests.football-data.org/78.svg",
            "韩职明星队": "https://media.api-sports.io/football/teams/5600.png",
            "巴萨": "https://crests.football-data.org/81.svg",
            "巴萨罗那": "https://crests.football-data.org/81.svg",
            "皇马": "https://crests.football-data.org/86.svg",
            "国米": "https://crests.football-data.org/108.svg",
            "多特": "https://crests.football-data.org/4.svg",
            "申花": "https://media.api-sports.io/football/teams/833.png",
            "上港": "https://media.api-sports.io/football/teams/836.png",
            "国安": "https://media.api-sports.io/football/teams/830.png",
            "泰山": "https://media.api-sports.io/football/teams/844.png",
            "纽卡": "https://crests.football-data.org/67.svg",
            "毕尔巴鄂": "https://crests.football-data.org/80.svg",
            "皇社": "https://crests.football-data.org/77.svg",
            "拉科": "https://media.api-sports.io/football/teams/546.png",
            "莱斯特": "https://crests.football-data.org/338.svg",
            "柏林联": "https://r2.thesportsdb.com/images/media/team/badge/q0o5001599679795.png",
            "药厂": "https://crests.football-data.org/3.svg",
            "大巴黎": "https://crests.football-data.org/524.svg",
            "阿尔萨德": "https://r2.thesportsdb.com/images/media/team/badge/908a011774579337.png",
            "席贝": "https://r2.thesportsdb.com/images/media/team/badge/xlskhy1693027251.png",
            "侯赛因": "https://r2.thesportsdb.com/images/media/team/badge/ijbwwm1753204799.png",
            "沙巴巴库": "https://r2.thesportsdb.com/images/media/team/badge/o2ttq41614284640.png",
            "布拉格斯拉维亚": "https://r2.thesportsdb.com/images/media/team/badge/l7kl4n1759252139.png",
            "LASK": "https://r2.thesportsdb.com/images/media/team/badge/oox26l1683556395.png",
            "林茨": "https://r2.thesportsdb.com/images/media/team/badge/oox26l1683556395.png",
            "顿涅茨克矿工": "https://r2.thesportsdb.com/images/media/team/badge/sqrxsr1421791799.png",
            "AEK雅典": "https://r2.thesportsdb.com/images/media/team/badge/4nogst1602773624.png",
            "雅典AEK": "https://r2.thesportsdb.com/images/media/team/badge/4nogst1602773624.png",
            "萨巴赫": "https://r2.thesportsdb.com/images/media/team/badge/o2ttq41614284640.png",
            "博多格林特": "https://media.api-sports.io/football/teams/327.png",
        }
        for icons in self.assets:
            for name, url in expected.items():
                self.assertEqual(url, icons.get(name), name)

    def test_missing_live_fixture_teams_resolve_without_network_search(self):
        expected = {
            "阿尔萨德": "https://r2.thesportsdb.com/images/media/team/badge/908a011774579337.png",
            "席贝": "https://r2.thesportsdb.com/images/media/team/badge/xlskhy1693027251.png",
            "侯赛因": "https://r2.thesportsdb.com/images/media/team/badge/ijbwwm1753204799.png",
            "沙巴巴库": "https://r2.thesportsdb.com/images/media/team/badge/o2ttq41614284640.png",
            "布拉格斯拉维亚": "https://r2.thesportsdb.com/images/media/team/badge/l7kl4n1759252139.png",
            "萨巴赫": "https://r2.thesportsdb.com/images/media/team/badge/o2ttq41614284640.png",
            "林茨": "https://r2.thesportsdb.com/images/media/team/badge/oox26l1683556395.png",
            "顿涅茨克矿工": "https://r2.thesportsdb.com/images/media/team/badge/sqrxsr1421791799.png",
            "雅典AEK": "https://r2.thesportsdb.com/images/media/team/badge/4nogst1602773624.png",
            "博多格林特": "https://media.api-sports.io/football/teams/327.png",
        }
        for name, url in expected.items():
            with self.subTest(team=name):
                self.assertEqual(url, proxy.resolve_team_icon(name)["url"])

    def test_ambiguous_or_unmapped_aliases_are_not_guessed(self):
        for icons in self.assets:
            for name in AMBIGUOUS_OR_UNMAPPED_ALIASES:
                self.assertNotIn(name, icons, name)

    def test_both_image_loaders_support_png_and_svg_sources(self):
        for variant in ("android", "internal/mobile"):
            app_source = (ROOT / variant / "app/src/main/java/com/qiubo/optimaltv/App.kt").read_text(encoding="utf-8")
            gradle_source = (ROOT / variant / "app/build.gradle").read_text(encoding="utf-8")
            self.assertIn("coil-svg", gradle_source, variant)
            self.assertIn("SvgDecoder.Factory()", app_source, variant)
            self.assertIn("coil-compose", gradle_source, variant)


if __name__ == "__main__":
    unittest.main()
