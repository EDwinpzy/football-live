package com.qiubo.optimaltv.ui.all

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class VodFilterDesignSpecTest {
    @Test
    fun compactLayout_exposesReferenceRhythm() {
        val field = VodFilterDesignSpec::class.java.declaredFields
            .firstOrNull { it.name == "compactLayout" }

        assertTrue(
            "The filter redesign must expose compact layout tokens before the UI is wired",
            field != null,
        )
        assertTrue(
            "Portrait filter text needs a phone-sized token instead of TV pixel scaling",
            VodFilterDesignSpec.Layout::class.java.declaredFields.any { it.name == "mobileTextSize" },
        )
        assertEquals(48f, VodFilterDesignSpec.compactLayout.mobileRowHeight, 0f)
        assertEquals(48f, VodFilterDesignSpec.compactLayout.tvRowHeight, 0f)
        assertEquals(32f, VodFilterDesignSpec.compactLayout.chipHeight, 0f)
        assertEquals(14f, VodFilterDesignSpec.compactLayout.optionGap, 0f)
        assertEquals(64f, VodFilterDesignSpec.compactLayout.selectedMinWidth, 0f)
        assertEquals(26f, VodFilterDesignSpec.compactLayout.mobileTextSize, 0f)

        val mobileChipHeight = VodFilterDesignSpec.Layout::class.java.declaredFields
            .firstOrNull { it.name == "mobileChipHeight" }
        assertTrue(
            "Portrait filter chips must be tall enough for the phone-sized text",
            mobileChipHeight != null,
        )
        mobileChipHeight?.isAccessible = true
        assertEquals(
            40f,
            mobileChipHeight?.getFloat(VodFilterDesignSpec.compactLayout) ?: 0f,
            0f,
        )
    }

    @Test
    fun mobileRows_matchDesignOrderAndVisibleOptions() {
        val rows = VodFilterDesignSpec.rows(portrait = true)

        assertEquals(listOf("类别", "类型", "地区", "年份", "排序"), rows.map { it.label })
        assertEquals(
            listOf("电影", "电视剧", "动漫", "综艺", "短剧"),
            rows.first().options.map { it.label },
        )
        assertEquals(
            listOf("全部", "剧情", "喜剧", "动作", "爱情", "科幻", "悬疑", "历史", "传记", "音乐"),
            rows[1].options.map { it.label },
        )
        assertTrue(rows.none { it.label == "评分" })
    }

    @Test
    fun landscapeRows_matchDesignOrderAndIncludeRating() {
        val rows = VodFilterDesignSpec.rows(portrait = false)

        assertEquals(listOf("类别", "类型", "地区", "年份", "评分", "排序"), rows.map { it.label })
        assertEquals("电影", rows.first().options[0].label)
        assertEquals(
            listOf("全部", "剧情", "喜剧", "动作", "爱情", "科幻", "动画", "悬疑", "惊悚", "恐怖", "犯罪", "历史", "传记", "音乐"),
            rows[1].options.map { it.label },
        )
        assertEquals("暂无评分", rows[4].options.last().label)
    }

    @Test
    fun filterText_usesTightLineBoxWithoutFontPadding() {
        val layoutType = VodFilterDesignSpec.Layout::class.java
        val tvLineHeight = layoutType.declaredFields.firstOrNull { it.name == "tvTextLineHeight" }
        val mobileLineHeight = layoutType.declaredFields.firstOrNull { it.name == "mobileTextLineHeight" }
        val includeFontPadding = layoutType.declaredFields.firstOrNull { it.name == "includeFontPadding" }

        assertTrue("Landscape filter text needs an explicit tight line height", tvLineHeight != null)
        assertTrue("Portrait filter text needs an explicit tight line height", mobileLineHeight != null)
        assertTrue("Filter text must opt out of Android font padding", includeFontPadding != null)

        tvLineHeight?.isAccessible = true
        mobileLineHeight?.isAccessible = true
        includeFontPadding?.isAccessible = true
        assertEquals(16f, tvLineHeight?.getFloat(VodFilterDesignSpec.compactLayout) ?: 0f, 0f)
        assertEquals(26f, mobileLineHeight?.getFloat(VodFilterDesignSpec.compactLayout) ?: 0f, 0f)
        assertEquals(false, includeFontPadding?.getBoolean(VodFilterDesignSpec.compactLayout) ?: true)
    }
}
