package com.qiubo.optimaltv.data.source

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Resolver 重排顺序契约（纯函数层，锁住「谁先被播」的决定）。
 *
 * 这段排序直接决定观众点播放后看到哪条线：preferred 被挤到后面，等于云端预匹配
 * 的收益完全归零；候选误匹配到别的集，则是**错集**。两者都是纯计算错误，
 * 没必要等真机点播才发现——所以在这里钉死。
 */
class ResolverRankingTest {

    private fun cand(source: String, content: String) =
        ResolvedSource(sourceId = source, contentId = content)

    @Test
    fun preferredComesFirstThenBackupsInGivenOrder() {
        val prefixes = listOf("s:a", "s:b", "s:c", "s:d")
        val order = ResolverRanking.order(
            linePrefixes = prefixes,
            preferred = cand("s", "c"),
            backups = listOf(cand("s", "a"), cand("s", "b")),
        )
        // preferred 必须在第一个；backups 保持后端给的 a→b 次序。
        // 反例（修复前）：backups 用 insert(0,…) 逐个前插，b 会被顶到 preferred 前面。
        assertEquals(listOf(2, 0, 1, 3), order)
    }

    @Test
    fun firstBackupDoesNotJumpAheadOfPreferred() {
        // 最小复现：preferred 在中间，backups 只有一个。修复前会返回 [1, 0]。
        val order = ResolverRanking.order(
            linePrefixes = listOf("s:x", "s:pref"),
            preferred = cand("s", "pref"),
            backups = listOf(cand("s", "x")),
        )
        assertEquals(listOf(1, 0), order)
    }

    @Test
    fun unnamedLinesKeepOriginalOrderAtTheTail() {
        // Resolver 只是排序层：没被点名的线路一条都不能丢，且保持原相对顺序。
        val order = ResolverRanking.order(
            linePrefixes = listOf("s:a", "s:z", "s:b", "s:y"),
            preferred = cand("s", "b"),
            backups = emptyList(),
        )
        assertEquals(listOf(2, 0, 1, 3), order)
    }

    @Test
    fun candidatesAbsentFromDetailAreSimplyDropped() {
        // 云端有候选但端侧 detail 没有这条线 → 跳过它，不能凭空造出可播线路。
        val order = ResolverRanking.order(
            linePrefixes = listOf("s:a", "s:b"),
            preferred = cand("s", "ghost"),
            backups = listOf(cand("s", "b")),
        )
        assertEquals(listOf(1, 0), order)
    }

    @Test
    fun matchingIsSegmentwiseNotRawPrefix() {
        // content-1 不得前缀命中 content-10 —— 那是**另一集**的线路，
        // 送过去既是错集，也往往正好是不可播的那条。
        val prefixes = listOf("src:content-10", "src:content-1")
        val order = ResolverRanking.order(
            linePrefixes = prefixes,
            preferred = cand("src", "content-1"),
            backups = emptyList(),
        )
        assertEquals(listOf(1, 0), order)
    }

    @Test
    fun differentSourceWithSameContentIdIsNotMatched() {
        // 只比 contentId 会跨源串线：不同站点的同名 id 完全可能指不同影片。
        val order = ResolverRanking.order(
            linePrefixes = listOf("aaa:vid-1", "bbb:vid-1"),
            preferred = cand("bbb", "vid-1"),
            backups = emptyList(),
        )
        assertEquals(listOf(1, 0), order)
    }

    @Test
    fun noCandidatesLeavesOrderUntouched() {
        val prefixes = listOf("s:a", "s:b", "s:c")
        assertEquals(listOf(0, 1, 2), ResolverRanking.order(prefixes, null, emptyList()))
    }

    @Test
    fun malformedOrBlankPrefixesAreNeverMatched() {
        // 解析不出 sourceId/contentId 两段的行（直链线路只有裸 url）不能被候选误认，
        // 否则会把一条无 playRef 的线路顶到首位。
        // 注意 "s:only-one" 本身是**合法**的两段前缀，应当被 its candidate 命中；
        // 真正不可匹配的是空串和只有一段的 "no-colon"。
        val order = ResolverRanking.order(
            linePrefixes = listOf("", "no-colon", "s:ok"),
            preferred = cand("s", "only-one"),
            backups = listOf(cand("s", "ok")),
        )
        // preferred 无对应线路被丢弃；backup 命中 index 2；两条不可匹配线路按原序补在后面。
        assertEquals(listOf(2, 0, 1), order)
    }

    @Test
    fun emptyLineListIsHandled() {
        assertEquals(emptyList<Int>(), ResolverRanking.order(emptyList(), cand("s", "a"), listOf(cand("s", "b"))))
    }

    /* ---------- matchesCandidate：L0（last_good 记忆命中）复用同一条匹配规则 ---------- */

    @Test
    fun matchesCandidateIsSegmentwise() {
        assertTrue(ResolverRanking.matchesCandidate("s:content-1", "s", "content-1"))
        // 经典错集陷阱：content-1 不得匹配 content-10
        assertFalse(ResolverRanking.matchesCandidate("s:content-10", "s", "content-1"))
        // 跨源不匹配
        assertFalse(ResolverRanking.matchesCandidate("other:vid-1", "s", "vid-1"))
        // 缺段 / 空串永不匹配
        assertFalse(ResolverRanking.matchesCandidate("", "s", "vid-1"))
        assertFalse(ResolverRanking.matchesCandidate("nocolon", "s", "vid-1"))
    }
}
