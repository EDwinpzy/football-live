"""队标映射回归：热刺必须使用 Tottenham 的 football-data crest。"""
import json
import pathlib
import sys
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class TottenhamCrestContractTest(unittest.TestCase):
    def test_tottenham_aliases_use_tottenham_crest_id(self):
        self.assertEqual(73, proxy.FB_CREST_IDS["热刺"])
        self.assertEqual(73, proxy.FB_CREST_IDS["托特纳姆热刺"])

    def test_resolver_returns_tottenham_crest_for_aliases(self):
        for name in ("热刺", "托特纳姆热刺"):
            result = proxy.resolve_team_icon(name)
            self.assertEqual("fbrest_svg", result["source"])
            self.assertEqual(73, result["id"])
            self.assertEqual("https://crests.football-data.org/73.svg", result["url"])

    def test_bundled_android_maps_both_aliases_to_same_crest(self):
        for relative in (
            "android/app/src/main/assets/team_icons.json",
            "internal/mobile/app/src/main/assets/team_icons.json",
        ):
            icons = json.loads((BACKEND.parents[1] / relative).read_text(encoding="utf-8"))
            self.assertEqual("https://crests.football-data.org/73.svg", icons["热刺"])
            self.assertEqual("https://crests.football-data.org/73.svg", icons["托特纳姆热刺"])


if __name__ == "__main__":
    unittest.main()
