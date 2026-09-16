"""豆瓣非 imgN 图床（asset.doubanio.com）海报出图契约。

线上现象：设备日志 `[img] 抓取失败 url=https://asset.doubanio.com/.../movie_default_large.png
referer=https://www.hhkan1.com/ err=ctype=text/html`，网页版详情页左侧海报整片空白。
根因：旧规则只把 img\\d+.doubanio.com 当豆瓣图床，asset.* 落到 hhkan Referer 分支，
被豆瓣防盗链判给 HTML；网页端更直接，浏览器发不出豆瓣 Referer，直连必被 ORB 拦。
"""
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
BACKEND = ROOT / "tools/backend-src"
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class BackendPosterRefererTest(unittest.TestCase):
    def setUp(self):
        self.source = (BACKEND / "proxy.py").read_text(encoding="utf-8")

    def test_asset_host_is_recognised_as_douban_cdn(self):
        self.assertTrue(proxy.DOUBAN_IMG_ANY_RE.match("https://asset.doubanio.com/cuphead/pics/a.png"))
        self.assertTrue(proxy.DOUBAN_IMG_ANY_RE.match("https://img9.doubanio.com/view/photo/x.jpg"))
        self.assertFalse(proxy.DOUBAN_IMG_ANY_RE.match("https://evil-doubanio.com.attacker.test/a.png"))

    def test_douban_cdn_uses_douban_referer_first_with_fallbacks(self):
        start = self.source.index("def _img_fetch_with_mirrors")
        block = self.source[start:self.source.index("\ndef ", start + 10)]
        self.assertIn("DOUBAN_IMG_ANY_RE.match(url)", block)
        self.assertIn("referers = [DOUBAN_IMG_REFERER, None,", block)
        # 判定标准始终是真实图片内容类型，而不是 HTTP 200
        self.assertIn("ctype.startswith(\"image/\")", block)

    def test_relay_accepts_douban_referer_mode(self):
        strategies = proxy._relay_strategies("douban")
        self.assertTrue(strategies[0].get("Referer", "").endswith("douban.com/"))
        self.assertNotIn("Referer", strategies[-1])          # 兜底：不带 Referer 再试一次

    def test_douban_placeholder_is_a_real_png(self):
        # 豆瓣反爬（HTTP 200 + JS 挑战页）时回中性占位图，客户端不会拿到 502 空图
        self.assertTrue(proxy.PLACEHOLDER_POSTER_PNG.startswith(b"\x89PNG\r\n\x1a\n"))
        start = self.source.index("def _img_fetch_with_mirrors")
        block = self.source[start:self.source.index("\ndef ", start + 10)]
        self.assertIn('return PLACEHOLDER_POSTER_PNG, "image/png"', block)
        self.assertIn("if is_douban:", block)                # 只对豆瓣系兜底，其他源失败照旧上抛


class WebPosterRelayTest(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "tools/backend-src/www/app.js").read_text(encoding="utf-8")

    def test_non_image_douban_host_goes_through_relay_with_douban_referer(self):
        start = self.source.index("function doubanImg(u) {")
        block = self.source[start:self.source.index("\n}", start)]
        self.assertIn("DOUBAN_ANY_RE.test(u)", block)
        self.assertIn('"&ref=douban"', block)

    def test_relay_path_is_not_wrapped_twice(self):
        start = self.source.index("function img(u) {")
        block = self.source[start:self.source.index("\n}", start)]
        self.assertIn("DOUBAN_RELAY_PREFIX", block)
        self.assertLess(block.index("DOUBAN_RELAY_PREFIX"), block.index('"/hhkan/proxy?u="'))


if __name__ == "__main__":
    unittest.main()
