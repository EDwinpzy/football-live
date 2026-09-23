import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ReleaseSigningConfigTests(unittest.TestCase):
    def test_release_signing_passwords_are_read_from_local_properties(self):
        for path in (ROOT / "android" / "app" / "build.gradle",
                     ROOT / "internal" / "mobile" / "app" / "build.gradle"):
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertTrue("OTV_RELEASE_STORE_PASSWORD" in source,
                                "release store password must come from local Gradle settings")
                self.assertTrue("OTV_RELEASE_KEY_PASSWORD" in source,
                                "release key password must come from local Gradle settings")
                self.assertIsNone(re.search(r"(?m)^\s*storePassword\s+['\"][^'\"]+['\"]\s*$", source),
                                  "release store password must not be hardcoded")
                self.assertIsNone(re.search(r"(?m)^\s*keyPassword\s+['\"][^'\"]+['\"]\s*$", source),
                                  "release key password must not be hardcoded")

    def test_apk_archives_follow_the_active_worktree(self):
        tv = (ROOT / "android" / "app" / "build.gradle").read_text(encoding="utf-8")
        mobile = (ROOT / "internal" / "mobile" / "app" / "build.gradle").read_text(encoding="utf-8")
        self.assertTrue('rootProject.file("../apk/TV版")' in tv,
                        "TV release output must stay inside the active checkout")
        self.assertTrue('rootProject.file("../../apk/移动版")' in mobile,
                        "mobile release output must stay inside the active checkout")
        self.assertNotIn("D:/MyProjects/My TV", tv + mobile,
                         "release builds must not write into the main worktree")

    def test_gradle_build_directories_follow_the_active_worktree(self):
        for path in (ROOT / "android" / "build.gradle",
                     ROOT / "internal" / "mobile" / "build.gradle"):
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertIn('buildDir = new File(rootDir, "build/', source,
                              "Gradle build directories must follow the active checkout")
                self.assertNotIn("D:/MyProjects", source,
                                 "shared build directories corrupt KSP state across worktrees")


if __name__ == "__main__":
    unittest.main()
