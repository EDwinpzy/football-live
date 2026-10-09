"""网页版豆瓣海报图床契约。

豆瓣图床 img1..img9 同源同路径，但对第三方 Referer 的放行策略不一致：img9 放行，
img1/img2/img3 回 HTML 被浏览器 ORB 拦掉（net::ERR_BLOCKED_BY_ORB），线上表现为
「最近热门」整片海报不出图。这里锁住修复契约：统一改写到首选图床 + 失败轮换。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class WebPosterHostContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "tools/backend-src/www/app.js").read_text(encoding="utf-8")

    def test_preferred_host_is_first_candidate(self):
        start = self.source.index("const DOUBAN_IMG_HOSTS")
        line = self.source[start:self.source.index("\n", start)]
        self.assertIn('"img9.doubanio.com"', line)
        self.assertLess(line.index("img9.doubanio.com"), line.index("img1.doubanio.com"))

    def test_every_douban_cdn_host_is_covered(self):
        start = self.source.index("const DOUBAN_IMG_HOSTS")
        line = self.source[start:self.source.index("\n", start)]
        for host in ("img1.doubanio.com", "img2.doubanio.com", "img3.doubanio.com", "img9.doubanio.com"):
            self.assertIn(host, line)

    def test_img_helper_rewrites_douban_host(self):
        start = self.source.index("function img(u) {")
        block = self.source[start:self.source.index("\n}", start)]
        self.assertIn("doubanImg(", block)

    def test_failed_poster_rotates_to_next_host(self):
        handler_at = self.source.index('document.addEventListener("error"')
        handler = self.source[handler_at:self.source.index("}, true);", handler_at)]
        self.assertIn('el.tagName !== "IMG"', handler)
        self.assertIn("retryImageFallback(el, ev)", handler)
        start = self.source.index("function retryImageFallback(")
        block = self.source[start:self.source.index("function doubanImg(", start)]
        self.assertIn("DOUBAN_IMG_RE", block)
        # 轮换必须按 DOUBAN_IMG_HOSTS 顺序取下一台，而不是原地重试同一台。
        self.assertIn("DOUBAN_IMG_HOSTS[index < 0 ? 0 : index + 1]", block)


if __name__ == "__main__":
    unittest.main()
