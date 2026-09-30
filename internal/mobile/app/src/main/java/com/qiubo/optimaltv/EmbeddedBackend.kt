package com.qiubo.optimaltv

import android.content.Context
import android.util.Log
import com.qiubo.optimaltv.BuildConfig
import java.io.File
import java.io.InputStream
import java.util.concurrent.TimeUnit

/**
 * 内置后端（需求：一个 app 搞定，投影仪不用开电脑/不配后端）。
 * app 启动时把后端 zip 解压到应用可写目录，用 Chaquopy 在本机 127.0.0.1:8090 跑 proxy.py
 * —— app 的 baseUrl 默认指向这里。
 *
 * proxy.py 只用标准库 + 自有模块（hhkan/team_backdrop/scraper），无需 pip 包；
 * www/ 与缓存目录(team_icon_cache/svg/transparent)运行前在解压目录内创建。
 *
 * v1.18 热更新（公测版）：优先解压 HotUpdateManager 暂存的云端包
 * filesDir/hotupdate/backend-hu.zip；坏包三层防护（详见 quarantine/probeHealthy），
 * 任何失败自动回退 assets 内置包，行为与旧版完全一致。
 */
object EmbeddedBackend {
    private const val TAG = "OTV-Backend"
    private const val PORT = 8090

    /** 热更暂存目录与文件（HotUpdateManager 写入） */
    private const val HU_DIR = "hotupdate"
    private const val HU_ZIP = "backend-hu.zip"
    private const val HU_MARKER = ".hotversion"

    /** 解压后的后端根目录（app 私有可写） */
    fun backendDir(ctx: Context): File = File(ctx.filesDir, "embedded_backend")

    /** 长时体检用（2026-09-16）：内嵌后端端口。注意后端跑在 :backend 独立进程，
     *  主进程读不到它的 started 标志（会得到假阴性），探活一律走本端口。 */
    fun port(): Int = PORT

    private var started = false

    private class Extracted(val dir: File, val src: String, val code: Int)

    /** app 启动时调用（幂等）：解压 zip → 建目录 → Chaquopy 后台跑 proxy.main(PORT)。
     *  v1.10：解压（36MB zip，低端 CPU 首装/升级时要数秒）挪进后台线程——
     *  旧版在主线程解压，App 冷启动首帧被卡住（投影仪上观感=开机黑屏久）。
     *  v1.18：解压源优先热更包；热更包 import 崩溃当场回退 assets 重启，
     *  起线程后健康探测不过则隔离（下次启动走 assets）。 */
    fun ensureStarted(ctx: Context) {
        if (started) return
        started = true
        try {
            val appCtx = ctx.applicationContext
            // 后台线程：解压 → 建目录 → Chaquopy 加载 python，把解压目录加进 sys.path，起 proxy
            // P2 修复（2026-09-04）：整个线程体包进 try——旧版 extractBest/makeRuntimeDirs
            // 在 try 外，磁盘满等 IO 异常未捕获直接杀 :backend 进程（START_STICKY 重建
            // 再崩=崩溃循环，内容永久不可用）；现捕获后走 assets 二次尝试并留日志
            Thread {
                try {
                    val migrationCaPath = writeMigrationCa(appCtx).absolutePath
                    var ex = extractBest(appCtx)
                    makeRuntimeDirs(ex.dir)
                    try {
                        // Chaquopy 要求：先 Python.start(AndroidPlatform(context))，再 getInstance()
                        com.chaquo.python.Python.start(com.chaquo.python.android.AndroidPlatform(appCtx))
                        val mod = com.chaquo.python.Python.getInstance().getModule("proxy_runner")
                        mod.callAttr("start", ex.dir.absolutePath, PORT, BuildConfig.API_BASE_URL, migrationCaPath)
                        if (ex.src.startsWith("hot") && probeHealthy(appCtx, ex.code)) {
                            // 热更包健康起服：记激活版本 + 上报 applied（强制更新闭环）
                            com.qiubo.optimaltv.hotupdate.HotUpdateManager.notifyApplied(appCtx, ex.code)
                        }
                        Log.i(TAG, "后端已启动 src=${ex.src} port=$PORT")
                    } catch (e: Exception) {
                        if (ex.src.startsWith("hot")) {
                            // proxy_runner.start() 内的 import proxy 是同步执行且失败模块不驻留
                            // sys.modules——把 assets 目录插到 sys.path[0] 后重新 start 即干净重导。
                            Log.e(TAG, "热更包启动失败: ${e.message} → 隔离并回退 assets")
                            quarantine(appCtx, "import_crash", ex.code)
                            ex = Extracted(extractZipIfNeeded(appCtx), "assets", 0)
                            makeRuntimeDirs(ex.dir)
                            try {
                                val mod2 = com.chaquo.python.Python.getInstance().getModule("proxy_runner")
                                mod2.callAttr("start", ex.dir.absolutePath, PORT, BuildConfig.API_BASE_URL, migrationCaPath)
                                com.qiubo.optimaltv.hotupdate.HotUpdateManager.notifyActiveAssets(appCtx)
                                Log.i(TAG, "后端已启动（回退 assets）port=$PORT")
                            } catch (e2: Exception) {
                                Log.e(TAG, "后端启动失败(assets): ${e2.message}", e2)
                            }
                        } else {
                            Log.e(TAG, "后端启动失败: ${e.message}", e)
                        }
                    }
                } catch (e: Exception) {
                    // 解压阶段异常（磁盘满/IO 错）：再给一次 assets 全新解压机会
                    Log.e(TAG, "后端解压失败（${e.message}），尝试 assets 重解压", e)
                    try {
                        val caPath = writeMigrationCa(appCtx).absolutePath
                        val ex2 = Extracted(extractZipIfNeeded(appCtx), "assets", 0)
                        makeRuntimeDirs(ex2.dir)
                        com.chaquo.python.Python.start(com.chaquo.python.android.AndroidPlatform(appCtx))
                        com.chaquo.python.Python.getInstance()
                            .getModule("proxy_runner").callAttr("start", ex2.dir.absolutePath, PORT, BuildConfig.API_BASE_URL, caPath)
                        com.qiubo.optimaltv.hotupdate.HotUpdateManager.notifyActiveAssets(appCtx)
                        Log.i(TAG, "后端已启动（解压异常后 assets 重试成功）port=$PORT")
                    } catch (e2: Exception) {
                        Log.e(TAG, "后端启动失败(assets 重试): ${e2.message}", e2)
                    }
                }
            }.start()
            Log.i(TAG, "后端已请求启动 port=$PORT")
        } catch (e: Exception) {
            Log.e(TAG, "后端初始化失败: ${e.message}", e)
        }
    }

    private fun writeMigrationCa(ctx: Context): File {
        val target = File(ctx.filesDir, "mytv_migration_ca.pem")
        ctx.resources.openRawResource(R.raw.mytv_migration_ca).use { input ->
            target.outputStream().use { output -> input.copyTo(output) }
        }
        return target
    }

    private fun makeRuntimeDirs(dir: File) {
        listOf("www", "svg_icon_cache", "transparent_icon_cache").forEach { File(dir, it).mkdirs() }
    }

    /** 选解压源：热更包（marker+zip 齐全）优先，解压失败自动隔离回退 assets */
    private fun extractBest(ctx: Context): Extracted {
        val hu = huZip(ctx)
        if (hu != null) {
            try {
                val (zip, code) = hu
                val dir = extractZipTo(ctx, zip.inputStream().buffered(), "hot|$code|${zip.length()}")
                return Extracted(dir, "hot:$code", code)
            } catch (e: Exception) {
                Log.e(TAG, "热更 zip 解压失败: ${e.message} → 回退 assets")
                quarantine(ctx, "zip_extract", hu.second)
            }
        }
        return Extracted(extractZipIfNeeded(ctx), "assets", 0)
    }

    /** 热更包就绪返回 (zip文件, 版本号)；marker 缺失/损坏视为不存在 */
    private fun huZip(ctx: Context): Pair<File, Int>? {
        if (!BuildConfig.HOTUPDATE_ENABLED) return null
        val dir = File(ctx.filesDir, HU_DIR)
        val code = runCatching {
            File(dir, HU_MARKER).takeIf { it.isFile }?.readText()?.trim()?.toIntOrNull() ?: 0
        }.getOrDefault(0)
        if (code <= 0) return null
        val zip = File(dir, HU_ZIP)
        if (!zip.isFile || zip.length() < 1024) return null
        return zip to code
    }

    /** 隔离坏包：删 marker + zip 改名 .bad（保留现场便于排查），并上报云端 log + 拉黑该版本 */
    private fun quarantine(ctx: Context, reason: String, code: Int) {
        runCatching {
            val dir = File(ctx.filesDir, HU_DIR)
            File(dir, HU_MARKER).delete()
            val zip = File(dir, HU_ZIP)
            if (zip.isFile) {
                File(dir, "$HU_ZIP.bad").delete()
                zip.renameTo(File(dir, "$HU_ZIP.bad"))
            }
            // 2026-09-30：坏包的解压目录必须一并作废。原来只删热更 marker，
            // 而 embedded_backend/.extracted 仍写着 `hot|<code>|<len>`，于是下次
            // 启动 extractZipIfNeeded 命中标记直接复用这份坏包解压目录，
            // 内置 assets 后端永远不生效（现场：APK 已升级但设备仍跑旧搜索逻辑）。
            EmbeddedBackendPolicy.invalidateAfterQuarantine(backendDir(ctx))
            com.qiubo.optimaltv.hotupdate.HotUpdateManager.notifyApplyFailed(ctx, code, reason)
        }
        Log.w(TAG, "热更包已隔离 ($reason)，本次及下次启动使用 assets 内置包")
    }

    /**
     * 热更包健康探测：proxy.main 绑定失败会进 proxy_runner 的 20 分钟重试循环
     * （callAttr 不抛错，只有探测能发现）。120s 内 /api/health 任何 HTTP 响应即算起服
     * （MuMu 多变体并存时 Python 冷导入实测可超 45s）；超时记失败一次，连续两次启动
     * 都失败才判坏包隔离（防共享端口/冷启动慢误杀好包）。仅热更源探测；
     * assets 路径保持历史行为不变。返回是否健康。
     */
    private fun probeHealthy(ctx: Context, code: Int): Boolean {
        val client = okhttp3.OkHttpClient.Builder()
            .connectTimeout(3, TimeUnit.SECONDS)
            .readTimeout(5, TimeUnit.SECONDS)
            .build()
        val req = okhttp3.Request.Builder().url("http://127.0.0.1:$PORT/api/health").build()
        val deadline = System.currentTimeMillis() + 120_000
        while (System.currentTimeMillis() < deadline) {
            try {
                client.newCall(req).execute().use { return true }   // 有响应=端口已绑定
            } catch (_: Exception) { /* 未起服，继续等 */ }
            try { Thread.sleep(2000) } catch (_: InterruptedException) { return false }
        }
        val fails = com.qiubo.optimaltv.hotupdate.HotUpdateManager.countProbeFail(ctx)
        if (fails < 2) {
            Log.w(TAG, "热更后端健康探测失败 #$fails（慢冷启动/共享端口，两击才隔离）")
            return false
        }
        Log.e(TAG, "热更后端连续两轮健康探测超时 → 隔离")
        quarantine(ctx, "health_probe", code)
        return false
    }

    /** 解压 assets zip 到应用目录（zip 内容变化时自动重新解压），返回目录 */
    private fun extractZipIfNeeded(ctx: Context): File {
        val assetPath = "backend/python-backend.zip"
        // 2026-09-19 冷启动优化（方案 C2）：旧实现每次启动全量流式读 36MB zip 算 SHA-256
        // 当签名（秒级纯 IO）；发布流水线保证 versionCode 单调递增，「版本号 + zip 长度」
        // 即可识别换包，available() 读资产长度是 O(1)，不再碰 zip 内容。
        val zipLength = ctx.assets.open(assetPath).use { it.available() }
        return extractZipTo(ctx, ctx.assets.open(assetPath).buffered(),
            "assets|${BuildConfig.VERSION_CODE}|$zipLength")
    }

    /** 通用解压：.extracted 标记记录「源签名」，不一致（换源/升级）就清目录重解压 */
    private fun extractZipTo(ctx: Context, input: InputStream, signature: String): File {
        val dir = backendDir(ctx)
        val flag = File(dir, EmbeddedBackendPolicy.MARKER_NAME)
        // 旧版仅凭 .extracted 标记跳过解压：后端 zip 升级后设备仍跑旧 proxy。
        // 现在标记里记录源签名（assets 版本号+大小 / hot 版本号+大小），不一致就重解压。
        val currentMarker = if (flag.isFile) runCatching { flag.readText() }.getOrNull() else null
        if (EmbeddedBackendPolicy.canReuse(currentMarker, signature)) return dir
        dir.deleteRecursively()
        dir.mkdirs()
        input.use { ins ->
            java.util.zip.ZipInputStream(ins).use { zin ->
                while (true) {
                    val entry = zin.nextEntry ?: break
                    val outFile = File(dir, entry.name)
                    if (entry.isDirectory) {
                        outFile.mkdirs()
                    } else {
                        outFile.parentFile?.mkdirs()
                        outFile.outputStream().use { out -> zin.copyTo(out) }
                    }
                    zin.closeEntry()
                }
            }
        }
        flag.writeText(signature)
        Log.i(TAG, "解压完成 → $dir (src=$signature)")
        return dir
    }

    /** app 内部后端地址（未启动时返回 null，上层回退演示库或提示） */
    fun baseUrl(): String = "http://127.0.0.1:$PORT"
}
