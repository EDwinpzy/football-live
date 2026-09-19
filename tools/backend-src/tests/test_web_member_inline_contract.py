"""网页版「我的」页会员区契约（2026-09-19 需求④）。

老板需求：① 网页版上面要增加续费的窗口；② 网页版会员信息展示改得和 TV 版、移动版一样。

App 基准：
  - 会员信息 = `ui/favorites/FavoritesScreen.kt` 的 `MemberInlineInfo`：并入「我的收藏」
    标题行靠右，状态点 + 「状态 · 套餐/到期/剩余」，无背景框，行尾「续费」入口。
  - 续费/兑换 = `ui/paywall/PaywallScreen.kt`：四张套餐卡（月/季/年/终身）+ 输入卡密兑换。
  - 续费按钮点下去走 `RenewalGateway.request(plan)`；两端 handler 均未注入 → 返回 false，
    页面提示「续费通道准备中，可先用输入卡密激活」——网页版必须同口径，不得自造支付通道。

本测试把这些口径锁死在源码文本上，改动 if 必须同步改这里。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WWW = ROOT / "www"
APP_JS = (WWW / "app.js").read_text(encoding="utf-8")
STYLE = (WWW / "style.css").read_text(encoding="utf-8")
HTML = (WWW / "index.html").read_text(encoding="utf-8")


def _member_status_body() -> str:
    """截取 memberStatus() 函数体（到下一个顶层 function 为止）。"""
    m = re.search(r"function memberStatus\(\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert m, "app.js 缺少 memberStatus()"
    return m.group(1)


class TestWebMemberInlineContract(unittest.TestCase):
    """会员信息行必须与 App MemberInlineInfo 逐分支同构。"""

    def test_five_status_branches_match_app(self):
        body = _member_status_body()
        # 分支顺序即 App 的 when 顺序，顺序错了会改变优先级语义（过期优先于联网校验）
        expect = [
            ("加载中…", "#8E8E93"),
            ("会员已过期", "#FF453A"),
            ("等待联网校验", "#FF9F0A"),
            ("终身会员", "#30D158"),
            ("会员有效", "#30D158"),
        ]
        pos = -1
        for text, color in expect:
            i = body.find('"' + text + '"')
            self.assertGreater(i, pos, f"状态「{text}」缺失或顺序与 App 不一致")
            pos = i
            self.assertIn(color, body, f"状态「{text}」配色 {color} 与 App 不符")

    def test_info_suffix_matches_app(self):
        body = _member_status_body()
        self.assertIn('" · 续费可用"', body)
        self.assertIn('"联网后自动恢复"', body)
        self.assertIn('"终身 · 永久有效"', body)
        # 有效态： 套餐 + 到期 yyyy-MM-dd + 剩余 N 天
        self.assertRegex(body, r"到期 \$\{ymd\} · 剩余 \$\{left\} 天")

    def test_only_leaf_renders_when_activated(self):
        """未持票 → 整行不渲染（App：未激活走统一会员页，不带行内小字）。"""
        m = re.search(r"function renderMemberInline\(\)\s*\{(.*?)\n\}", APP_JS, re.S)
        self.assertIsNotNone(m, "app.js 缺少 renderMemberInline()")
        body = m.group(1)
        self.assertIn("el.hidden = !show", body)
        self.assertIn("license.data", body)

    def test_leaf_is_inline_not_card(self):
        """「和 App 一样」= 无背景框的行内小字。旧版 .member-card 大卡定义不得复活。"""
        for dead in [".member-card{", ".mc-title{", ".mc-badge{", ".mc-code{", ".mc-form input{"]:
            self.assertNotIn(dead, STYLE, f"旧版会员卡样式 {dead} 应已移除")
        self.assertNotIn('class="member-card"', HTML)
        self.assertNotIn("renderMemberCard", APP_JS)
        self.assertNotIn("licenseDesc", APP_JS)   # 已由 memberStatus 取代，别留第二份口径

    def test_row_aligns_to_content_right_edge(self):
        """「续费」右缘必须落在 .fav-body 的内容右边界（App end = 86f.sx 与侧距同源）。"""
        m = re.search(r"\.fav-body\{(.*?)\}", STYLE, re.S)
        self.assertIsNotNone(m)
        self.assertIn("86px", m.group(1))
        self.assertIn("margin-left:auto", STYLE)   # 会员行靠右

    def test_portrait_top_pad_not_on_title_leaf(self):
        """竖屏顶部留白走行容器：写在文字叶子上会把同行的会员行一起顶下去。"""
        self.assertNotIn("body.portrait .fav-sec-title{padding-top", STYLE)
        self.assertIn("body.portrait .fav-title-row{padding-top:150px}", STYLE)


class TestWebRenewModalContract(unittest.TestCase):
    """顶部续费窗口：套餐目录、兑换入口、兜底口径都要与 App 对齐。"""

    def test_modal_exists_in_html(self):
        self.assertIn('id="memberModal"', HTML)
        for el in ["mmPlans", "mmInput", "mmBtn", "mmMsg", "mmClose", "mmSub"]:
            self.assertIn(f'id="{el}"', HTML, f"弹窗缺少 {el}")

    def test_plans_match_app_catalog(self):
        """套餐目录 = App PaywallShared.PLANS（改价必须两端口径一致）。"""
        for frag in ['"monthly", name: "月卡", price: "¥9.9"',
                     '"quarterly", name: "季卡", price: "¥29.9"',
                     '"yearly", name: "年卡", price: "¥99.9"',
                     '"lifetime", name: "终身", price: "¥128"']:
            self.assertIn(frag, APP_JS, f"套餐目录与 App 不一致：{frag}")

    def test_renew_falls_back_to_code_like_app(self):
        """App RenewalGateway 未注入 handler → false，页面提示走卡密兜底。

        网页版不得自造支付通道（跳转/下单/收款），否则两端行为分叉。
        """
        m = re.search(r"function mmRenew\(\)\s*\{(.*?)\n\}", APP_JS, re.S)
        self.assertIsNotNone(m, "app.js 缺少 mmRenew()")
        body = m.group(1)
        self.assertIn("续费通道准备中", body)
        self.assertIn("兑换", body)     # 指向卡密兑换
        for forbidden in ["window.open", "location.href", "afdian", "pay.", "alipay"]:
            self.assertNotIn(forbidden, body, f"续费按钮不得直接跳支付通道（{forbidden}）")

    def test_renew_entry_wired_to_modal(self):
        m = re.search(r"function renderMemberInline\(\)\s*\{(.*?)\n\}", APP_JS, re.S)
        self.assertIn("openMemberModal", m.group(1), "行尾「续费」必须能打开弹窗")

    def test_redeem_reuses_activate_code_path(self):
        """兑换必须复用 activateCode()（与 App 同一个 /api/activate 协议），不自造端点。"""
        m = re.search(r"async function mmSubmit\(\)\s*\{(.*?)\n\}", APP_JS, re.S)
        self.assertIsNotNone(m, "app.js 缺少 mmSubmit()")
        self.assertIn("activateCode(", m.group(1))

    def test_renew_refreshes_member_row(self):
        """兑换成功后状态行与弹窗状态要同步刷新（App 也是实时 collectAsState）。"""
        m = re.search(r"async function mmSubmit\(\)\s*\{(.*?)\n\}", APP_JS, re.S)
        body = m.group(1)
        self.assertIn("renderMemberInline()", body)
        self.assertIn("openMemberModal()", body)

    def test_portrait_redeem_input_has_explicit_height(self):
        """竖屏列向排列时 flex:1 作用于高度会塌陷，必须显式给高度。"""
        m = re.search(r"body\.portrait \.mm-redeem input\{(.*?)\}", STYLE, re.S)
        self.assertIsNotNone(m, "缺少竖屏兑换输入框样式")
        body = m.group(1)
        self.assertIn("height:", body)
        self.assertIn("flex:none", body)


if __name__ == "__main__":
    unittest.main()
