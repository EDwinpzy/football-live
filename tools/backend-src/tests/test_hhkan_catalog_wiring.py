"""网页版影视页「好好看」补位链路的静态契约。

背景（2026-09-30）：用户要求"把好好看片源加上"。第一次实现把它做成了
第六个分类 chip（hhkan:home），用户明确否定——chips 是内容分类
（电影/电视剧/动漫/综艺/短剧），好好看是片源，不该混在同一排。

正确形态（app.js 文件头自己写的架构）：
    影视目录 /hhkan/home|channel|...|channel-sections（好好看唯一卡片源）
所以好好看是这五个分类的**片源后盾**：豆瓣某栏空/降级时补位，
chips 仍然只有五类。

本测试锁住这个形态，避免以后又被改回 chip。
"""

import pathlib
import re
import unittest


APP_JS = pathlib.Path(__file__).resolve().parents[1] / "www" / "app.js"


class HhkanCatalogWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = APP_JS.read_text(encoding="utf-8")

    def test_categories_stay_five_plus_all(self):
        self.assertIn(
            '[["movie", "电影"], ["tv", "电视剧"], ["anime", "动漫"], '
            '["variety", "综艺"], ["short", "短剧"]]',
            self.js,
            "分类定义被改动：好好看不应作为第六个分类混进 VOD_CATEGORIES",
        )

    def test_no_hhkan_chip_is_pushed_into_categories(self):
        self.assertNotRegex(
            self.js, r'cats\.push\(\{\s*id:\s*"hhkan',
            "好好看被当成分类 chip 加进了 chips——它应该是片源，不是分类",
        )
        self.assertNotIn(
            'sel.id === "hhkan:home"',
            self.js,
            "renderVodLib 里仍有 hhkan:home 分类分支，应删除",
        )

    def test_hhkan_channel_mapping_covers_all_five_categories(self):
        m = re.search(r"const HHKAN_CHANNELS = \{([^}]*)\}", self.js)
        self.assertIsNotNone(m, "缺少 HHKAN_CHANNELS 映射")
        pairs = dict(re.findall(r"(\w+):\s*(\d+)", m.group(1)))
        self.assertEqual(
            pairs,
            {"movie": "1", "tv": "2", "anime": "3",
             "variety": "4", "short": "6"},
            "好好看频道号映射与 hhkan_snapshot.json 的 channels 键不一致",
        )

    def test_fetch_lib_blocks_falls_back_to_hhkan(self):
        self.assertIn("HHKAN_CHANNELS[cid]", self.js)
        self.assertIn('"/hhkan/channel-sections/" + hkCid', self.js)
        self.assertIn('"/hhkan/channel/" + hkCid', self.js)
        self.assertIn('"/hhkan/show/" + hkCid', self.js)

    def test_hhkan_items_get_prefixed_ids(self):
        self.assertRegex(self.js, r'"hhkan:"\s*\+\s*id')
        self.assertIn("function normalizeHhkanItem", self.js)

    def test_normalizer_takes_category_not_map_index(self):
        m = re.search(r"function normalizeHhkanItem\(it,\s*(\w+)\)", self.js)
        self.assertIsNotNone(m, "normalizeHhkanItem 应显式接收分类参数")
        self.assertNotRegex(
            self.js, r"\.map\(normalizeHhkanItem\)",
            "normalizeHhkanItem 被直接传给 map()，index 会污染 categoryId",
        )


if __name__ == "__main__":
    unittest.main()
