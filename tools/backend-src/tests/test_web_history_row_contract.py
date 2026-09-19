"""网页版「最近观看」两则契约（2026-09-19 老板报障）。

① 「最近观看」混进足球比赛卡片
   App 端 history 表只由影视页写入（HistoryEntity.vodId），直播从不入历史。
   旧版网页版在 attachPlayer 成功后 pushHistory({id:"live:"+matchId}) —— 于是
   「最近观看 / 我的-历史记录」混进「热刺 对阵 埃弗顿」这类卡，且 live 无 poster
   恒为空封面显示成空卡。修法：不再写 live 历史 + getHistory() 过滤存量 live: 记录。

② 「上次看到」角标靠左
   App 端是 Modifier.align(Alignment.BottomEnd) + 内容自适应宽度（靠右）。
   旧 CSS `left:6px;right:6px` 左右撑满，文字自然贴左，两端不一致。
   修法：width:fit-content + right 单边定位。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class HistoryRowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "tools/backend-src/www/app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "tools/backend-src/www/style.css").read_text(encoding="utf-8")

    # ---------- ① 直播不入观看历史 ----------
    def test_live_match_not_pushed_to_history(self):
        """足球开播成功后不得写观看历史。"""
        self.assertNotIn('pushHistory({ id: "live:', self.app_js,
                         "足球直播又写进观看历史了；App 端 history 只收影视")

    def test_only_vod_pushes_history(self):
        """pushHistory 的真实调用点应只有影视播放那一处（注释里的字面量不算）。"""
        real = [ln.strip() for ln in self.app_js.splitlines()
                if ln.strip().startswith("pushHistory({")]
        self.assertEqual(len(real), 1, "pushHistory 调用点数量异常：%r" % real)
        self.assertIn("ctx.vid", real[0], "唯一调用点应为影视播放")

    def test_get_history_filters_legacy_live_entries(self):
        """存量清理：旧版已落盘的 live: 记录不得再回显。"""
        start = self.app_js.index("function getHistory()")
        line = self.app_js[start:self.app_js.index("\n", start)]
        self.assertIn("live:", line, "getHistory 未过滤 live: 存量记录")
        self.assertIn("filter", line, "getHistory 未做过滤")

    # ---------- ② 角标靠右 ----------
    def test_hist_tag_is_right_aligned(self):
        start = self.css.index(".pcard .pc-hist-tag{")
        block = self.css[start:self.css.index("}", start)]
        self.assertIn("right:", block, "角标缺少 right 定位")
        self.assertIn("width:fit-content", block, "角标应按内容收窄，不能左右撑满")
        self.assertNotIn("left:", block, "角标不应再有 left 锚点（会撑满导致文字贴左）")

    def test_hist_tag_does_not_overflow_poster(self):
        start = self.css.index(".pcard .pc-hist-tag{")
        block = self.css[start:self.css.index("}", start)]
        self.assertIn("max-width", block, "需限制最大宽度，长文案不得溢出海报")


if __name__ == "__main__":
    unittest.main()
