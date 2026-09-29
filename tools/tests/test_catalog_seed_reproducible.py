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
        # 条目数**不做硬编码**：种子由 build_catalog_seed.py 从豆瓣快照生成，而
        # 快照每天都在滚动（榜单换新），实测 606 -> 605、270 -> 268 都属正常波动。
        # 原先写死 270，于是每次 `release_all.py` 刷快照后这条都会红；而它想守的
        # 「规模没有塌掉」其实没被守住——真塌了（比如只剩 3 条）也一样红。
        # 改成断言「规模合理」，既守得住退化，也不误报滚动波动。
        count = len(payload["items"])
        self.assertGreaterEqual(count, 200, "种子条目过少（%d），目录可能抓取失败" % count)
        self.assertLessEqual(count, 400, "种子条目异常膨胀（%d）" % count)

    def test_every_seed_item_is_structurally_complete(self):
        """规模之外还要守结构：每条都得有 id/标题/分类/海报，否则首装卡片是空的。"""
        payload = json.loads(TARGETS[0].read_text(encoding="utf-8"))
        items = payload["items"]
        known = {c["id"] for c in payload["categories"]}

        self.assertTrue(items, "种子不能为空")
        for item in items:
            for field in ("id", "t", "c", "p"):
                self.assertTrue(
                    item.get(field), "条目缺 %s：%r" % (field, item.get("id"))
                )
            self.assertIn(
                item["c"], known,
                "条目分类 %r 不在 categories 里：%r" % (item["c"], item.get("id")),
            )

    def test_seed_covers_every_category(self):
        """5 个分类都要有内容，否则首装某一栏是空的。"""
        payload = json.loads(TARGETS[0].read_text(encoding="utf-8"))
        seen = {item["c"] for item in payload["items"]}
        for category in payload["categories"]:
            self.assertIn(
                category["id"], seen,
                "分类 %s（%s）在种子里没有条目" % (category["id"], category.get("name")),
            )


if __name__ == "__main__":
    unittest.main()
