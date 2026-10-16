import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
HARNESS = ROOT / "tools" / "backend-src" / "tests" / "web_player_seek_harness.mjs"


class WebPlayerSeekTest(unittest.TestCase):
    def test_seek_gesture_and_recovery_runtime_harness(self):
        result = subprocess.run(
            ["node", str(HARNESS)],
            cwd=ROOT / "tools" / "backend-src",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("All player-seek harness checks passed.", result.stdout)


if __name__ == "__main__":
    unittest.main()
