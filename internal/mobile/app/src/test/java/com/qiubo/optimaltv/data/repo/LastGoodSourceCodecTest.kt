package com.qiubo.optimaltv.data.repo

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * BUG-03：移动端曾把 line id 三段里的**线路号**丢掉。
 *
 * line id 形如 `{sourceId}:{contentId}:{lineIndex}`。旧代码拆出三段却只存前两段，
 * 读回时线路号缺失被当成 0，于是「上次播第 3 条」静默变成「这次播第 0 条」。
 * 与 TV 端 [com.qiubo.optimaltv.data.repo.LastGoodSourceCodec] 同口径。
 */
class LastGoodSourceCodecTest {

    @Test
    fun `three part line id keeps its line index`() {
        val stored = LastGoodSourceCodec.encode("hhkan:abc123:7")
        assertEquals("hhkan", stored?.sourceId)
        assertEquals("abc123", stored?.contentId)
        assertEquals(7, stored?.lineIndex)
        assertEquals("hhkan:abc123:7", stored?.lineId())
    }

    @Test
    fun `two part line id means line zero`() {
        val stored = LastGoodSourceCodec.encode("hhkan:abc123")
        assertEquals("hhkan", stored?.sourceId)
        assertEquals("abc123", stored?.contentId)
        assertEquals(0, stored?.lineIndex)
    }

    @Test
    fun `non numeric third segment falls back to zero instead of dropping the memory`() {
        val stored = LastGoodSourceCodec.encode("hhkan:abc123:oops")
        assertEquals(0, stored?.lineIndex)
    }

    @Test
    fun `blank or malformed line ids are rejected`() {
        assertNull(LastGoodSourceCodec.encode(""))
        assertNull(LastGoodSourceCodec.encode("   "))
        assertNull(LastGoodSourceCodec.encode("onlyone"))
        assertNull(LastGoodSourceCodec.encode("a:b:c:d"))
        assertNull(LastGoodSourceCodec.encode(":b"))
        assertNull(LastGoodSourceCodec.encode("a:"))
    }

    @Test
    fun `round trip preserves the line index`() {
        val original = "source-x:content-9:5"
        val stored = LastGoodSourceCodec.encode(original)!!
        assertEquals(original, stored.lineId())
    }
}
