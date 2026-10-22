package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
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
import com.qiubo.optimaltv.apkupdate.ApkUpdateManager
import com.qiubo.optimaltv.apkupdate.ApkUpdateState
import com.qiubo.optimaltv.apkupdate.ApkDownloadProgress
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/** APK 更新确认、下载进度和“允许安装未知应用”提示（TV 遥控器可操作）。 */
private const val APKUPDATE_DIALOG_GROUP = "apkupdate-dialog"

@Composable
fun ApkUpdateUi() {
    val offer by ApkUpdateManager.offer.collectAsStateWithLifecycle()
    val state by ApkUpdateManager.state.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    val requester = remember { FocusRequester() }
    val canDefer = state !is ApkUpdateState.Downloading && state != ApkUpdateState.Installing

    AnimatedVisibility(visible = offer != null, enter = fadeIn(), exit = fadeOut()) {
        val current = offer ?: return@AnimatedVisibility
        LaunchedEffect(current.manifest.versionCode, state is ApkUpdateState.PermissionRequired) {
            runCatching { requester.requestFocus() }
        }
        Box(
            Modifier.fillMaxSize().background(Color(0x99000000)).padding(horizontal = 120.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .background(Color(0xF217171C), RoundedCornerShape(28f.sx(s)))
                    .padding(horizontal = 64f.sx(s), vertical = 52f.sx(s))
                    .fillMaxWidth(),
            ) {
                androidx.compose.material3.Text(
                    current.manifest.title,
                    color = OtvColors.White,
                    fontSize = 38f.sxs(s),
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.height(12f.sx(s)))
                androidx.compose.material3.Text(
                    "版本 ${current.manifest.versionName}",
                    color = OtvColors.White60,
                    fontSize = 24f.sxs(s),
                )
                Spacer(Modifier.height(14f.sx(s)))
                current.manifest.changelog.take(6).forEach {
                    androidx.compose.material3.Text(
                        "• $it",
                        color = OtvColors.White70,
                        fontSize = 22f.sxs(s),
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                Spacer(Modifier.height(28f.sx(s)))
                ApkUpdateStatus(state, s)
                Spacer(Modifier.height(34f.sx(s)))
                Row(horizontalArrangement = Arrangement.spacedBy(26f.sx(s))) {
                    if (canDefer) {
                        ApkUpdateKey("稍后更新", s, primary = false, navGroup = APKUPDATE_DIALOG_GROUP) { ApkUpdateManager.declineUpdate() }
                    }
                    val primaryLabel = when (state) {
                        is ApkUpdateState.Downloading -> "取消更新"
                        ApkUpdateState.PermissionRequired -> "打开安装权限"
                        ApkUpdateState.Installing -> "正在打开系统安装器…"
                        is ApkUpdateState.Error -> "重试"
                        else -> "立即更新"
                    }
                    ApkUpdateKey(
                        label = primaryLabel,
                        s = s,
                        primary = true,
                        enabled = state != ApkUpdateState.Installing,
                        focus = requester,
                        navGroup = APKUPDATE_DIALOG_GROUP,
                    ) {
                        when (state) {
                            is ApkUpdateState.Downloading -> ApkUpdateManager.cancelUpdate()
                            ApkUpdateState.PermissionRequired -> ApkUpdateManager.openUnknownAppSettings()
                            ApkUpdateState.Installing -> Unit
                            is ApkUpdateState.Error -> ApkUpdateManager.retryUpdate()
                            else -> ApkUpdateManager.acceptUpdate()
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ApkUpdateStatus(state: ApkUpdateState?, s: Float) {
    when (state) {
        is ApkUpdateState.Downloading -> {
            androidx.compose.material3.LinearProgressIndicator(
                progress = { state.progress.coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth().height(10f.sx(s)),
            )
            Spacer(Modifier.height(10f.sx(s)))
            androidx.compose.material3.Text(
                ApkDownloadProgress.text(
                    downloaded = state.downloadedBytes,
                    total = state.totalBytes,
                    bytesPerSecond = state.bytesPerSecond,
                    attempt = state.attempt,
                ),
                color = OtvColors.White60,
                fontSize = 20f.sxs(s),
            )
        }
        ApkUpdateState.PermissionRequired -> androidx.compose.material3.Text(
            "请在系统设置中允许 足球直播 安装未知应用，返回后会继续安装。",
            color = OtvColors.White70,
            fontSize = 20f.sxs(s),
        )
        is ApkUpdateState.Error -> androidx.compose.material3.Text(
            state.message,
            color = Color(0xFFFFB4AB),
            fontSize = 20f.sxs(s),
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
        ApkUpdateState.Installing -> androidx.compose.material3.Text(
            "正在打开系统安装器…",
            color = OtvColors.White70,
            fontSize = 20f.sxs(s),
        )
        null -> androidx.compose.material3.Text(
            "APK 将下载完成后自动打开系统安装器。",
            color = OtvColors.White60,
            fontSize = 20f.sxs(s),
        )
    }
}

@Composable
private fun ApkUpdateKey(
    label: String,
    s: Float,
    primary: Boolean,
    enabled: Boolean = true,
    focus: FocusRequester? = null,
    navGroup: String? = null,
    onTap: () -> Unit,
) {
    var focused by remember { mutableStateOf(false) }
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .then(if (focus != null) Modifier.focusRequester(focus) else Modifier)
            .dpadFocusable(
                enabled = enabled,
                navGroup = navGroup,
                scaleFocused = 1.04f,
                focusedBg = if (primary) OtvColors.White else null,
                focusedBgRadius = 30f.sx(s),
                onFocusedChange = { focused = it },
                onClick = onTap,
            )
            .background(
                if (primary) Color(0x24FFFFFF) else Color(0x14FFFFFF),
                RoundedCornerShape(30f.sx(s)),
            )
            .padding(horizontal = 46f.sx(s), vertical = 21f.sx(s)),
    ) {
        androidx.compose.material3.Text(
            label,
            color = if (focused && primary) OtvColors.Bg else OtvColors.White,
            fontSize = 26f.sxs(s),
            fontWeight = FontWeight.SemiBold,
        )
    }
}
