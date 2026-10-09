package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.qiubo.optimaltv.announcement.AnnouncementManager
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * v1.20 运营公告顶部悬浮窗；2026-09-05 视觉稿 B 方案重设计：
 * 喇叭圆徽标（蓝 18% 底）+ 标题/正文两行 + ✕ 圆钮 + 底部蓝色倒计时细条（20s 收起进度）。
 * 位置：顶栏（tab bar）正下方居中悬浮（宽 940 设计px），深底 92% + hairline 描边。
 * 展示 20s 未操作自动收起（同内容本机不再弹——SHOWN 内容指纹由 AnnouncementManager 保证）。
 * 失焦不抢内容区初始焦点——仅在用户主动移入时才聚焦 ✕。
 */
@Composable
fun AnnouncementBanner() {
    val ann by AnnouncementManager.current.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    AnimatedVisibility(
        visible = ann != null,
        enter = fadeIn() + slideInVertically(initialOffsetY = { -it / 2 }),
        exit = fadeOut() + slideOutVertically(targetOffsetY = { -it / 2 }),
    ) {
        val a = ann ?: return@AnimatedVisibility
        // 底部细条 = 20s 收起倒计时（线性 1→0）
        val prog = remember(a.id) { Animatable(1f) }
        LaunchedEffect(a.id) {
            launch { prog.animateTo(0f, tween(20_000, easing = LinearEasing)) }
            delay(20_000)
            AnnouncementManager.dismiss()
        }
        Box(
            modifier = Modifier
                .padding(top = 128f.sx(s))
                .padding(horizontal = 490f.sx(s))   // 宽 940 居中
                .fillMaxWidth()
                .background(Color(0xEB101014), RoundedCornerShape(22f.sx(s)))
                .border(1f.sx(s), Color(0x17FFFFFF), RoundedCornerShape(22f.sx(s))),
        ) {
            Row(
                verticalAlignment = Alignment.Top,
                modifier = Modifier.padding(
                    start = 26f.sx(s), end = 26f.sx(s), top = 22f.sx(s), bottom = 24f.sx(s),
                ),
            ) {
                // 喇叭圆徽标
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .size(58f.sx(s))
                        .background(AccentBlue.copy(alpha = 0.18f), RoundedCornerShape(14f.sx(s))),
                ) {
                    Canvas(Modifier.size(30f.sx(s))) {
                        val w = this.size.width
                        val cone = Path().apply {
                            moveTo(w * 0.10f, w * 0.38f)
                            lineTo(w * 0.40f, w * 0.38f)
                            lineTo(w * 0.66f, w * 0.14f)
                            lineTo(w * 0.66f, w * 0.86f)
                            lineTo(w * 0.40f, w * 0.62f)
                            lineTo(w * 0.10f, w * 0.62f)
                            close()
                        }
                        drawPath(cone, Color(0xFF4DA3FF))
                        drawArc(
                            color = Color(0xFF4DA3FF),
                            startAngle = -55f, sweepAngle = 110f, useCenter = false,
                            topLeft = Offset(w * 0.62f, w * 0.28f),
                            size = Size(w * 0.36f, w * 0.44f),
                            style = Stroke(width = w * 0.08f, cap = StrokeCap.Round),
                        )
                    }
                }
                Spacer(Modifier.width(20f.sx(s)))
                Column(Modifier.weight(1f)) {
                    Text(
                        a.title,
                        color = OtvColors.White,
                        fontSize = 27f.sxs(s),
                        fontWeight = FontWeight.Bold,
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                    if (a.content.isNotBlank()) {
                        Spacer(Modifier.height(8f.sx(s)))
                        Text(
                            a.content,
                            color = OtvColors.White.copy(alpha = 0.62f),
                            fontSize = 21f.sxs(s),
                            maxLines = 2, overflow = TextOverflow.Ellipsis,
                            lineHeight = 30f.sxs(s),
                        )
                    }
                }
                Spacer(Modifier.width(18f.sx(s)))
                var closeF by remember { mutableStateOf(false) }
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .size(46f.sx(s))
                        .dpadFocusable(
                            scaleFocused = 1.06f,
                            focusedBg = Color(0x33FFFFFF),
                            focusedBgRadius = 23f.sx(s),
                            onFocusedChange = { closeF = it },
                            // v1.21 乱跑修复：公告条属顶部 chrome，✕ 不得作为垂直候选——
                            // 实测公告滑出/半隐时 UP 会抓到屏顶 ~20px 残条上（光标隐形）。
                            // floating 与 tab 栏同策略：只参与水平行内导航。
                            floating = true,
                        ) { AnnouncementManager.dismiss() },
                ) {
                    Text("✕", color = if (closeF) OtvColors.White else OtvColors.White50, fontSize = 22f.sxs(s))
                }
            }
            // 底部倒计时细条
            Box(
                Modifier
                    .align(Alignment.BottomStart)
                    .padding(start = 26f.sx(s), end = 26f.sx(s), bottom = 10f.sx(s))
                    .fillMaxWidth()
                    .height(3f.sx(s))
                    .background(Color(0x14FFFFFF), RoundedCornerShape(2f.sx(s))),
            ) {
                val radius = 2f.sx(s)
                Canvas(Modifier.fillMaxSize()) {
                    drawRoundRect(
                        color = AccentBlue,
                        size = Size(size.width * prog.value.coerceIn(0f, 1f), size.height),
                        cornerRadius = CornerRadius(radius.toPx()),
                    )
                }
            }
        }
    }
}
