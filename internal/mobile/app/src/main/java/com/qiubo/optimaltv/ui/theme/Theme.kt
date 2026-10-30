package com.qiubo.optimaltv.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * 极简清爽设计 tokens（SPEC v1 2026-10-23：全端统一设计语言）。
 * 数值与 artifacts/2026-10-23-ui-redesign/SPEC.md 一致；字号 = TV 基准 ×0.85（手机端）。
 */
val Bg = Color(0xFF0A0A0A)              // 页面背景
val SurfaceDark = Color(0xFF141414)     // 卡片/条目背景
val SurfaceFocus = Color(0xFF1F1F1F)    // 焦点/按压态卡片背景
val Brand = Color(0xFFFFFFFF)           // 焦点描边、选中态、强调
val BrandDeep = Color(0xFFC9C9C9)       // 按压态、渐变端点
val LiveRed = Color(0xFFE53935)         // LIVE 徽标红色背景
val TextPrimary = Color(0xFFF5F5F5)     // 主文字
val TextSecondary = Color(0xFFA0A0A0)   // 联赛、时间等次级文字
val TextDisabled = Color(0xFF666666)    // 未开赛弱化信息
val Divider = Color(0x14FFFFFF)         // rgba(255,255,255,0.08) 分隔线

private val DarkScheme = darkColorScheme(
    primary = Brand,
    onPrimary = Color.Black,
    background = Bg,
    onBackground = TextPrimary,
    surface = SurfaceDark,
    onSurface = TextPrimary,
    surfaceVariant = SurfaceFocus,
    onSurfaceVariant = TextSecondary,
    secondary = BrandDeep,
)

/** 字号层级（TV 基准 ×0.85）：display 37 / title 22 / body 17 / label 13 / caption 11 */
val AppTypography = androidx.compose.material3.Typography(
    displaySmall = TextStyle(fontSize = 37.sp, fontWeight = FontWeight.Bold, lineHeight = 44.sp),
    titleLarge = TextStyle(fontSize = 22.sp, fontWeight = FontWeight.SemiBold, lineHeight = 30.sp),
    titleMedium = TextStyle(fontSize = 17.sp, fontWeight = FontWeight.SemiBold, lineHeight = 24.sp),
    bodyLarge = TextStyle(fontSize = 17.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontSize = 15.sp, lineHeight = 22.sp),
    labelLarge = TextStyle(fontSize = 13.sp, fontWeight = FontWeight.Medium),
    labelMedium = TextStyle(fontSize = 11.sp),
)

@Composable
fun QiuJiLiveTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkScheme,
        typography = AppTypography,
        content = content,
    )
}
