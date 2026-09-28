"""影视 tab 三栏契约：分类 id 必须是「豆瓣口径」解析，否则片库恒空。

2026-09-15 线上实测事故：目录切豆瓣后分类 id 变成 `douban:movie` 形态，但首页仍按旧的
`removePrefix("hhkan:").toIntOrNull()` 解析 → 恒为 null → 三栏恒空，影视 tab 一直显示
「暂无内容」（数据其实已由 libBlocks 拉到 libBlocksFlow）。这里锁住「统一走
VodRepository.categoryCid」的契约。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
ANDROID = ROOT / "android/app/src/main/java/com/qiubo/optimaltv"
MOBILE = ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv"


class VodCategoryCidContractTest(unittest.TestCase):
    def test_repository_exposes_shared_resolver(self):
        src = (ANDROID / "data/repo/VodRepository.kt").read_text(encoding="utf-8")
        self.assertIn("fun categoryCid(categoryId: String): Int?", src)
        # 必须同时认豆瓣口径与旧的 hhkan:<数字> 形态
        self.assertIn('removePrefix("douban:")', src)
        self.assertIn('removePrefix("hhkan:")', src)

    def test_home_screen_uses_shared_resolver(self):
        src = (ANDROID / "ui/home/HomeScreen.kt").read_text(encoding="utf-8")
        self.assertIn("VodRepository.categoryCid(selCatId)", src)
        self.assertNotIn('selCatId.removePrefix("hhkan:").toIntOrNull()', src)

    def test_cover_preload_uses_shared_resolver(self):
        src = (ANDROID / "App.kt").read_text(encoding="utf-8")
        self.assertIn("VodRepository.categoryCid(cid)", src)
        self.assertNotIn('cid.removePrefix("hhkan:").toIntOrNull()', src)

    def test_mobile_tree_matches_tv_tree(self):
        """The category contract is shared; device-specific resolver code can differ."""
        tv_app = (ANDROID / "App.kt").read_text(encoding="utf-8")
        mb_app = (MOBILE / "App.kt").read_text(encoding="utf-8")
        self.assertEqual(tv_app, mb_app)
        marker = "fun categoryCid(categoryId: String): Int? {"
        bodies = []
        for root in (ANDROID, MOBILE):
            source = (root / "data/repo/VodRepository.kt").read_text(encoding="utf-8")
            bodies.append(source.split(marker, 1)[1].split("}", 1)[0])
        self.assertEqual(*bodies)

    def test_mobile_home_screen_uses_shared_resolver(self):
        src = (MOBILE / "ui/home/HomeScreen.kt").read_text(encoding="utf-8")
        self.assertIn("VodRepository.categoryCid(selCatId)", src)
        self.assertNotIn('selCatId.removePrefix("hhkan:").toIntOrNull()', src)


if __name__ == "__main__":
    unittest.main()
