package com.qiubo.optimaltv.data.repo

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * last_good（L0 线路记忆）的**线路号往返**契约。
 *
 * 后端 `/vod/detail` 的线路 id 格式是 `{source_id}:{content_id}:{lineIndex}`，第三段
 * 是「该内容在源下的第几条线路」，**不是集号**（见 tools/backend-src/vod_sources.py
 * `"%s:%s:%d" % (source.id, candidate["id"], index)`）。
 *
 * 本轮修的真实缺陷：L0 记忆在写入时只留了 `source_id` + `content_id`、**丢掉了第三段
 * lineIndex**，读回时又硬编码补 `:0`。于是「上次成功播的是第 3 条线路」被记成第 0 条，
 * 下次起播会去播**另一条线路**——记忆反而变成了「随机换线」，比没有记忆更糟。
 *
 * 这条测试锁住：第 N 条线路原样往返，读回来仍是第 N 条。
 */
class LastGoodSourceCodecTest {

    @Test
    fun `a remembered line keeps its line index through the round trip`() {
        listOf(0, 1, 2, 5, 11).forEach { idx ->
            val stored = requireNotNull(LastGoodSourceCodec.encode("hhkan:content-abc:$idx")) {
                "第 $idx 条线路的 id 应能解析"
            }
            assertEquals("第 $idx 条线路的 sourceId 不该被改", "hhkan", stored.sourceId)
            assertEquals("第 $idx 条线路的 contentId 不该被改", "content-abc", stored.contentId)
            assertEquals("第 $idx 条线路的 lineIndex 丢失了", idx, stored.lineIndex)
            assertEquals("往返后必须还原成同一条线路", "hhkan:content-abc:$idx", stored.lineId())
        }
    }

    @Test
    fun `a two segment id is treated as line index zero`() {
        // 云端 resolver 的候选只有 source_id + content_id（没有 line_index 字段），
        // 拼出的 id 就是两段；这属于「第 0 条线路」，不是解析失败。
        val stored = requireNotNull(LastGoodSourceCodec.encode("hhkan:content-abc"))
        assertEquals("hhkan", stored.sourceId)
        assertEquals("content-abc", stored.contentId)
        assertEquals(0, stored.lineIndex)
    }

    @Test
    fun `malformed ids are rejected instead of silently mapping to a wrong line`() {
        // 宁可当作「没有记忆」回落本地全量匹配，也不要拿一个坏 id 拼出错误线路。
        listOf("", "   ", "no-colon", "hhkan:", ":content", "hhkan::3").forEach { bad ->
            assertNull(
                "「$bad」应判为无效记忆而不是解析出错误线路",
                LastGoodSourceCodec.encode(bad),
            )
        }
    }

    @Test
    fun `a negative or non numeric line index falls back to zero`() {
        assertEquals(0, LastGoodSourceCodec.encode("hhkan:c:abc")?.lineIndex)
        assertEquals(0, LastGoodSourceCodec.encode("hhkan:c:-1")?.lineIndex)
    }
}
