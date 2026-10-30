package com.qiubo.optimaltv.ui.theme

import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp

/**
 * 设计稿坐标系（1:1 复刻原版 web）：
 * 原版按 1920×1080 CSS px 设计，运行时等比缩放（与原版 fit() 同思路）。
 * 用法：val s = rememberUiScale()
 *   320f.sx(s)  → 320 设计px 的尺寸(dp)
 *   29f.sxs(s)  → 29 设计px 的字号(sp)
 */
@Composable
fun rememberUiScale(): Float {
    val cfg = LocalConfiguration.current
    // scale = 屏宽dp / 1920：sx(v) = v*scale dp → v*scale*density 物理 px = v×(屏宽物理px/1920)
    return cfg.screenWidthDp / 1920f
}

fun Float.sx(scale: Float): Dp = (this * scale).dp

private const val TV_TEXT_SCALE = 1.30f

/** TV 文字在 1920 设计稿坐标上统一放大 30%；几何尺寸仍由 sx() 独立控制，避免整页等比放大。 */
@Composable
fun Float.sxs(scale: Float): TextUnit = with(LocalDensity.current) { (this@sxs * scale * TV_TEXT_SCALE).dp.toSp() }

/**
 * 全端统一设计 token（artifacts/2026-10-23-ui-redesign/SPEC.md「极简清爽」）。
 * 语义 token 供 UI 层引用；旧别名（White60 等）保留为兼容垫片，逐步迁移。
 */
object OtvColors {
    // —— 规范 token ——
    val Bg = Color(0xFF0A0A0A)              // 页面背景
    val Surface = Color(0xFF141414)         // 卡片/条目背景
    val SurfaceFocus = Color(0xFF1F1F1F)    // 焦点/按压态卡片背景
    val Brand = Color(0xFFFFFFFF)           // 焦点描边、选中态、强调
    val BrandDeep = Color(0xFFC9C9C9)       // 按压态、渐变端点
    val AccentGreen = Color(0xFF5BF29F)     // 球迹直播品牌绿：焦点、直播状态强调
    val Live = Color(0xFFE53935)            // LIVE 徽标红色背景
    val TextPrimary = Color(0xFFF5F5F5)     // 主文字
    val TextSecondary = Color(0xFFA0A0A0)   // 联赛、时间等次级文字
    val TextDisabled = Color(0xFF666666)    // 未开赛弱化信息
    val Divider = Color(0x14FFFFFF)         // rgba(255,255,255,0.08)

    // —— 兼容别名（旧调用点语义映射）——
    val Panel = Surface
    val White = TextPrimary                 // 文字场景的主文字
    val PureWhite = Color(0xFFFFFFFF)       // 胶囊底/海报雾等真正需要纯白处
    val White60 = TextSecondary
    val White75 = Color(0xBFF4F6F4)
    val White70 = Color(0xB3F4F6F4)
    val White55 = Color(0x8CF4F6F4)
    val White50 = TextDisabled
    val White30 = Color(0x4D6B746B)
    val Line = Divider
    val ChipBg = Surface                    // 未选 chip 底
    val Overlay = Color(0xCC151815)        // 海报上的点选浮层底
    val CardBlack80 = Surface               // 比赛卡背景
    val PanelBg = Color(0xF5151815)
    val InputBg = SurfaceFocus
    val AccentBlue = Color(0xFFFFFFFF)      // 旧强调色统一并入 brand
}
