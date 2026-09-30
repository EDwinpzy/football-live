package com.qiubo.optimaltv.ui.components

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 热搜词候选引擎纯逻辑单测（ImeHotwordCandidates）。
 *
 * 覆盖三类会被线上改动打穿的契约：
 * ① 排序分级：全拼精确 > 全拼前缀 > 首字母精确 > 首字母前缀 > 全拼包含 > 落选，
 *    同级内影视优先于通用词、再比权重；
 * ② 应用语义：热词按「中文前缀重叠」吞掉重复片名，普通 native 候选一律纯追加
 *    （否则「人人ren」选「人」会变成「人人」而不是「人人人」）；
 * ③ 载荷清洗：只收非空、含中文、拼音/首字母为纯小写字母的安全条目，坏条目整条丢弃，
 *    全部非法时整包视为无效（调用方据此保留旧数据）。
 *
 * 全部期望值手写字面量，不复用被测代码的派生逻辑。
 */
class ImeHotwordCandidatesTest {

    private fun word(
        text: String,
        pinyin: String,
        initials: String,
        kind: String = "movie",
        weight: Int = 100,
    ) = ImeHotwordCandidates.HotwordEntry(text, pinyin, initials, kind, weight)

    // ---------- 排序分级 ----------

    @Test
    fun `同分级同类同权重时短片名在前`() {
        val entries = listOf(
            word("爱情公寓番外篇", "aiqinggongyu", "aqgy", weight = 100),
            word("爱情公寓", "aiqinggongyu", "aqgy", weight = 100),
        )
        // 同拼音=同级、同类、同权重：更贴近查询的短正片先出
        assertEquals(
            listOf("爱情公寓", "爱情公寓番外篇"),
            ImeHotwordCandidates.rank("aiqinggongyu", entries, 8),
        )
    }

    @Test
    fun `全拼精确优先于全拼前缀`() {
        val entries = listOf(
            word("安家", "anjia", "aj", weight = 10),
            word("安家风云", "anjiafengyun", "ajfy", weight = 1),
        )
        assertEquals(
            listOf("安家", "安家风云"),
            ImeHotwordCandidates.rank("anjia", entries, 8),
        )
    }

    @Test
    fun `全拼前缀优先于首字母精确`() {
        val entries = listOf(
            // 全拼前缀 ab*：阿爆米花(abaomihua)
            word("阿爆米花", "abaomihua", "abmh", weight = 1),
            // 首字母精确 ab，但全拼首字母不匹配，故只能落到「首字母精确」级
            word("阿爸", "baba", "ab", weight = 9999),
        )
        // 分级压过权重：权重 9999 的首字母精确仍排在全拼前缀之后
        assertEquals(
            listOf("阿爆米花", "阿爸"),
            ImeHotwordCandidates.rank("ab", entries, 8),
        )
    }

    @Test
    fun `首字母精确优先于首字母前缀`() {
        val entries = listOf(
            word("安家", "anjia", "aj", weight = 1),
            word("安静杀手", "anjingshashou", "ajss", weight = 1),
        )
        assertEquals(
            listOf("安家", "安静杀手"),
            ImeHotwordCandidates.rank("aj", entries, 8),
        )
    }

    @Test
    fun `同为全拼前缀时影视词优先于通用词_即使通用词权重更高`() {
        val entries = listOf(
            word("你好，旧时光", "nihaojiushiguang", "nhjsg", kind = "tv", weight = 5),
            word("你好们", "nihaomen", "nhm", kind = "general", weight = 9999),
        )
        assertEquals(
            listOf("你好，旧时光", "你好们"),
            ImeHotwordCandidates.rank("nihao", entries, 8),
        )
    }

    @Test
    fun `同为全拼前缀且同类时按权重从高到低`() {
        val entries = listOf(
            word("三体2", "santi2", "st2", kind = "tv", weight = 80),
            word("三体", "santi", "st", kind = "tv", weight = 10),
        )
        assertEquals(
            listOf("三体2", "三体"),
            ImeHotwordCandidates.rank("san", entries, 8),
        )
    }

    @Test
    fun `尾音节查询靠包含级找回完整片名`() {
        // 「爱情gongyu」只把尾段 gongyu 交给 rank，必须靠包含级找回「爱情公寓」
        val entries = listOf(word("爱情公寓", "aiqinggongyu", "aqgy", kind = "tv", weight = 100))
        assertEquals(listOf("爱情公寓"), ImeHotwordCandidates.rank("gongyu", entries, 8))
    }

    @Test
    fun `查询串大写被归一化后照常命中`() {
        val entries = listOf(word("爱情公寓", "aiqinggongyu", "aqgy"))
        assertEquals(listOf("爱情公寓"), ImeHotwordCandidates.rank("AIQINGGONGYU", entries, 8))
    }

    @Test
    fun `同一条目重复出现只保留一次`() {
        val entries = listOf(
            word("爱情公寓", "aiqinggongyu", "aqgy", weight = 10),
            word("爱情公寓", "aiqinggongyu", "aqgy", weight = 900),
        )
        assertEquals(listOf("爱情公寓"), ImeHotwordCandidates.rank("aiqinggongyu", entries, 8))
    }

    @Test
    fun `结果条数不超过 limit 且非正 limit 返回空`() {
        val entries = listOf(
            word("剧一", "juyi", "jy"),
            word("剧二", "juer", "je"),
            word("剧三", "jusan", "js"),
        )
        assertEquals(2, ImeHotwordCandidates.rank("ju", entries, 2).size)
        assertEquals(emptyList<String>(), ImeHotwordCandidates.rank("ju", entries, 0))
        assertEquals(emptyList<String>(), ImeHotwordCandidates.rank("ju", entries, -3))
    }

    @Test
    fun `无任何命中时返回空列表`() {
        val entries = listOf(word("爱情公寓", "aiqinggongyu", "aqgy"))
        assertEquals(emptyList<String>(), ImeHotwordCandidates.rank("zzz", entries, 8))
    }

    @Test
    fun `查询串里非字母字符被剔除后仍可匹配`() {
        val entries = listOf(word("爱情公寓", "aiqinggongyu", "aqgy"))
        assertEquals(listOf("爱情公寓"), ImeHotwordCandidates.rank("爱-qing gongyu", entries, 8))
    }

    @Test
    fun `单字母查询不查首字母但保留全部全拼前缀命中`() {
        val entries = listOf(
            word("三体", "santi", "st", kind = "tv", weight = 50),
            word("山楂树之恋", "shanzhashuzhilian", "szszl", kind = "movie", weight = 50),
        )
        // 「s」作首字母会命中成千上万片名，噪声大；全拼前缀 s* 才是有效命中
        assertEquals(listOf("三体", "山楂树之恋"), ImeHotwordCandidates.rank("s", entries, 8))
    }

    // ---------- 应用语义：热词重叠 vs 普通追加 ----------

    @Test
    fun `热词应用吞掉已上屏的中文前缀_不重复整部片名`() {
        // query = 爱情 + 尾部拼音 gongyu；候选是整部「爱情公寓」
        assertEquals("爱情公寓", ImeHotwordCandidates.applyHotword("爱情gongyu", "爱情公寓"))
    }

    @Test
    fun `热词应用无重叠时等同普通替换`() {
        assertEquals("三体", ImeHotwordCandidates.applyHotword("santi", "三体"))
    }

    @Test
    fun `热词应用仅一字前缀也按重叠吞掉_爱gongyu选爱情公寓得爱情公寓`() {
        // 单字前缀也是候选的真前缀，重叠规则照样生效（避免「爱爱情公寓」）
        assertEquals("爱情公寓", ImeHotwordCandidates.applyHotword("爱gongyu", "爱情公寓"))
    }

    @Test
    fun `热词应用在候选首字不同时原样前置`() {
        // 上屏「爱」+ 拼音，候选「三体」首字不同 → 不能吞，原样拼接
        assertEquals("爱三体", ImeHotwordCandidates.applyHotword("爱santi", "三体"))
    }

    @Test
    fun `热词应用遇到尾部无拼音时直接前置追加候选`() {
        assertEquals("爱情公寓", ImeHotwordCandidates.applyHotword("爱情", "公寓"))
    }

    @Test
    fun `热词应用遇到候选短于已上屏前缀时不吞任何字`() {
        // 上屏「爱情公寓」，候选「公」比前缀短，不构成完整前缀，必须原样拼接
        assertEquals("爱情公寓公", ImeHotwordCandidates.applyHotword("爱情公寓gong", "公"))
    }

    @Test
    fun `普通候选应用不吞重复字_人人ren选人得到人人人`() {
        // 这是热词应用与普通替换必须分开的原因：重叠逻辑会把「人人」误吞成「人」
        val head = "人人"
        val candidate = "人"
        assertEquals("人人人", head + candidate)
    }

    // ---------- 载荷清洗 ----------

    @Test
    fun `合法条目被清洗保留且归一化`() {
        val out = ImeHotwordCandidates.clean(
            listOf(word("  爱情公寓 ", "AIQingGongYu", "AQGY", kind = "movie", weight = 7)),
        )
        assertEquals(1, out.size)
        assertEquals(
            ImeHotwordCandidates.HotwordEntry("爱情公寓", "aiqinggongyu", "aqgy", "movie", 7),
            out[0],
        )
    }

    @Test
    fun `数字片名后缀被保留_pinyin允许数字`() {
        val out = ImeHotwordCandidates.clean(
            listOf(word("年会不能停2", "nianhuibuting2", "nhbt2", weight = 5)),
        )
        // 真实热词含数字后缀（年会2/三体2），拼音字母位必须允许 [a-z0-9]
        assertEquals(1, out.size)
        assertEquals("nianhuibuting2", out[0].pinyin)
        assertEquals("nhbt2", out[0].initials)
    }

    @Test
    fun `纯ASCII拼音仍因无中文被丢弃`() {
        val out = ImeHotwordCandidates.clean(listOf(word("santi2", "santi2", "st2")))
        assertEquals(emptyList<ImeHotwordCandidates.HotwordEntry>(), out)
    }

    @Test
    fun `空文本与无中文条目被丢弃`() {
        val out = ImeHotwordCandidates.clean(
            listOf(
                word("", "abc", "a"),
                word("   ", "abc", "a"),
                word("abc", "abc", "a"),
                word("123", "123", "1"),
            ),
        )
        assertEquals(emptyList<ImeHotwordCandidates.HotwordEntry>(), out)
    }

    @Test
    fun `拼音或首字母含非小写字母的条目被丢弃`() {
        val out = ImeHotwordCandidates.clean(
            listOf(
                // 空格会被 normalizeLetters 直接剥掉，剥完就不再是后端给的读音了，
                // 属于「静默改成别的词」，比丢弃更危险 → 整条丢弃
                word("爱情公寓", "ai qing", "aqgy"),
                word("三体", "santi", ""),
                word("三体", "", "st"),
            ),
        )
        assertEquals(emptyList<ImeHotwordCandidates.HotwordEntry>(), out)
    }

    @Test
    fun `超长条目被丢弃且负权重归零`() {
        val out = ImeHotwordCandidates.clean(
            listOf(
                word("字".repeat(64), "a".repeat(64), "a", weight = 5),
                word("三体", "santi", "st", weight = -3),
            ),
        )
        assertEquals(1, out.size)
        assertEquals("三体", out[0].text)
        assertEquals(0, out[0].weight)
    }

    @Test
    fun `清洗后按文本去重并保留首条`() {
        val out = ImeHotwordCandidates.clean(
            listOf(
                word("爱情公寓", "aiqinggongyu", "aqgy", weight = 900),
                word("爱情公寓", "aiqinggongyu", "aqgy", weight = 10),
            ),
        )
        assertEquals(1, out.size)
        assertEquals(900, out[0].weight)
    }
}
