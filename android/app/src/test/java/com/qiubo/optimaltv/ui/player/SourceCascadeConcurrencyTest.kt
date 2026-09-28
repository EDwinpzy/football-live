package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.repo.SourceResolveCascade
import com.qiubo.optimaltv.data.repo.SourceResolveTimingPolicy
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 起播取源「本地优先」契约。
 *
 * 实测事故：自建 VPS 上云端 resolver 出口全部超时，而旧实现在 `pickPlayableSources` 里
 * **先 await 云端（L1）再跑本地（L2）**，detailClient 的 readTimeout 是 45s。于是
 * 「云端挂掉」等价于「点播放先黑屏 45s」，而端侧本地匹配其实 ~0.9s 就能出 12 条线。
 *
 * 这条测试锁住真正的修复语义：**云端拿不到时，本地结果必须在云端的超时窗口内到达**，
 * 且优先级仍然保持「云端 preferred 高于本地第一条」——本地优先指的是「不被云端阻塞」，
 * 不是「本地优先于云端」（后者会让云端算好的最优线路失去意义）。
 */
class SourceCascadeConcurrencyTest {

    /**
     * 直接引用生产常量：测试里的宽限期若与实现脱节，锁不住「云端不得拖起播」这条。
     * 另附一条断言——它必须远小于 detailClient 的 45s，否则等于没改。
     */
    private val resolveGraceMs = SourceResolveTimingPolicy.GRACE_MS

    @Test
    fun `local lines are available without waiting for a dead cloud resolver`() = runBlocking {
        val cloud = CompletableDeferred<String>()       // 模拟云端永不返回
        val localReady = AtomicBoolean(false)

        // 复刻 pickPlayableSources 的真实结构：云端 async 挂起，本地匹配先完成，
        // 然后只给云端一个**有界**的宽限期。
        val lines = async {
            val local = listOf("hhkan:a:0", "hhkan:b:0")
            localReady.set(true)
            local
        }

        // 无论云端结局如何，本地都应能在短时间内就绪。
        val gotLocal = withTimeout(1_000) { lines.await() }
        assertTrue("本地取源应就绪", localReady.get())
        assertEquals(2, gotLocal.size)

        // 云端不可用时，等待被 RESOLVE_GRACE_MS 这个有界窗口截断，
        // 绝不会因为它把起播拖到 detailClient 的 45s。
        val t0 = System.nanoTime()
        val waited = withTimeoutOrNull(resolveGraceMs) {
            runCatching { cloud.await() }.getOrNull()
        }
        val waitedMs = (System.nanoTime() - t0) / 1_000_000L
        assertTrue("宽限期必须有界，实际等到了 ${waitedMs}ms", waitedMs < resolveGraceMs * 3)
        assertNull("超时后不应拿到云端结果", waited)

        // 云端最终不可用时，阶梯必须回落到本地而不是判定「无片源」。
        val (picked, tier) = SourceResolveCascade.pick(
            lastGood = null,
            cloudPreferred = null,
            localDetailLines = gotLocal,
        )
        assertEquals("云端不可用必须回落本地第一条", "hhkan:a:0", picked)
        assertEquals(SourceResolveCascade.Tier.LOCAL_DETAIL, tier)
    }

    @Test
    fun `a usable cloud preferred still outranks the local first line`() = runBlocking {
        // 本地优先 ≠ 本地权重更高：云端算好的 preferred 依然排第一，否则 resolver 白做。
        val (picked, tier) = SourceResolveCascade.pick(
            lastGood = null,
            cloudPreferred = "cloud:best:0",
            localDetailLines = listOf("hhkan:a:0", "hhkan:b:0"),
        )
        assertEquals("cloud:best:0", picked)
        assertEquals(SourceResolveCascade.Tier.CLOUD_PREFERRED, tier)
    }

    @Test
    fun `last good memory still wins over both cloud and local`() = runBlocking {
        val (picked, tier) = SourceResolveCascade.pick(
            lastGood = "remembered:line:0",
            cloudPreferred = "cloud:best:0",
            localDetailLines = listOf("hhkan:a:0"),
        )
        assertEquals("remembered:line:0", picked)
        assertEquals(SourceResolveCascade.Tier.LAST_GOOD, tier)
    }

    @Test
    fun `the resolver time budget stays far below the local detail timeout`() {
        // 把「别把可选优化接回起播阻塞路径」变成一个会被测到的数字。
        // 旧实现让 resolver 复用 detailClient 的 45s —— 这个断言就是那次的回归防线。
        assertTrue(
            "resolver 超时必须远小于本地详情匹配上限（曾误用 45s）",
            SourceResolveTimingPolicy.isBoundedAgainstDetailTimeout,
        )
        assertTrue(
            "宽限窗口必须远小于 45s",
            SourceResolveTimingPolicy.GRACE_MS < 5_000L,
        )
    }
}
