package com.qiubo.optimaltv.ui.components

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * /api/ime/hotwords 响应解析单测（走真实 org.json，仅 Robolectric 提供实现）。
 *
 * 覆盖会被打穿的契约：
 * - 坏 JSON / 缺 words / words 非数组 → 解析为 null，调用方据此保留旧快照，绝不清空；
 * - 混入坏条目时只丢坏条目，好条目照常进快照；
 * - updated_at / source / stale 三个元字段按后端契约读取。
 */
@RunWith(RobolectricTestRunner::class)
class ImeHotwordParseTest {

    @Test
    fun `正常载荷解析出词条与元信息`() {
        val body = """
            {"words":[{"text":"爱情公寓","pinyin":"aiqinggongyu","initials":"aqgy","kind":"tv","weight":980},
                      {"text":"三体","pinyin":"santi","initials":"st","kind":"tv","weight":900}],
             "updated_at":1758000000.5,"source":"cache","stale":false}
        """.trimIndent()
        val out = ImeHotwordCandidates.parseResponse(body)
        assertEquals(2, out!!.entries.size)
        assertEquals("爱情公寓", out.entries[0].text)
        assertEquals(1758000000.5, out.updatedAt, 0.001)
        assertEquals("cache", out.source)
        assertFalse(out.stale)
    }

    @Test
    fun `缺 words 字段返回 null`() {
        assertEquals(null, ImeHotwordCandidates.parseResponse("""{"updated_at":1,"source":"x","stale":true}"""))
    }

    @Test
    fun `words 不是数组返回 null`() {
        assertEquals(null, ImeHotwordCandidates.parseResponse("""{"words":{"a":1}}"""))
    }

    @Test
    fun `坏 JSON 返回 null`() {
        assertEquals(null, ImeHotwordCandidates.parseResponse("not json at all"))
        assertEquals(null, ImeHotwordCandidates.parseResponse(""))
    }

    @Test
    fun `全坏条目返回 null_避免用空快照覆盖旧数据`() {
        val body = """{"words":[{"text":"","pinyin":"","initials":"","kind":"tv","weight":1}],
                       "updated_at":1,"source":"cache","stale":true}"""
        assertEquals(null, ImeHotwordCandidates.parseResponse(body))
    }

    @Test
    fun `混合载荷只丢弃坏条目`() {
        val body = """
            {"words":[{"text":"","pinyin":"x","initials":"x","kind":"tv","weight":1},
                      "not-an-object",
                      {"text":"三体","pinyin":"santi","initials":"st","kind":"tv","weight":900}],
             "updated_at":5,"source":"catalog-live","stale":false}
        """.trimIndent()
        val out = ImeHotwordCandidates.parseResponse(body)!!
        assertEquals(listOf("三体"), out.entries.map { it.text })
        assertTrue(out.entries[0].weight == 900)
    }

    @Test
    fun `缺元字段时用保守默认值`() {
        val body = """{"words":[{"text":"三体","pinyin":"santi","initials":"st","kind":"tv","weight":900}]}"""
        val out = ImeHotwordCandidates.parseResponse(body)!!
        assertEquals(0.0, out.updatedAt, 0.001)
        assertEquals("", out.source)
        assertTrue(out.stale)
    }
}
