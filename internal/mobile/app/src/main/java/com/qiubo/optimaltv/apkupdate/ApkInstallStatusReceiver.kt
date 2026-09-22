package com.qiubo.optimaltv.apkupdate

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller

/** 接收 PackageInstaller 会话安装结果：需要用户确认时拉起系统安装页，成败回写更新状态。 */
class ApkInstallStatusReceiver : BroadcastReceiver() {
    @Suppress("DEPRECATION")
    override fun onReceive(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
        val userAction = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
        ApkUpdateManager.onInstallSessionStatus(status, message, userAction)
    }
}
