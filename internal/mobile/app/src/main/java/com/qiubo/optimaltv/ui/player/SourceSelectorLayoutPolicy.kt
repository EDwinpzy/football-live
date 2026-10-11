package com.qiubo.optimaltv.ui.player

/** Calculates the largest source prefix that fits within a fixed number of wrapped rows. */
internal object SourceSelectorLayoutPolicy {
    fun visiblePrefixCount(
        chipWidthsPx: List<Int>,
        availableWidthPx: Int,
        spacingPx: Int,
        maxRows: Int = 2,
    ): Int {
        if (chipWidthsPx.isEmpty() || maxRows <= 0) return 0
        if (availableWidthPx == Int.MAX_VALUE) return chipWidthsPx.size
        if (availableWidthPx <= 0) return 0

        val spacing = spacingPx.coerceAtLeast(0)
        var visibleCount = 0
        var rowCount = 1
        var rowWidth = 0
        var itemsInRow = 0

        for (rawWidth in chipWidthsPx) {
            val chipWidth = rawWidth.coerceIn(0, availableWidthPx)
            val nextWidth = if (itemsInRow == 0) chipWidth else rowWidth + spacing + chipWidth
            if (itemsInRow > 0 && nextWidth > availableWidthPx) {
                rowCount += 1
                if (rowCount > maxRows) break
                rowWidth = chipWidth
                itemsInRow = 1
            } else {
                rowWidth = nextWidth
                itemsInRow += 1
            }
            visibleCount += 1
        }
        return visibleCount
    }
}
