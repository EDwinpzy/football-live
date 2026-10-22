package com.qiubo.optimaltv.playback

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.OtvLog
import java.security.MessageDigest

/**
 * 播放解码器计数快照（长时体检 / 冻结取证）。
 *
 * `rendered` 是【真正送到画面的帧数】，这是判定「时钟在走、画面冻住」的唯一硬信号：
 * 位置照常前进但 rendered 长时间不增 = 解码线程/渲染管线已经不出帧（既有两道看门狗
 * 都只认「BUFFERING 超时」与「位置不前进」，对这种形态不响）。
 * 引擎拿不到计数时返回 null（libVLC 兜底引擎），此时只记日志、不做冻结判定。
 */
data class DecoderStats(
    val rendered: Int,
    val skipped: Int,
    val dropped: Int,
    val droppedInput: Int,
    val decoderInits: Int,
    val consecutiveDropped: Int,
)

/** 一次长时体检的完整现场快照（字段对齐《15–20 分钟卡顿排查方案》§21/§22）。 */
data class PlaybackHealthSnapshot(
    val kind: String,
    val content: String,
    val lineIndex: Int,
    val lineLabel: String,
    val lineCount: Int,
    val url: String,
    val engineId: String,
    val engineName: String,
    val engineEpoch: Int,
    val state: String,
    val isPlaying: Boolean,
    val positionMs: Long,
    val durationMs: Long,
    val bufferedAheadMs: Long,
    val firstFrame: Boolean,
    val decoder: DecoderStats?,
    val viaRelay: Boolean,
    val relayBroken: Int,
    val backendStarted: Boolean,
    val decryptSlots: Int,
    val recoveryStep: Int,
    val stallRecoveries: Int,
    val note: String,
)

/**
 * 长时播放体检（2026-09-16 用户需求①：「看十几二十分钟后卡死不动、退出重进才好」取证）。
 *
 * 线上现象：足球直播播放十几分钟后画面冻住不再恢复，且它不属于既有
 * 看门狗覆盖的形态——PlayerViewModel 的卡顿监视只认「BUFFERING 超过 8s」与「READY 但位置
 * 不前进」；真实故障很可能是**位置照常前进、解码器不再出帧**，两道看门狗都不响，用户只能
 * 退出重进（甚至重启投影仪）。
 *
 * 本模块负责「取证」：
 * 1) 每 5 分钟打一行体检（`[HEALTH]`）：引擎/状态/位置/缓冲/渲染帧数/丢帧/中继/后端/
 *    解密池/内存/线程/网络——卡死前那几分钟的走向一眼可见；
 * 2) 卡顿或冻结时打一份多行转储（`[STALL-DUMP]`），对齐排查方案 §22 的字段清单。
 *
 * 日志经 [OtvLog] 双写 logcat 与 files/logs/otv.log，**重启 app 后仍能回看冻结前现场**
 * （这正是「卡了只能重启设备」场景唯一的证据来源）。所有取数都走 runCatching，
 * 体检自身永不影响播放。
 */
object PlaybackHealthLog {

    /** 内容名截断长度：体检行保持单行可读（完整片名在 startSession 日志里）。 */
    private const val CONTENT_MAX = 28

    /** 播放 URL 只记短哈希：可跨日志行关联，又不会把带签名的长地址灌进日志。 */
    fun urlHash(url: String): String {
        if (url.isBlank()) return "-"
        return runCatching {
            val d = MessageDigest.getInstance("SHA-1").digest(url.toByteArray())
            d.take(4).joinToString("") { "%02x".format(it) }
        }.getOrDefault("-")
    }

    /** 一行体检（每 5 分钟一次）。 */
    fun line(s: PlaybackHealthSnapshot): String {
        val d = s.decoder
        return "[HEALTH] ${s.note} kind=${s.kind} content=${trim(s.content)} " +
            "line=${s.lineIndex}/${s.lineCount}(${s.lineLabel}) url=${urlHash(s.url)} " +
            "engine=${s.engineId}#${s.engineEpoch} state=${s.state} playing=${yn(s.isPlaying)} " +
            "pos=${s.positionMs / 1000}s dur=${s.durationMs / 1000}s buf=${s.bufferedAheadMs}ms " +
            "first=${yn(s.firstFrame)} " +
            "frames=${d?.rendered ?: -1} drop=${d?.dropped ?: -1} dropIn=${d?.droppedInput ?: -1} " +
            "init=${d?.decoderInits ?: -1} streak=${d?.consecutiveDropped ?: -1} " +
            "relay=${yn(s.viaRelay)} relaysBroken=${s.relayBroken} " +
            "backend=${yn(s.backendStarted)} slots=${s.decryptSlots} " +
            "recovery=${s.recoveryStep}/${s.stallRecoveries} " +
            "heap=${heapUsedMb()}/${heapMaxMb()}MB thr=${threadCount()} net=${network()}"
    }

    /** 卡顿/冻结转储：多行现场（对齐排查方案 §22 字段）。 */
    fun dump(s: PlaybackHealthSnapshot): String {
        val d = s.decoder
        return buildString {
            appendLine("===== PLAYBACK STALL DUMP =====")
            appendLine("reason=${s.note}")
            appendLine(
                "kind=${s.kind} content=${trim(s.content)} line=${s.lineIndex}/${s.lineCount}" +
                    "(${s.lineLabel}) url=${urlHash(s.url)}",
            )
            appendLine(
                "engine=${s.engineId}#${s.engineEpoch}(${s.engineName}) state=${s.state} " +
                    "playing=${yn(s.isPlaying)} first=${yn(s.firstFrame)}",
            )
            appendLine("pos=${s.positionMs / 1000}s dur=${s.durationMs / 1000}s buf=${s.bufferedAheadMs}ms")
            appendLine(
                "decoder: rendered=${d?.rendered ?: -1} skipped=${d?.skipped ?: -1} " +
                    "dropped=${d?.dropped ?: -1} droppedInput=${d?.droppedInput ?: -1} " +
                    "inits=${d?.decoderInits ?: -1} maxStreak=${d?.consecutiveDropped ?: -1}",
            )
            appendLine(
                "relay=${yn(s.viaRelay)} relaysBroken=${s.relayBroken} " +
                    "backend=${yn(s.backendStarted)} decryptSlots=${s.decryptSlots}",
            )
            appendLine(
                "recovery=${s.recoveryStep}/${s.stallRecoveries} " +
                    "heap=${heapUsedMb()}/${heapMaxMb()}MB thr=${threadCount()} net=${network()}",
            )
            append("===============================")
        }
    }

    /** 每 5 分钟体检（INFO：正常路径不刷屏）。 */
    fun tick(s: PlaybackHealthSnapshot) = OtvLog.i(line(s))

    /** 起播现场（INFO）：短视频 / 30s 验收也有体检行可比对。 */
    fun start(s: PlaybackHealthSnapshot) = OtvLog.i(line(s.copy(note = "起播")))

    /** 卡顿转储（WARN）：交给恢复阶梯前先留现场。 */
    fun stall(s: PlaybackHealthSnapshot) = OtvLog.w(dump(s))

    /** 冻结转储（ERROR）：「时钟在走、画面不出帧」的高危形态。 */
    fun freeze(s: PlaybackHealthSnapshot) = OtvLog.e(dump(s))

    // ---- 设备侧环境（均为廉价调用；体检 10s 一次，不构成主线程负担） ----

    fun heapUsedMb(): Long = runCatching {
        val rt = Runtime.getRuntime()
        (rt.totalMemory() - rt.freeMemory()) / (1024L * 1024L)
    }.getOrDefault(-1L)

    fun heapMaxMb(): Long = runCatching {
        Runtime.getRuntime().maxMemory() / (1024L * 1024L)
    }.getOrDefault(-1L)

    fun threadCount(): Int = runCatching { Thread.activeCount() }.getOrDefault(-1)

    /** 当前链路类型（ETHERNET/WIFI/CELLULAR/OTHER/NONE）：卡顿排查要能区分
     *  「上游挂了」还是「设备自己掉网」。 */
    fun network(): String = runCatching {
        val cm = Graph.appContext
            .getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return@runCatching "UNKNOWN"
        val net = cm.activeNetwork ?: return@runCatching "NONE"
        val caps = cm.getNetworkCapabilities(net) ?: return@runCatching "NONE"
        when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ETHERNET"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "WIFI"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "CELLULAR"
            else -> "OTHER"
        }
    }.getOrDefault("UNKNOWN")

    private fun yn(b: Boolean) = if (b) "1" else "0"

    private fun trim(s: String): String =
        if (s.length <= CONTENT_MAX) s.ifBlank { "-" } else s.take(CONTENT_MAX) + "…"
}
