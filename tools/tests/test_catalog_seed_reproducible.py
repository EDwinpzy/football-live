import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TARGETS = [
    ROOT / "android" / "app" / "src" / "main" / "assets" / "catalog_seed.json",
    ROOT / "internal" / "mobile" / "app" / "src" / "main" / "assets" / "catalog_seed.json",
]


class CatalogSeedReproducibleTests(unittest.TestCase):
    """目录种子必须是可复现构建。

    App 读的是 assets/catalog_seed.json（MetadataCatalogStore.SEED_ASSET）。
    如果每次跑 build_catalog_seed.py 都刷新 ts，即使 270 条目录一字未改，
    产物哈希也会变，发布后自检里的"APK 内嵌种子 == 仓库种子"就永远失败，
    只能靠人工解释差异。ts 只表示种子新鲜度，不代表内容版本。
    """

    def test_rerunning_the_builder_does_not_change_the_bytes(self):
        before = [p.read_bytes() for p in TARGETS]
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_catalog_seed.py")],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        after = [p.read_bytes() for p in TARGETS]
        for path, old, new in zip(TARGETS, before, after):
            self.assertEqual(old, new, f"{path.name} 在内容未变时不该被改写")

    def test_both_targets_stay_byte_identical(self):
        self.assertEqual(
            TARGETS[0].read_bytes(), TARGETS[1].read_bytes(),
            "TV 与移动端的目录种子必须逐字节一致")

    def test_seed_shape_is_unchanged(self):
        payload = json.loads(TARGETS[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["v"], 3)
        self.assertIsInstance(payload["ts"], int)
        self.assertEqual(len(payload["categories"]), 5)
        self.assertEqual(len(payload["items"]), 270)


if __name__ == "__main__":
    unittest.main()
