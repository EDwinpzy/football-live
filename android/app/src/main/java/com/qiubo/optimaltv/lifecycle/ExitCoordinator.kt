package com.qiubo.optimaltv.lifecycle

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import java.util.concurrent.atomic.AtomicBoolean
import com.qiubo.optimaltv.BackendService
import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.announcement.AnnouncementManager
import com.qiubo.optimaltv.hotupdate.HotUpdateManager
import com.qiubo.optimaltv.playback.StreamDecryptServer

/** Explicit user exit is intentionally stronger than an ordinary background transition. */
object ExitCoordinator {
    private val exitStarted = AtomicBoolean(false)

    fun isExiting(): Boolean = exitStarted.get()

    fun exit(activity: Activity) {
        if (!exitStarted.compareAndSet(false, true)) return
        OtvLog.i("explicit exit: release playback helpers and backend")
        AppLifecycleManager.shutdown()
        AnnouncementManager.shutdown()
        HotUpdateManager.shutdown()
        StreamDecryptServer.shutdown()
        /* 内嵌后端跑在独立进程（:backend）。旧实现只杀主进程，后端会活下来继续占着
           8090 与解密桥端口——下次进入复用这套「旧状态」，这正是「退出重进还是卡着」
           与「必须重启投影仪」的来源。这里三管齐下：先发退出指令、再停服务、最后兜底
           逐个结束本包内除主进程以外的进程。 */
        val serviceIntent = Intent(activity, BackendService::class.java)
        runCatching {
            activity.startService(serviceIntent.setAction(BackendService.ACTION_SHUTDOWN))
        }
        runCatching { activity.stopService(serviceIntent) }
        activity.finishAffinity()
        Handler(Looper.getMainLooper()).postDelayed({
            stopOwnedProcesses(activity)
            android.os.Process.killProcess(android.os.Process.myPid())
        }, 700L)
    }

    /** 结束本包其它进程（:backend 等）。runningAppProcesses 在 API 21+ 只回本应用自己的进程。 */
    private fun stopOwnedProcesses(activity: Activity) {
        runCatching {
            val am = activity.getSystemService(Context.ACTIVITY_SERVICE) as? android.app.ActivityManager
            val myPid = android.os.Process.myPid()
            am?.runningAppProcesses
                ?.filter { it.processName.startsWith(activity.packageName) && it.pid != myPid }
                ?.forEach { proc ->
                    OtvLog.i("explicit exit: kill ${proc.processName} (pid=${proc.pid})")
                    android.os.Process.killProcess(proc.pid)
                }
        }
    }
}
