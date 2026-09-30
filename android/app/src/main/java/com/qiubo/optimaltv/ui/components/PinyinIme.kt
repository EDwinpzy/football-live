package com.qiubo.optimaltv.ui.components

import android.content.Context
import android.util.Log
import java.io.BufferedReader
import java.io.InputStreamReader
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPInputStream
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * TV Dpad 拼音输入法引擎（2026-08-29 重写）：
 * - 主引擎：AOSP 谷歌拼音输入法 native 解码器（Apache-2.0，cpp/pinyinime + 1MB 系统词典），
 *   支持音节歧义切分与长句组词（aiqinggongyu → 爱情公寓），候选质量与当年谷歌输入法一致；
 * - 兜底：so 加载/初始化失败时回退旧版词典引擎（assets/pinyin_ime.dictz 前缀二分 + 单字组词）。
 * 解码器为进程级单例，Java 侧已串行化；这里只做薄封装。
 */
object PinyinIme {
    private const val TAG = "PinyinIme"
    private const val ASSET = "pinyin_ime.dictz"   // gzip 内容；不能用 .gz 后缀（aapt 打包会自动解压并去后缀）
    private const val MAX_SCAN = 400          // 前缀区间最多扫描行数
    private const val SYLL_MAX = 6            // 拼音音节最大长度

    @Volatile
    private var lines: Array<String>? = null

    @Volatile
    private var loadFailed = false

    /** 词表是否就绪（native 或兜底任一可用） */
    fun ready(): Boolean = nativeReady || lines != null

    private val nativeReady: Boolean
        get() = !com.android.inputmethod.pinyin.PinyinDecoderService.isLoadFailed() &&
            com.android.inputmethod.pinyin.PinyinDecoderService.isReady()

    /**
     * 初始化（IO 线程调用一次）：优先 native 解码器，失败降级旧词典。
     */
    fun load(context: Context): Boolean {
        contextRef = context.applicationContext
        try {
            com.android.inputmethod.pinyin.PinyinDecoderService.init(contextRef!!)
            if (nativeReady) {
                Log.d(TAG, "native decoder ready")
                return true
            }
        } catch (e: Exception) {
            Log.w(TAG, "native decoder init failed: ${e.message}")
        }
        return loadDictFallback()
    }

    /** 旧版词典引擎加载（兜底路径，~3.8MB gz） */
    private fun loadDictFallback(): Boolean {
        if (lines != null || loadFailed) return lines != null
        synchronized(this) {
            if (lines != null || loadFailed) return lines != null
            return try {
                val t0 = System.currentTimeMillis()
                val text = BufferedReader(
                    InputStreamReader(GZIPInputStream(contextRef!!.assets.open(ASSET)), Charsets.UTF_8),
                ).use { it.readText() }
                lines = text.split('\n').filter { it.isNotEmpty() }.toTypedArray()
                Log.d(TAG, "dict fallback loaded: ${lines!!.size} lines in ${System.currentTimeMillis() - t0}ms")
                true
            } catch (e: Exception) {
                Log.w(TAG, "dict load failed: ${e.message}")
                loadFailed = true
                false
            }
        }
    }

    // 兜底引擎需要 Context；load() 后持有 application 引用（与 App 同生命周期）
    @Volatile
    private var contextRef: Context? = null

    /** 拼音串 → 候选词列表（已按引擎内部词频排序） */
    fun candidates(raw: String, limit: Int = 12): List<String> {
        val q = raw.lowercase().filter { it in 'a'..'z' }
        if (q.isEmpty()) return emptyList()
        if (nativeReady) {
            val out = LinkedHashSet<String>(limit * 2)
            val n = com.android.inputmethod.pinyin.PinyinDecoderService.search(q)
            var i = 0
            while (i < n && out.size < limit) {
                val c = com.android.inputmethod.pinyin.PinyinDecoderService.getChoice(i)
                if (c.isNullOrBlank()) break
                out.add(c.trim())
                i++
            }
            return out.toList()
        }
        return candidatesFallback(q, limit)
    }

    /** 引擎切分后的拼音显示串（如 "ai qing gong yu"）；native 不可用返回原串 */
    fun segmentedPinyin(raw: String): String {
        val q = raw.lowercase().filter { it in 'a'..'z' }
        if (q.isEmpty()) return q
        if (nativeReady) {
            com.android.inputmethod.pinyin.PinyinDecoderService.search(q)
            return com.android.inputmethod.pinyin.PinyinDecoderService.getPyStr(true) ?: q
        }
        return q
    }

    /** 从查询串中取尾部拼音输入段（[a-z] 连续段），供候选替换 */
    fun trailingSegment(query: String): String {
        var end = query.length
        while (end > 0 && query[end - 1] in 'a'..'z') end--
        return query.substring(end)
    }

    /** 用候选词替换尾部拼音段 */
    fun applyCandidate(query: String, candidate: String): String {
        val end = query.length
        var start = end
        while (start > 0 && query[start - 1] in 'a'..'z') start--
        return query.substring(0, start) + candidate
    }

    // ---------------- 首字母联想（2026-09-05 用户需求：aqgy→爱情公寓 / heysn→花儿与少年） ----------------
    // native 谷歌拼音解码器只支持全拼组词，不支持首字母缩写；用构建期生成的
    // assets/pinyin_initials.txt（tools/gen_pinyin_initials.py，pypinyin 权威读音，GB2312
    // 6763 字全覆盖）加载「汉字→拼音首字母」映射，供搜索页对片名建首字母索引做前缀匹配。
    // （运行期 dictz 反查方案已否决：词典无"寓"等字独立音节行、词组行污染映射。）

    private const val INITIALS_ASSET = "pinyin_initials.txt"

    @Volatile
    private var initials: Map<Char, Char>? = null

    fun initialsReady(): Boolean = initials != null

    /** 加载「汉字→拼音首字母」映射（IO 线程调用一次；~40KB 文本，毫秒级） */
    fun loadInitials(): Boolean {
        if (initials != null) return true
        synchronized(this) {
            if (initials != null) return true
            return try {
                val map = HashMap<Char, Char>(8192)
                contextRef!!.assets.open(INITIALS_ASSET).bufferedReader(Charsets.UTF_8).useLines { seq ->
                    for (line in seq) {
                        if (line.length >= 2) map[line[0]] = line[1]
                    }
                }
                initials = map
                Log.d(TAG, "initials map loaded: ${map.size} chars")
                true
            } catch (e: Exception) {
                Log.w(TAG, "initials asset load failed: ${e.message}")
                false
            }
        }
    }

    /** 片名 → 拼音首字母串：汉字查表、英文字母保留小写、其余字符跳过；表未就绪返回空串 */
    fun titleInitials(title: String): String {
        val map = initials ?: return ""
        val sb = StringBuilder(title.length)
        for (c in title) {
            when {
                c in 'a'..'z' -> sb.append(c)
                c in 'A'..'Z' -> sb.append(c.lowercaseChar())
                c.code > 127 -> map[c]?.let { sb.append(it) }
                // 数字/标点/空格：跳过（不参与首字母匹配）
            }
        }
        return sb.toString()
    }

    // ---------------- 片名首字母索引（联想数据源 = hhkan 全站可播内容快照） ----------------
    // assets/pinyin_titles.txt 由 tools/gen_pinyin_titles.py 构建期抓取生成（6 分类 show
    // 分页全量 ~1800 条，每行「首字母串<TAB>片名」）；与可播内容精确对齐（联想出的都能播）。

    private const val TITLES_ASSET = "pinyin_titles.txt"

    @Volatile
    private var titleIndex: List<Pair<String, String>>? = null

    fun titleIndexReady(): Boolean = titleIndex != null

    /** 加载片名首字母索引（IO 线程调用一次；~2k 行毫秒级） */
    fun loadTitleIndex(): Boolean {
        if (titleIndex != null) return true
        synchronized(this) {
            if (titleIndex != null) return true
            return try {
                val list = ArrayList<Pair<String, String>>(2048)
                contextRef!!.assets.open(TITLES_ASSET).bufferedReader(Charsets.UTF_8).useLines { seq ->
                    for (line in seq) {
                        val i = line.indexOf('\t')
                        if (i > 1) list.add(line.substring(0, i) to line.substring(i + 1))
                    }
                }
                titleIndex = list
                Log.d(TAG, "title index loaded: ${list.size} rows")
                true
            } catch (e: Exception) {
                Log.w(TAG, "title index load failed: ${e.message}")
                false
            }
        }
    }

    /**
     * 首字母联想匹配（2026-09-05 需求⑩优化，策略对齐 GitHub TinyPinyin/PinIn 类库的
     * 常见匹配层级）：三级命中——
     * ① 首字母串完全相等（aqgy = 爱情公寓）② 前缀（aqg → 爱情公寓）
     * ③ 包含（qgy → 爱情公寓，命中位置任意）
     * 同级按「首字母串更短优先」（与查询更贴近）→「片名更短优先」（短词在前更符合
     * 联想直觉）排序；全表 ~2k 行线性扫毫秒级。输入 ≥2 字母防单字母噪声。
     */
    fun matchByInitials(query: String, limit: Int): List<String> {
        val idx = titleIndex ?: return emptyList()
        if (query.length < 2) return emptyList()
        var exact: Pair<String, String>? = null
        val prefixHits = ArrayList<Pair<String, String>>()
        val containsHits = ArrayList<Pair<String, String>>()
        for (row in idx) {
            when {
                row.first == query -> if (exact == null) exact = row
                row.first.startsWith(query) -> prefixHits.add(row)
                row.first.contains(query) -> containsHits.add(row)
            }
        }
        val rank = compareBy<Pair<String, String>> { it.first.length }.thenBy { it.second.length }
        return buildList {
            exact?.let { add(it.second) }
            addAll(prefixHits.sortedWith(rank).map { it.second })
            addAll(containsHits.sortedWith(rank).map { it.second })
        }.distinct().take(limit)
    }

    // ---------------- 旧版词典引擎（兜底，原实现保留） ----------------

    private fun keyOf(line: String): String {
        val i = line.indexOf(':')
        return if (i < 0) line else line.substring(0, i)
    }

    private fun wordsOf(line: String): List<String> {
        val i = line.indexOf(':')
        if (i < 0 || i + 1 >= line.length) return emptyList()
        return line.substring(i + 1).split(',')
    }

    private fun lowerBound(q: String): Int {
        val arr = lines ?: return 0
        var lo = 0
        var hi = arr.size
        while (lo < hi) {
            val mid = (lo + hi) / 2
            if (keyOf(arr[mid]) < q) lo = mid + 1 else hi = mid
        }
        return lo
    }

    private fun candidatesFallback(q: String, limit: Int): List<String> {
        val arr = lines ?: return emptyList()
        val out = LinkedHashSet<String>(limit * 2)
        var exact: MutableList<String>? = null
        var longer: MutableList<String>? = null
        var i = lowerBound(q)
        var scanned = 0
        while (i < arr.size && scanned < MAX_SCAN) {
            val line = arr[i]
            val k = keyOf(line)
            if (!k.startsWith(q)) break
            scanned++
            val bucket = wordsOf(line)
            if (k.length == q.length) {
                if (exact == null) exact = mutableListOf()
                if (exact.size < 40) exact.addAll(bucket)
            } else {
                if (longer == null) longer = mutableListOf()
                if (longer.size < 40) longer.addAll(bucket.take(4))
            }
            i++
        }
        exact?.let { out.addAll(it) }
        longer?.let { out.addAll(it) }
        if (q.length > 2) {
            composeBySyllables(q)?.let { out.add(it) }
        }
        return out.toList().take(limit)
    }

    private fun composeBySyllables(q: String): String? {
        val arr = lines ?: return null
        val sylls = mutableListOf<String>()
        var pos = 0
        while (pos < q.length) {
            var take = 0
            for (len in minOf(SYLL_MAX, q.length - pos) downTo 1) {
                val syl = q.substring(pos, pos + len)
                val idx = lowerBound(syl)
                if (idx < arr.size && keyOf(arr[idx]) == syl) {
                    take = len
                    sylls.add(syl)
                    break
                }
            }
            if (take == 0) return null
            pos += take
        }
        if (sylls.size < 2) return null
        val sb = StringBuilder()
        for (syl in sylls) {
            val idx = lowerBound(syl)
            val w = wordsOf(arr[idx]).firstOrNull() ?: return null
            sb.append(w.firstOrNull() ?: return null)
        }
        return sb.toString()
    }

    // ---------------- 整词续写 + 联网热词（2026-09-30 用户需求） ----------------
    // 两路数据源并进同一条候选栏：
    // ① native predicts 整词续写：已上屏「爱情」→ 追加后缀「公寓」，UI 直接 query + cand；
    // ② 后端 /api/ime/hotwords 热词：把「爱情gongyu」补成整部片名，
    //    走 applyHotwordCandidate 的前缀重叠；套普通替换会得到「爱情爱情公寓」。
    // 排序/清洗/应用逻辑都在 ImeHotwordCandidates（纯 Kotlin，有 JVM 单测），
    // 这里只做 native 桥接与网络拉取。

    @Volatile
    private var hotwordSnapshot: ImeHotwordCandidates.Snapshot? = null

    /** 热词是否已成功拉取过。 */
    fun hotwordsReady(): Boolean = hotwordSnapshot != null

    private val hotwordHttp by lazy {
        OkHttpClient.Builder()
            .connectTimeout(4, TimeUnit.SECONDS)
            .readTimeout(6, TimeUnit.SECONDS)
            .callTimeout(8, TimeUnit.SECONDS)
            .build()
    }

    /** 热词响应上限：正常 ~800 条约 100KB，1MiB 足够宽松，超出即视为异常响应丢弃。 */
    private const val HOTWORDS_MAX_BYTES = 1L shl 20

    /**
     * 拉取热词并整体替换本地快照。**失败或返回空一律 false 并保留旧数据**——
     * 冷启动时后端可能还没起好，词表丢了会退化成只有 native 候选。
     * 内部已切到 IO 线程，调用方无需再包一层。
     */
    suspend fun refreshHotwords(baseUrl: String): Boolean = withContext(Dispatchers.IO) {
        val base = baseUrl.trim().trimEnd('/')
        if (base.isEmpty()) return@withContext false
        val body = try {
            val req = Request.Builder()
                .url("$base/api/ime/hotwords")
                .header("User-Agent", com.qiubo.optimaltv.data.net.HttpUa.BROWSER)
                .header("Accept", com.qiubo.optimaltv.data.net.HttpUa.ACCEPT)
                .build()
            hotwordHttp.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return@use null
                val source = r.body?.source() ?: return@use null
                // 限长读取：多读一个字节用于判断超限，避免被超大响应撑爆内存
                source.request(HOTWORDS_MAX_BYTES + 1)
                if (source.buffer.size > HOTWORDS_MAX_BYTES) {
                    Log.w(TAG, "hotwords response too large, dropped")
                    return@use null
                }
                source.buffer.snapshot().utf8()
            }
        } catch (e: Exception) {
            Log.w(TAG, "hotwords fetch failed: ${e.message}")
            null
        } ?: return@withContext false

        val parsed = ImeHotwordCandidates.parseResponse(body) ?: return@withContext false
        hotwordSnapshot = parsed
        Log.d(TAG, "hotwords updated: ${parsed.entries.size} words from ${parsed.source}")
        true
    }

    /**
     * 尾部拼音段 → 整部片名候选（热词）。返回词条原文，配合 [applyHotwordCandidate] 用。
     * 还没拉到热词时返回空列表，UI 自然退回 initials + native 候选。
     */
    fun hotwordCandidates(raw: String, limit: Int = 8): List<String> {
        val snap = hotwordSnapshot ?: return emptyList()
        return ImeHotwordCandidates.rank(raw, snap.entries, limit)
    }

    /**
     * 整词续写候选。**返回的是后缀**：已上屏「爱情」拿到「公寓」，UI 直接 `query + cand`
     * 追加，不要再替换尾部拼音段。顺序：热词 → 离线片名表 → native predicts。
     * 离线片名单排在 native 前，保证后端没起来时也有片名续写。
     */
    fun predictions(history: String, limit: Int = 24): List<String> {
        val h = history.trim()
        if (h.isEmpty() || limit <= 0) return emptyList()
        val out = LinkedHashSet<String>()
        // native 预测缓冲按汉字历史设计，纯数字/空白/标点没有可预测内容，直接跳过
        if (h.any { it.code > 0x2E80 }) {
            // ① 热词续写：「爱情」→「公寓」
            val snap = hotwordSnapshot
            if (snap != null) {
                for (e in snap.entries) {
                    if (e.text.length > h.length && e.text.startsWith(h)) {
                        out.add(e.text.substring(h.length))
                    }
                    if (out.size >= limit) break
                }
            }
            // ② 离线片名表续写（后端没起来时的兜底数据源）
            val idx = titleIndex
            if (idx != null) {
                for ((_, title) in idx) {
                    if (title.length > h.length && title.startsWith(h)) {
                        out.add(title.substring(h.length))
                    }
                    if (out.size >= limit) break
                }
            }
        }
        if (out.size < limit && nativeReady) {
            // ③ native 整词续写
            for (s in com.android.inputmethod.pinyin.PinyinDecoderService.getPredicts(h, limit)) {
                if (s.isNotEmpty()) out.add(s)
            }
        }
        return out.toList().take(limit)
    }

    /**
     * 应用热词候选：词条是整部片名，若已上屏的中文已是它的前缀就吞掉，避免重复整片。
     * 只对热词候选用；普通 native 候选请继续用 [applyCandidate]（纯追加）。
     */
    fun applyHotwordCandidate(query: String, candidate: String): String =
        ImeHotwordCandidates.applyHotword(query, candidate)
}
