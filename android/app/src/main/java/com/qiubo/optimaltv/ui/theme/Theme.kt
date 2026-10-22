package com.qiubo.optimaltv.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// 10-foot UI（方案 §2.2）：暗光环境高对比、正文字号 ≥16sp
val BgBlack = Color(0xFF050507)
val SurfaceDark = Color(0xFF141419)
val SurfaceCard = Color(0xFF1C1C22)
val AccentBlue = Color(0xFF0A84FF)
val TextPrimary = Color(0xFFF5F5F7)
val TextSecondary = Color(0xFF9A9AA2)

private val DarkScheme = darkColorScheme(
    primary = AccentBlue,
    onPrimary = Color.White,
    background = BgBlack,
    onBackground = TextPrimary,
    surface = SurfaceDark,
    onSurface = TextPrimary,
    surfaceVariant = SurfaceCard,
    onSurfaceVariant = TextSecondary,
    secondary = Color(0xFF8E8E93),
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
fun FootballLiveTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkScheme,
        typography = AppTypography,
        content = content,
    )
}
