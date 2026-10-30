package com.qiubo.optimaltv.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// 10-foot UI（方案 §2.2）：暗光环境高对比、正文字号 ≥16sp；
// 色板 2026-10-23 起对齐全端「极简清爽」规范（SPEC.md tokens）。
private val DarkScheme = darkColorScheme(
    primary = OtvColors.Brand,
    onPrimary = OtvColors.PureWhite,
    background = OtvColors.Bg,
    onBackground = OtvColors.TextPrimary,
    surface = OtvColors.Surface,
    onSurface = OtvColors.TextPrimary,
    surfaceVariant = OtvColors.SurfaceFocus,
    onSurfaceVariant = OtvColors.TextSecondary,
    secondary = OtvColors.BrandDeep,
)

val AppTypography = androidx.compose.material3.Typography(
    displaySmall = TextStyle(fontSize = 40.sp, fontWeight = FontWeight.Bold),
    titleLarge = TextStyle(fontSize = 28.sp, fontWeight = FontWeight.Bold, lineHeight = 38.sp),
    titleMedium = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.SemiBold, lineHeight = 28.sp),
    bodyLarge = TextStyle(fontSize = 18.sp, lineHeight = 27.sp),
    bodyMedium = TextStyle(fontSize = 16.sp, lineHeight = 24.sp),
    labelLarge = TextStyle(fontSize = 17.sp, fontWeight = FontWeight.Medium),
    labelMedium = TextStyle(fontSize = 14.sp),
)

@Composable
fun ZuoLiveTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkScheme,
        typography = AppTypography,
        content = content,
    )
}
