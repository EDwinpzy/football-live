"""网页版好好看 vres 封面出图契约（2026-09-19 定）。

线上症状：网页版影视页「最近热门 / 最近观看」间歇整片空卡——失败的卡片露出
`.pc-img` 的占位渐变（linear-gradient(160deg,#20304A,#0E1626)），说明 <img> 没出像素。

两条独立根因（都要锁住，少一条就会复发）：

① 前端把 https 图片一律放行给浏览器直连。vres.* 是好好看 CDN，会按 TLS/Referer 拒连，
   而前端的失败重试只覆盖豆瓣图床（DOUBAN_IMG_RE），vres 失败即死卡。内嵌后端场景
   （127.0.0.1:8090）出口就是用户家宽，必须走本机中继才能吃到服务端的镜像轮换 +
   磁盘缓存 + Referer 兜底。云端（腾讯云出口被 CDN 拒）反过来，仍须直连。

② hhkan.IMG_DOMAINS 首选域过期。2026-08-28 记录「cyscyy 已失联」把它排在末位，
   2026-09-19 复测 cyscyy 已恢复且最快（0.1~0.4s），enbymae 反而最慢（最长 31.2s，
   越过 _IMG_FETCH_TIMEOUT=20s 直接判失败）。每张图都先撞最慢域 → 整片超时。
"""
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class WebVresRelayContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "tools/backend-src/www/app.js").read_text(encoding="utf-8")
        cls.hhkan = (ROOT / "tools/backend-src/hhkan.py").read_text(encoding="utf-8")

    # ---------- ① 本地内嵌后端必须走中继 ----------
    def test_local_backend_flag_exists(self):
        self.assertIn("IS_LOCAL_BACKEND", self.app_js)

    def test_local_backend_covers_loopback_and_file(self):
        start = self.app_js.index("const IS_LOCAL_BACKEND")
        block = self.app_js[start:self.app_js.index("function img(u) {", start)]
        for probe in ('location.protocol === "file:"', '"localhost"', '"127.0.0.1"'):
            self.assertIn(probe, block, "本地后端判据缺少 %s" % probe)

    def test_img_routes_local_backend_through_relay(self):
        """本地后端必须先于「https 直连放行」分流，否则改动形同虚设。"""
        start = self.app_js.index("function img(u) {")
        block = self.app_js[start:self.app_js.index("\n}", start)]
        relay_at = block.index("IS_LOCAL_BACKEND")
        https_passthrough_at = block.index("/^https:\\/\\//i.test(u)")
        self.assertLess(relay_at, https_passthrough_at,
                        "IS_LOCAL_BACKEND 分支必须在 https 直连放行之前")

    def test_cloud_still_passes_https_through(self):
        """云端不能改：腾讯云出口被 hhkan CDN 拒，直连才出图。"""
        start = self.app_js.index("function img(u) {")
        block = self.app_js[start:self.app_js.index("\n}", start)]
        self.assertIn("/^https:\\/\\//i.test(u)) return u;", block)

    # ---------- ② 中继失败要回落直连，别死卡 ----------
    def test_relay_failure_falls_back_to_direct(self):
        start = self.app_js.index('document.addEventListener("error"')
        block = self.app_js[start:self.app_js.index("function img(u) {", start)]
        # 源码里正则是转义形态（\/hhkan\/proxy\?u=），按语义片段断言即可。
        self.assertIn("hhkan", block, "error 兜底未处理中继地址")
        self.assertIn("proxy", block, "error 兜底未处理中继地址")
        self.assertIn("dataset.fbk", block, "回落必须打标记防循环重试")
        self.assertIn("decodeURIComponent", block, "回落需从 u= 参数还原原始图片地址")

    # ---------- ③ 镜像域顺序按实测速度排 ----------
    def test_cyscyy_is_first_candidate(self):
        start = self.hhkan.index("IMG_DOMAINS = [")
        block = self.hhkan[start:self.hhkan.index("]", start)]
        hosts = re.findall(r"https://vres\.([a-z]+)\.com", block)
        self.assertTrue(hosts, "IMG_DOMAINS 解析为空")
        self.assertEqual(hosts[0], "cyscyy",
                         "首选域应为 2026-09-19 实测最快的 cyscyy，实得 %s" % hosts[0])

    def test_all_three_mirrors_retained(self):
        start = self.hhkan.index("IMG_DOMAINS = [")
        block = self.hhkan[start:self.hhkan.index("]", start)]
        for host in ("cyscyy", "zyxpedu", "enbymae"):
            self.assertIn("vres.%s.com" % host, block, "镜像域丢失：%s" % host)


if __name__ == "__main__":
    unittest.main()
