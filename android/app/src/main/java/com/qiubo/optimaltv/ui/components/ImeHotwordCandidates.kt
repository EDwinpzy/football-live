package com.qiubo.optimaltv.ui.components

import org.json.JSONObject

/**
 * 联网热词的候选匹配、排序、应用与载荷清洗（纯 Kotlin，不碰 native / Android 上下文，
 * 因此可被 JVM 单测直接覆盖）。
 *
 * 匹配分级（先全拼后首字母，命中越紧越靠前）：
 *  ① 全拼精确 ② 全拼前缀 ③ 首字母精确 ④ 首字母前缀 ⑤ 全拼包含
 *  单字母查询跳过首字母两级——「s」作首字母能命中几千个片名，纯噪声。
 * 同级内：影视类优先于通用词（general），再比权重，最后比片名长度。
 */
object ImeHotwordCandidates {

    /** 影视类 kind（后端 ime_hotwords.py 的 KINDS）。通用词 kind=general，排序垫底。 */
    private val FILM_KINDS = setOf("movie", "tv", "anime", "variety", "short")
    private const val GENERAL_KIND = "general"
    private const val MAX_TEXT_LEN = 40
    private const val MAX_PINYIN_LEN = 64

    data class HotwordEntry(
        val text: String,
        val pinyin: String,
        val initials: String,
        val kind: String,
        val weight: Int,
    )

    /** 一次成功拉取的完整快照；entries 已清洗去重。 */
    data class Snapshot(
        val entries: List<HotwordEntry>,
        val updatedAt: Double,
        val source: String,
        val stale: Boolean,
    )

    /** 按 [query] 排热词候选（返回词条原文，供 UI 替换尾部拼音段）。 */
    fun rank(query: String, entries: List<HotwordEntry>, limit: Int): List<String> {
        if (limit <= 0) return emptyList()
        val q = normalizeLetters(query)
        if (q.isEmpty()) return emptyList()
        // 单字母查首字母会命中几千个片名，纯噪声
        val useInitials = q.length >= 2
        val scored = ArrayList<Pair<Int, HotwordEntry>>(entries.size)
        for (e in entries) {
            val tier = when {
                e.pinyin == q -> 0
                e.pinyin.startsWith(q) -> 1
                useInitials && e.initials == q -> 2
                useInitials && e.initials.startsWith(q) -> 3
                e.pinyin.contains(q) -> 4
                else -> -1
            }
            if (tier >= 0) scored.add(tier to e)
        }
        val ranker = compareBy<Pair<Int, HotwordEntry>> { it.first }
            .thenBy { if (FILM_KINDS.contains(it.second.kind)) 0 else 1 }
            .thenByDescending { it.second.weight }
            .thenBy { it.second.text.length }
        val out = LinkedHashSet<String>()
        for ((_, e) in scored.sortedWith(ranker)) {
            out.add(e.text)
            if (out.size >= limit) break
        }
        return out.toList()
    }

    /**
     * 应用热词候选：候选是整部片名，若 [query] 里已上屏的中文正好是它的前缀就吞掉，
     * 避免「爱情gongyu」选「爱情公寓」得到「爱情爱情公寓」。
     *
     * 与 [PinyinIme.applyCandidate] 必须分开：普通 native 候选一律追加，
     * 套用重叠会吞掉真实字符（「人人ren」选「人」应得「人人人」）。
     */
    fun applyHotword(query: String, candidate: String): String {
        var start = query.length
        while (start > 0 && query[start - 1] in 'a'..'z') start--
        val head = query.substring(0, start)
        // 候选比已上屏前缀长、且前 head.length 个字与 head 完全一致时才吞
        return if (head.isNotEmpty() && candidate.length > head.length && candidate.startsWith(head)) {
            candidate
        } else {
            head + candidate
        }
    }

    /** 清洗远端词表：去空白、转小写、丢非法条目、按 text 去重（保留首条）。 */
    fun clean(entries: List<HotwordEntry>): List<HotwordEntry> {
        val out = ArrayList<HotwordEntry>(entries.size)
        val seen = HashSet<String>()
        for (raw in entries) {
            val text = raw.text.trim()
            if (text.isEmpty() || text.length > MAX_TEXT_LEN) continue
            if (!text.any(::isCjk)) continue
            // 拼音位只允许字母数字（数字来自「三体2」这类片名后缀）。含空格/标点的读音
            // 整条丢弃：剥掉分隔符会把「ai qing」悄悄变成另一个词「aiqing」，比丢弃更糟。
            val pinyin = raw.pinyin.trim().lowercase()
            val initials = raw.initials.trim().lowercase()
            if (!pinyin.all(::isAlnum) || !initials.all(::isAlnum)) continue
            if (pinyin.isEmpty() || pinyin.length > MAX_PINYIN_LEN) continue
            if (initials.isEmpty() || initials.length > pinyin.length) continue
            val kind = raw.kind.trim().lowercase().ifEmpty { GENERAL_KIND }
            if (!seen.add(text)) continue
            out.add(HotwordEntry(text, pinyin, initials, kind, raw.weight.coerceAtLeast(0)))
        }
        return out
    }

    /**
     * 解析 `GET {base}/api/ime/hotwords` 响应。
     * 结构异常或清洗后为空都返回 null，调用方据此保留旧快照（绝不用空数据覆盖）。
     */
    fun parseResponse(body: String): Snapshot? {
        val root = try {
            JSONObject(body)
        } catch (e: Exception) {
            return null
        }
        val arr = root.optJSONArray("words") ?: return null
        val raw = ArrayList<HotwordEntry>(arr.length())
        for (i in 0 until arr.length()) {
            val row = arr.optJSONObject(i) ?: continue
            raw.add(
                HotwordEntry(
                    text = row.optString("text"),
                    pinyin = row.optString("pinyin"),
                    initials = row.optString("initials"),
                    kind = row.optString("kind", GENERAL_KIND),
                    weight = row.optInt("weight", 0),
                ),
            )
        }
        val entries = clean(raw)
        if (entries.isEmpty()) return null
        val updatedAt = root.optDouble("updated_at", 0.0)
            .let { if (it.isNaN() || it.isInfinite()) 0.0 else it }
        return Snapshot(entries, updatedAt, root.optString("source"), root.optBoolean("stale", true))
    }

    /** 查询串归一：只保留 [a-z0-9] 并转小写（用户可能打出空格/横杠分隔音节）。 */
    private fun normalizeLetters(value: String): String {
        val sb = StringBuilder(value.length)
        for (c in value) {
            when {
                c in 'a'..'z' -> sb.append(c)
                c in 'A'..'Z' -> sb.append(c.lowercaseChar())
                c in '0'..'9' -> sb.append(c)
            }
        }
        return sb.toString()
    }

    private fun isCjk(c: Char): Boolean = c.code in 0x3400..0x9FFF || c.code in 0xF900..0xFAFF

    private fun isAlnum(c: Char): Boolean =
        c in 'a'..'z' || c in '0'..'9'
}
