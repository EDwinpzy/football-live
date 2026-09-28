import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
APP = ROOT / "tools" / "backend-src" / "www" / "app.js"


class WebDoubanContractTest(unittest.TestCase):
    def test_web_uses_vod_api_and_douban_filters(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn('"/vod/home?category="', app)
        self.assertIn('"评分"', app)
        self.assertIn('[["hot", "热门"], ["new", "最新上映"], ["rating", "豆瓣高分"]]', app)
        self.assertNotIn('["lang", "语言"', app)

    def test_web_renders_matching_and_unavailable_separately(self):
        """匹配中 / 线路不可用必须是两种不同状态，且都给出可恢复文案。

        2026-09-28 优化方案 §6.3 起，「暂无片源」被判为禁止文案——把一次网络
        抖动/出口不可达说成「片库里没有这部片」是误报。契约改为锁定新的
        两条文案，详见 tests/test_web_vod_resolver_flow.py。
        """
        app = APP.read_text(encoding="utf-8")
        self.assertIn("正在匹配最佳线路", app)
        self.assertIn("线路暂时不可用，正在后台恢复", app)
        self.assertNotIn("暂无片源", app)

    def test_web_sends_early_year_instead_of_blanking_it(self):
        """旧实现把「更早」发成空串，等于不筛选（后端拿不到年份就按全量返回）。"""
        app = APP.read_text(encoding="utf-8")
        self.assertNotIn('all.sel.year === "更早" ? ""', app)
        self.assertIn("year: all.sel.year", app)

    def test_web_reports_degraded_upstream_separately_from_empty(self):
        """上游被拦时不能显示成「没有符合条件的内容」：有内容提示「可能不是最新」，
        没内容提示「暂时获取不到」。"""
        app = APP.read_text(encoding="utf-8")
        self.assertIn("d.upstream_available === false", app)
        self.assertIn("数据可能不是最新", app)
        self.assertIn("暂时获取不到片库", app)


if __name__ == "__main__":
    unittest.main()
