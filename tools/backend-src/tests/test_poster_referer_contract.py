"""豆瓣非 imgN 图床（asset.doubanio.com）海报出图契约。

线上现象：设备日志 `[img] 抓取失败 url=https://asset.doubanio.com/.../movie_default_large.png
referer=https://www.hhkan1.com/ err=ctype=text/html`，网页版详情页左侧海报整片空白。
根因：旧规则只把 img\\d+.doubanio.com 当豆瓣图床，asset.* 落到 hhkan Referer 分支，
被豆瓣防盗链判给 HTML；网页端更直接，浏览器发不出豆瓣 Referer，直连必被 ORB 拦。
"""
import io
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


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

    def test_legacy_douban_placeholder_is_a_real_png_for_cache_detection(self):
        self.assertTrue(proxy.PLACEHOLDER_POSTER_PNG.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_failed_douban_fetch_is_not_returned_as_a_successful_image(self):
        with mock.patch.object(proxy, "_img_fetch_pooled", side_effect=OSError("offline")):
            result = proxy._img_fetch_with_mirrors(
                "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p1.jpg")
        self.assertIsNone(result)

    def test_cache_rejects_new_and_discards_legacy_placeholder_but_keeps_real_images(self):
        url = "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p-cache-test.jpg"
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            proxy, "_IMG_CACHE", pathlib.Path(directory),
        ):
            data_path, type_path = proxy._img_cache_paths(url)
            proxy._img_cache_store(url, proxy.PLACEHOLDER_POSTER_PNG, "image/png")
            self.assertFalse(data_path.exists())
            self.assertFalse(type_path.exists())

            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_path.write_bytes(proxy.PLACEHOLDER_POSTER_PNG)
            type_path.write_text("image/png", encoding="ascii")
            self.assertIsNone(proxy._img_cache_load(url))
            self.assertFalse(data_path.exists())
            self.assertFalse(type_path.exists())

            real_image = b"real poster bytes"
            proxy._img_cache_store(url, real_image, "image/jpeg")
            self.assertEqual(proxy._img_cache_load(url), (real_image, "image/jpeg"))

    def test_failed_proxy_response_is_no_store_and_not_cached(self):
        class FakeHandler:
            def __init__(self):
                self.headers = {}
                self.response_headers = {}
                self.status = None
                self.wfile = io.BytesIO()

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.response_headers[name] = value

            def end_headers(self):
                pass

        handler = FakeHandler()
        image_url = "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p-proxy-test.jpg"
        with mock.patch.object(proxy, "_blocked_internal_url", return_value=False), \
             mock.patch.object(proxy, "_img_cache_load", return_value=None), \
             mock.patch.object(proxy, "_img_fetch_with_mirrors", return_value=None), \
             mock.patch.object(proxy, "_img_cache_store") as cache_store:
            proxy.Handler._hhkan_proxy(handler, image_url)

        self.assertEqual(handler.status, 502)
        self.assertEqual(handler.response_headers.get("Cache-Control"), "no-store")
        self.assertNotIn(b"\x89PNG\r\n\x1a\n", handler.wfile.getvalue())
        cache_store.assert_not_called()

    def test_successful_proxy_image_keeps_normal_disk_and_http_caching(self):
        class FakeHandler:
            def __init__(self):
                self.headers = {}
                self.response_headers = {}
                self.status = None
                self.wfile = io.BytesIO()

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.response_headers[name] = value

            def end_headers(self):
                pass

        handler = FakeHandler()
        image_url = "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p-success-test.jpg"
        image_bytes = b"normal poster bytes"
        with mock.patch.object(proxy, "_blocked_internal_url", return_value=False), \
             mock.patch.object(proxy, "_img_cache_load", return_value=None), \
             mock.patch.object(proxy, "_img_fetch_with_mirrors", return_value=(image_bytes, "image/jpeg")), \
             mock.patch.object(proxy, "_img_cache_store") as cache_store:
            proxy.Handler._hhkan_proxy(handler, image_url)

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.response_headers.get("Cache-Control"), "max-age=86400")
        self.assertEqual(handler.wfile.getvalue(), image_bytes)
        cache_store.assert_called_once_with(image_url, image_bytes, "image/jpeg")

    def test_prefetch_does_not_cache_failed_poster(self):
        image_url = "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p-prefetch-test.jpg"
        with mock.patch.object(proxy, "_img_cache_load", return_value=None), \
             mock.patch.object(proxy, "_img_fetch_with_mirrors", return_value=None), \
             mock.patch.object(proxy, "_img_cache_store") as cache_store:
            proxy._img_prefetch_one(image_url)
        cache_store.assert_not_called()


class WebPosterRelayTest(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "tools/backend-src/www/app.js").read_text(encoding="utf-8")

    def test_non_image_douban_host_goes_through_relay_with_douban_referer(self):
        start = self.source.index("function doubanImg(u) {")
        block = self.source[start:self.source.index("\n}", start)]
        self.assertIn("DOUBAN_ANY_RE.test(u)", block)
        self.assertIn('"&ref=douban"', block)

    def test_img_hosts_also_go_through_relay_with_douban_referer(self):
        """img1..img9 与 asset 一样受 Referer 防盗链，不能再让浏览器直连。"""
        start = self.source.index("function doubanImg(u) {")
        block = self.source[start:self.source.index("\n}", start)]
        self.assertNotIn("DOUBAN_IMG_HOSTS[0]", block)
        self.assertIn("DOUBAN_ANY_RE.test(u)", block)
        self.assertIn('"&ref=douban"', block)

    def test_relay_path_is_not_wrapped_twice(self):
        start = self.source.index("function img(u) {")
        block = self.source[start:self.source.index("\n}", start)]
        self.assertIn("DOUBAN_RELAY_PREFIX", block)
        self.assertLess(block.index("DOUBAN_RELAY_PREFIX"), block.index('"/hhkan/proxy?u="'))


if __name__ == "__main__":
    unittest.main()
