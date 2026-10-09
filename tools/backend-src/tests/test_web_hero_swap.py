"""Behavioral async verification for the web hero image swap."""

import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
HARNESS = ROOT / "tools/backend-src/tests/web_hero_swap_harness.mjs"


class WebHeroSwapTest(unittest.TestCase):
    def test_load_decode_latest_selection_and_failure_flow(self):
        result = subprocess.run(
            ["node", str(HARNESS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("hero swap async harness: 7 scenarios passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
