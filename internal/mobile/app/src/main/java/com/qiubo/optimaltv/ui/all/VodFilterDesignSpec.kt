package com.qiubo.optimaltv.ui.all

/**
 * 片库筛选设计稿 v1 的固定视觉内容。
 *
 * 选项值仍使用后端筛选值，label 只负责按设计稿展示；因此锁定视觉稿不会破坏筛选请求。
 */
object VodFilterDesignSpec {
    data class Option(val value: String, val label: String)
    data class Row(val key: String, val label: String, val options: List<Option>)

    /**
     * Compact filter rhythm for the reference-style toolbar. Values are density-independent
     * design units and are intentionally shared by the screen and its visual regression tests.
     */
    data class Layout(
        // Keep the 40px portrait pill unchanged, but add breathing room between rows.
        val mobileRowHeight: Float = 48f,
        val tvRowHeight: Float = 48f,
        val chipHeight: Float = 32f,
        val mobileChipHeight: Float = 40f,
        val selectedMinWidth: Float = 64f,
        val optionGap: Float = 14f,
        val mobileTextSize: Float = 26f,
        val tvTextSize: Float = 16f,
        val mobileTextLineHeight: Float = 26f,
        val tvTextLineHeight: Float = 16f,
        val includeFontPadding: Boolean = false,
        val sectionVerticalPadding: Float = 8f,
    )

    @JvmField
    val compactLayout = Layout()

    fun rows(portrait: Boolean): List<Row> = if (portrait) {
        listOf(
            // Match the reference toolbar: movie is the default selection, not a separate "全部" chip.
            Row("category", "类别", categories(includeAll = false)),
            Row("genre", "类型", genres(mobile = true)),
            Row("region", "地区", regions(mobile = true)),
            Row("year", "年份", years(mobile = true)),
            Row("sort", "排序", sorts()),
        )
    } else {
        listOf(
            Row("category", "类别", categories(includeAll = false)),
            Row("genre", "类型", genres(mobile = false)),
            Row("region", "地区", regions(mobile = false)),
            Row("year", "年份", years(mobile = false)),
            Row("rating", "评分", ratings()),
            Row("sort", "排序", sorts()),
        )
    }

    private fun categories(includeAll: Boolean): List<Option> = buildList {
        if (includeAll) add(Option("", "全部"))
        add(Option("1", "电影"))
        add(Option("2", "电视剧"))
        add(Option("3", "动漫"))
        if (!includeAll) {
            add(Option("4", "综艺"))
            add(Option("6", "短剧"))
        }
    }

    private fun genres(mobile: Boolean): List<Option> {
        val labels = if (mobile) {
            listOf("全部", "剧情", "喜剧", "动作", "爱情", "科幻", "悬疑", "历史", "传记", "音乐")
        } else {
            listOf("全部", "剧情", "喜剧", "动作", "爱情", "科幻", "动画", "悬疑", "惊悚", "恐怖", "犯罪", "历史", "传记", "音乐")
        }
        return labels.map { Option(if (it == "全部") "" else it, it) }
    }

    private fun regions(mobile: Boolean): List<Option> {
        val values = if (mobile) {
            listOf(
                "" to "全部", "中国大陆" to "大陆", "中国香港" to "香港", "中国台湾" to "台湾",
                "美国" to "美国", "英国" to "英国", "日本" to "日本",
            )
        } else {
            listOf(
                "" to "全部", "中国大陆" to "中国大陆", "中国香港" to "中国香港", "中国台湾" to "中国台湾",
                "美国" to "美国", "英国" to "英国", "日本" to "日本", "韩国" to "韩国",
                "印度" to "印度", "泰国" to "泰国", "澳大利亚" to "澳大利亚",
            )
        }
        return values.map { (value, label) -> Option(value, label) }
    }

    private fun years(mobile: Boolean): List<Option> {
        val labels = if (mobile) listOf("全部", "2026", "2025", "2024", "2023", "2022", "更早")
        else listOf("全部", "2026", "2025", "2024", "2023", "2022", "2021", "2020", "2019", "更早")
        return labels.map { Option(if (it == "全部") "" else it, it) }
    }

    private fun ratings(): List<Option> = listOf("全部", "9分以上", "8分以上", "7分以上", "暂无评分")
        .map { Option(if (it == "全部") "" else it, it) }

    private fun sorts(): List<Option> = listOf(
        Option("hot", "热门"), Option("new", "最新上映"), Option("rating", "豆瓣高分"),
    )
}
