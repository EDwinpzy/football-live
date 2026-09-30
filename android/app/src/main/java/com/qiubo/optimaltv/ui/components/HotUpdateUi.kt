package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
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
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.qiubo.optimaltv.hotupdate.HotUpdateManager
import com.qiubo.optimaltv.hotupdate.UserUpdateState
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/**
 * v1.20 热更新交互层（用户需求 #11-#13）：
 * - [HotUpdateOfferDialog]：非强制更新提案弹窗——「立即更新 / 暂不更新」二选一。
 *   取消不打扰（同版本不再问）；确认进入后台下载。
 * - [HotUpdateProgressLayer]：确认后的下载进度浮层 → 完成自动重启（需求#11）。
 * 两者覆盖全屏（半透明遮罩保证可读），按钮聚焦走 dpadFocusable 焦点环。
 * 按钮归入独立 navGroup：引擎组内搜索无候选即封锁，方向键焦点绝不逃逸到弹窗后内容。
 */
private const val HOTUPDATE_DIALOG_GROUP = "hotupdate-dialog"

@Composable
fun HotUpdateOfferDialog() {
    val offer by HotUpdateManager.offer.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    AnimatedVisibility(
        visible = offer != null,
        enter = fadeIn() + scaleIn(initialScale = 0.96f),
        exit = fadeOut() + scaleOut(targetScale = 0.96f),
    ) {
        val okFr = remember { FocusRequester() }
        LaunchedEffect(Unit) { runCatching { okFr.requestFocus() } }
        Box(
            Modifier
                .fillMaxSize()
                .background(Color(0x99000000))
                .padding(horizontal = 120.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .background(Color(0xF217171C), RoundedCornerShape(28f.sx(s)))
                    .padding(horizontal = 64f.sx(s), vertical = 52f.sx(s))
                    .fillMaxWidth(),
            ) {
                Text("发现新版本", color = OtvColors.White, fontSize = 38f.sxs(s), fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(14f.sx(s)))
                val o = offer ?: return@Column
                val sizeTxt = if (o.sizeBytes > 0) " · %.1f MB".format(o.sizeBytes / 1048576f) else ""
                // 一步到位：跨版本时不逐级提示，明确「当前 vX → 最新 vY」
                val jumpTxt = if (o.fromVersion in 1 until o.version) "当前 v${o.fromVersion} → 最新" else "最新"
                Text("${o.name}$sizeTxt", color = OtvColors.White60, fontSize = 24f.sxs(s))
                Spacer(Modifier.height(10f.sx(s)))
                Text(
                    "$jumpTxt v${o.version}，将一步更新到位\n后台下载，完成后自动生效（无需重启 app）",
                    color = OtvColors.White50, fontSize = 24f.sxs(s),
                    maxLines = 3, overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.height(40f.sx(s)))
                Row(horizontalArrangement = Arrangement.spacedBy(26f.sx(s))) {
                    // 暂不更新：ghost 键（需求#12：非强更绝不影响使用）
                    UpdateDialogKey("暂不更新", s, primary = false, navGroup = HOTUPDATE_DIALOG_GROUP) { HotUpdateManager.declineUpdate() }
                    UpdateDialogKey("立即更新", s, primary = true, focus = okFr, navGroup = HOTUPDATE_DIALOG_GROUP) { HotUpdateManager.acceptUpdate() }
                }
            }
        }
    }
}

@Composable
private fun UpdateDialogKey(
    label: String,
    s: Float,
    primary: Boolean,
    focus: FocusRequester? = null,
    navGroup: String? = null,
    onTap: () -> Unit,
) {
    var f by remember { mutableStateOf(false) }
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .then(if (focus != null) Modifier.focusRequester(focus) else Modifier)
            .dpadFocusable(
                navGroup = navGroup,
                scaleFocused = 1.04f,
                focusedBg = if (primary) OtvColors.White else null,
                focusedBgRadius = 30f.sx(s),
                onFocusedChange = { f = it },
            ) { onTap() }
            .background(
                if (primary) Color(0x24FFFFFF) else Color(0x14FFFFFF),
                RoundedCornerShape(30f.sx(s)),
            )
            .padding(horizontal = 52f.sx(s), vertical = 22f.sx(s)),
    ) {
        Text(
            label,
            color = if (f && primary) OtvColors.Bg else OtvColors.White,
            fontSize = 28f.sxs(s), fontWeight = FontWeight.SemiBold,
        )
    }
}

/** 用户确认后的下载/重启进度浮层（中央卡片 + 进度条；完成自动重启） */
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
        val bar by animateFloatAsState(prog, tween(260), label = "huBar")
        Box(
            Modifier
                .fillMaxSize()
                .background(Color(0x73000000))
                .padding(horizontal = 120.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .background(Color(0xF217171C), RoundedCornerShape(28f.sx(s)))
                    .padding(horizontal = 64f.sx(s), vertical = 52f.sx(s))
                    .fillMaxWidth(),
            ) {
                Text(
                    when (v) {
                        is UserUpdateState.Downloading -> "正在下载更新"
                        is UserUpdateState.Restarting -> "更新完成，正在生效…"
                    },
                    color = OtvColors.White, fontSize = 32f.sxs(s), fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.height(28f.sx(s)))
                Box(
                    Modifier
                        .fillMaxWidth()
                        .height(10f.sx(s))
                        .background(Color(0x24FFFFFF), RoundedCornerShape(5f.sx(s))),
                ) {
                    Box(
                        Modifier
                            .fillMaxWidth(bar)
                            .height(10f.sx(s))
                            .background(OtvColors.White, RoundedCornerShape(5f.sx(s))),
                    )
                }
                Spacer(Modifier.height(14f.sx(s)))
                if (v is UserUpdateState.Downloading) {
                    Text(
                        if (v.mbTotal > 0) "%.1f / %.1f MB".format(v.mbDone, v.mbTotal)
                        else "%.1f MB".format(v.mbDone),
                        color = OtvColors.White50, fontSize = 22f.sxs(s),
                    )
                }
            }
        }
    }
}
