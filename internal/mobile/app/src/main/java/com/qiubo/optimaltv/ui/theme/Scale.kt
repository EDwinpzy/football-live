package com.qiubo.optimaltv.ui.theme

import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp

/**
 * 设计稿坐标系（v1.18 与 TV 版 1:1 同款，1:1 复刻原版 web）：
 * 原版按 1920×1080 CSS px 设计，运行时等比缩放（与原版 fit() 同思路）。
 * 移动版 v1.18 起前端样式/布局与 TV 版完全一致（横屏），仅交互保留触控。
 * v1.19（2026-09-03 总体#2）竖屏自适应：竖屏时改按 750 移动设计宽缩放——
 * 字号/控件呈手机常规尺寸（420dp 宽手机：chip 高 36dp、字号 16sp、海报卡 130dp），
 * 横屏仍按 1920 与 TV 版 1:1。
 * v2（2026-10-23 UI 重设计）：主要页面（赛程列表/顶栏/比赛卡）改用 SPEC 绝对
 * dp/sp 值；sx/sxs 仅保留给残留的设计稿坐标组件（播放页选源条、轻提示）。
 * 用法：val s = rememberUiScale()
 *   320f.sx(s)  → 320 设计px 的尺寸(dp)
 *   29f.sxs(s)  → 29 设计px 的字号(sp)
 */
@Composable
fun rememberUiScale(): Float {
    val cfg = LocalConfiguration.current
    // 横屏：scale = 屏宽dp / 1920（sx(v) = v*scale dp = v×屏宽物理px/1920）
    // 竖屏：scale = 屏宽dp / 750（移动 2x 设计宽基准，全站 sx() 尺寸自动手机化）
    return if (cfg.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE)
        cfg.screenWidthDp / 1920f
    else cfg.screenWidthDp / 750f
}

fun Float.sx(scale: Float): Dp = (this * scale).dp

@Composable
fun Float.sxs(scale: Float): TextUnit = with(LocalDensity.current) { (this@sxs * scale).dp.toSp() }
