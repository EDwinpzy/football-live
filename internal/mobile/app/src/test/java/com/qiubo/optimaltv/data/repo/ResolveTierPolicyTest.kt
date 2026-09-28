package com.qiubo.optimaltv.data.repo

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 取源阶梯的**顺序契约**（纯策略层，钉住「云端不得拖住起播」）。
 *
 * 背景：磐石云机房无境外出口，云端 `/vod/resolve` 常年不可达。旧序把云端排在
 * 端侧 detail 之前**串行**等它，每次点播先白等一个 10s 超时才回落——方案的
 * 「客户端优先直连 / 云端失败不拖起播」被架空。这里把「本地先拿线、云端只做
 * 有上限的锦上添花排序」写成可执行断言，防止以后又被改回云端优先。
 */
class ResolveTierPolicyTest {

    @Test
    fun resolverTimeoutIsShortEnoughToStayInsideTheFirstFrameBudget() {
        // 方案：首帧 P95 ≤ 4s。Resolver 是排序层，超时必须远低于这个预算，
        // 否则每次点播都要先付一遍「等云端」的固定成本。
        assertTrue(
            "云端 Resolver 超时应 < 2000ms（首帧 P95 预算 4s 的一半）",
            VodRepositoryConfig.RESOLVER_TIMEOUT_MS < 2_000L,
        )
    }

    @Test
    fun localTierIsEvaluatedBeforeTheCloudTier() {
        // 阶梯顺序：本机 detail（唯一能造出可播线路的一层）→ L0 记忆 → L1 云端排序。
        // 云端永远在最后，且失败不影响前两层已产出的线路。
        val order = VodRepositoryConfig.TIER_ORDER
        assertEquals("detail", order[0])
        assertTrue("云端排序层不得排在端侧取源之前", order.indexOf("cloud") == order.lastIndex)
        assertTrue(order.indexOf("cloud") > order.indexOf("last_good"))
    }

    @Test
    fun cloudFailureKeepsTheLocallyResolvedLines() {
        // 云端不可用（超时/不可达/无候选）→ 观感必须与旧版一致：仍拿本地那批线。
        val local = listOf("s:a", "s:b", "s:c")
        assertEquals(local, VodRepositoryConfig.onCloudUnavailable(local))
        // 空本地（端侧也没线）→ 仍然空，且不因云端而凭空造线。
        assertEquals(emptyList<String>(), VodRepositoryConfig.onCloudUnavailable(emptyList<String>()))
    }
}
