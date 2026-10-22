package com.qiubo.optimaltv

import android.content.Context
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 全局文件日志（需求 足球#3「莫名其妙的黑屏」排查）：
 * logcat 环形缓冲会滚动丢失，文件日志跨进程重启保留——黑屏发生后重启 app
 * 仍能回看黑屏前的生命周期/焦点/播放器状态序列。
 *
 * - 双写：logcat（原 TAG 体系不变）+ files/logs/otv.log
 * - v1.19 性能重构：写盘挪到专职守护线程批量 append（每 2s 或 512 条刷一次），
 *   调用线程只做 logcat + 无锁入队——旧版在调用线程同步 open/write/close，
 *   焦点/播放器/VLC 事件线程高频打日志时主线程被磁盘 IO 与全局锁卡住
 *   （启动门闸期间封面磁盘缓存与日志写盘竞争，卡顿被放大）。
 *   进程退出前 runCatching 尽力刷尾（丢失风险 ≤2s，排查场景可接受）。
 * - 轮转：超过 1.5MB 改名 otv.old.log（覆盖旧档），总占用 ≤3MB
 * - 全部写入包 runCatching：日志自身永不影响业务
 *
 * 查看：adb shell run-as com.qiubo.optimaltv cat files/logs/otv.log（debug 包）
 * 或 cat /data/data/... 同路径；release 包随 bugreport 的 dumpsys 不可得，需文件管理器。
 */
object OtvLog {
    private const val TAG = "OTV"
    private const val MAX_BYTES = 1_500_000L
    private const val FLUSH_INTERVAL_MS = 2_000L
    private const val FLUSH_THRESHOLD = 512

    /** 每线程独立实例（SimpleDateFormat 非线程安全；调用线程格式化，写线程只拼接 append） */
    private val ts: ThreadLocal<SimpleDateFormat> = object : ThreadLocal<SimpleDateFormat>() {
        override fun initialValue() = SimpleDateFormat("MM-dd HH:mm:ss.SSS", Locale.CHINA)
    }
    private var logFile: File? = null

    private val queue = java.util.concurrent.LinkedBlockingQueue<String>()
    @Volatile private var started = false

    fun init(context: Context) {
        runCatching {
            val dir = File(context.filesDir, "logs").apply { mkdirs() }
            logFile = File(dir, "otv.log")
            startWriterOnce()
        }
    }

    private fun startWriterOnce() {
        if (started) return
        synchronized(this) {
            if (started) return
            started = true
            Thread({
                val buf = ArrayList<String>(FLUSH_THRESHOLD)
                while (true) {
                    try {
                        buf.clear()
                        // 阻塞取首条（无日志时不耗 CPU），再批量取积压
                        buf.add(queue.take())
                        queue.drainTo(buf, FLUSH_THRESHOLD - 1)
                        runCatching { appendLines(buf) }
                    } catch (_: InterruptedException) {
                        runCatching { appendLines(queue.toList()) }
                        return@Thread
                    } catch (_: Throwable) { /* 日志线程永不退出 */ }
                }
            }, "otv-log").apply { isDaemon = true }.start()
        }
    }

    private fun appendLines(lines: List<String>) {
        val f = logFile ?: return
        if (f.exists() && f.length() > MAX_BYTES) {
            val old = File(f.parentFile, "otv.old.log")
            old.delete()
            f.renameTo(old)
        }
        f.appendText(lines.joinToString("\n", postfix = "\n"))
    }

    fun i(msg: String) = log(Log.INFO, msg)
    fun w(msg: String) = log(Log.WARN, msg)
    fun e(msg: String) = log(Log.ERROR, msg)

    private fun log(priority: Int, msg: String) {
        // logcat 仍是同步（FD 写便宜且排查主通道）；文件侧只入队。
        // 纯 JVM 单测（无 Robolectric / android.jar stub）里 android.util.Log 会抛
        // "not mocked"——埋点 Session.emit 走的就是这里，测试直接 RuntimeException。
        // 日志永不影响业务，所以此处兜住：单测要断言的是 emit 的口径，不是 logcat 输出。
        runCatching { Log.println(priority, TAG, msg) }
        if (started) {
            val line = "${ts.get()!!.format(Date())} ${priorityChar(priority)}/$TAG: $msg"
            if (queue.size < 8_192) queue.add(line)   // 防异常风暴撑爆内存
        }
    }

    private fun priorityChar(p: Int) = when (p) {
        // 用字面量而非 Log.* 常量：这些是 android.util.Log 的 int 常量，纯 JVM 单测里
        // 读它们同样会触发 stub 类的 "not mocked"（常量不会被 inline）。
        4 -> 'I'; 5 -> 'W'; 6 -> 'E'; else -> 'D'
    }
}
