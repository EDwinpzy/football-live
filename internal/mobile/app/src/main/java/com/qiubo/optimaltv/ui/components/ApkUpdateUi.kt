package com.qiubo.optimaltv.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.qiubo.optimaltv.apkupdate.ApkUpdateManager
import com.qiubo.optimaltv.apkupdate.ApkUpdateState

/** 移动端 APK 更新弹窗：下载完成后自动调起系统安装器。 */
@Composable
fun ApkUpdateUi() {
    val offer by ApkUpdateManager.offer.collectAsStateWithLifecycle()
    val state by ApkUpdateManager.state.collectAsStateWithLifecycle()
    val current = offer ?: return
    val primaryLabel = when (state) {
        ApkUpdateState.PermissionRequired -> "打开安装权限"
        is ApkUpdateState.Error -> "重试"
        else -> "立即更新"
    }
    AlertDialog(
        onDismissRequest = { if (!current.required) ApkUpdateManager.declineUpdate() },
        title = { Text(current.manifest.title) },
        text = {
            Column {
                Text("版本 ${current.manifest.versionName}")
                Spacer(Modifier.height(8.dp))
                current.manifest.changelog.take(6).forEach {
                    Text("• $it", maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                Spacer(Modifier.height(12.dp))
                when (state) {
                    is ApkUpdateState.Downloading -> {
                        val downloading = state as ApkUpdateState.Downloading
                        LinearProgressIndicator(
                            progress = { downloading.progress.coerceIn(0f, 1f) },
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Text(
                            if (downloading.attempt > 1) {
                                "正在下载 ${(downloading.progress * 100).toInt()}%（自动重试第 ${downloading.attempt} 次）"
                            } else {
                                "正在下载 ${(downloading.progress * 100).toInt()}%"
                            },
                        )
                    }
                    ApkUpdateState.PermissionRequired -> Text("请允许 My TV 安装未知应用，返回后会继续安装。")
                    ApkUpdateState.Installing -> Text("正在打开系统安装器…")
                    is ApkUpdateState.Error -> {
                        val error = state as ApkUpdateState.Error
                        Text(error.message)
                    }
                    null -> Text("下载完成后将自动打开系统安装器。")
                }
            }
        },
        confirmButton = {
            Button(onClick = {
                when (state) {
                    ApkUpdateState.PermissionRequired -> ApkUpdateManager.openUnknownAppSettings()
                    is ApkUpdateState.Error -> ApkUpdateManager.retryUpdate()
                    else -> ApkUpdateManager.acceptUpdate()
                }
            }) { Text(primaryLabel) }
        },
        dismissButton = if (!current.required) {
            { TextButton(onClick = { ApkUpdateManager.declineUpdate() }) { Text("稍后更新") } }
        } else null,
    )
}
