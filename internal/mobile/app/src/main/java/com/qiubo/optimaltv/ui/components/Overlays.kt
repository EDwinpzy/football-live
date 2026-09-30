package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.qiubo.optimaltv.hotupdate.HotUpdateManager
import com.qiubo.optimaltv.hotupdate.UserUpdateState
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * v1.20 移动版全局浮层（与 TV 版同款语义，触控交互）：
 * - [HotUpdateOfferDialog]：非强制更新提案弹窗（立即更新/暂不更新）
 * - [HotUpdateProgressLayer]：确认后的下载进度浮层（完成自动重启）
 */
@Composable
fun HotUpdateOfferDialog() {
    val offer by HotUpdateManager.offer.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    AnimatedVisibility(
        visible = offer != null,
        enter = fadeIn() + scaleIn(initialScale = 0.96f),
        exit = fadeOut() + scaleOut(targetScale = 0.96f),
    ) {
        Box(
            Modifier
                .fillMaxSize()
                .background(Color(0x99000000))
                .padding(horizontal = 44.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .clip(RoundedCornerShape(24f.sx(s)))
                    .background(Color(0xF217171C))
                    .padding(horizontal = 44f.sx(s), vertical = 40f.sx(s))
                    .fillMaxWidth(),
            ) {
                Text("发现新版本", color = OtvColors.White, fontSize = 32f.sxs(s), fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(12f.sx(s)))
                val o = offer ?: return@Column
                val sizeTxt = if (o.sizeBytes > 0) " · %.1f MB".format(o.sizeBytes / 1048576f) else ""
                // 一步到位：跨版本时不逐级提示，明确「当前 vX → 最新 vY」
                val jumpTxt = if (o.fromVersion in 1 until o.version) "当前 v${o.fromVersion} → 最新" else "最新"
                Text("${o.name}$sizeTxt", color = OtvColors.White60, fontSize = 22f.sxs(s))
                Spacer(Modifier.height(8f.sx(s)))
                Text(
                    "$jumpTxt v${o.version}，将一步更新到位\n后台下载，完成后自动生效（无需重启 app）",
                    color = OtvColors.White50, fontSize = 22f.sxs(s),
                    maxLines = 3, overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.height(34f.sx(s)))
                Row {
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .clip(RoundedCornerShape(30f.sx(s)))
                            .tapCard { HotUpdateManager.declineUpdate() }
                            .background(Color(0x14FFFFFF), RoundedCornerShape(30f.sx(s)))
                            .padding(horizontal = 44f.sx(s), vertical = 20f.sx(s)),
                    ) {
                        Text("暂不更新", color = OtvColors.White, fontSize = 25f.sxs(s), fontWeight = FontWeight.SemiBold)
                    }
                    Spacer(Modifier.width(22f.sx(s)))
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .clip(RoundedCornerShape(30f.sx(s)))
                            .tapCard { HotUpdateManager.acceptUpdate() }
                            .background(Color(0xE6FFFFFF), RoundedCornerShape(30f.sx(s)))
                            .padding(horizontal = 44f.sx(s), vertical = 20f.sx(s)),
                    ) {
                        Text("立即更新", color = OtvColors.Bg, fontSize = 25f.sxs(s), fontWeight = FontWeight.SemiBold)
                    }
                }
            }
        }
    }
}

@Composable
fun HotUpdateProgressLayer() {
    val st by HotUpdateManager.userUpdate.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    AnimatedVisibility(visible = st != null, enter = fadeIn(), exit = fadeOut()) {
        val v = st ?: return@AnimatedVisibility
        val prog = when (v) {
            is UserUpdateState.Downloading -> v.progress.coerceIn(0f, 1f)
            is UserUpdateState.Restarting -> 1f
        }
        val bar by animateFloatAsState(prog, tween(260), label = "huBarM")
        Box(
            Modifier
                .fillMaxSize()
                .background(Color(0x73000000))
                .padding(horizontal = 44.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .clip(RoundedCornerShape(24f.sx(s)))
                    .background(Color(0xF217171C))
                    .padding(horizontal = 44f.sx(s), vertical = 40f.sx(s))
                    .fillMaxWidth(),
            ) {
                Text(
                    when (v) {
                        is UserUpdateState.Downloading -> "正在下载更新"
                        is UserUpdateState.Restarting -> "更新完成，正在生效…"
                    },
                    color = OtvColors.White, fontSize = 28f.sxs(s), fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.height(24f.sx(s)))
                Box(
                    Modifier
                        .fillMaxWidth()
                        .height(8f.sx(s))
                        .background(Color(0x24FFFFFF), RoundedCornerShape(4f.sx(s))),
                ) {
                    Box(
                        Modifier
                            .fillMaxWidth(bar)
                            .height(8f.sx(s))
                            .background(OtvColors.White, RoundedCornerShape(4f.sx(s))),
                    )
                }
                Spacer(Modifier.height(12f.sx(s)))
                if (v is UserUpdateState.Downloading) {
                    Text(
                        if (v.mbTotal > 0) "%.1f / %.1f MB".format(v.mbDone, v.mbTotal)
                        else "%.1f MB".format(v.mbDone),
                        color = OtvColors.White50, fontSize = 20f.sxs(s),
                    )
                }
            }
        }
    }
}
