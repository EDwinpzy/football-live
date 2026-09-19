package com.qiubo.optimaltv.ui.search

/**
 * tvOS search canvas values shared by the composable and its layout contract tests.
 * Values are in the 1920x1080 reference coordinate system and are scaled by sx().
 */
internal object SearchLayoutSpec {
    const val pageSidePadding = 70f
    const val keyboardHeight = 295f
    const val dividerOpacity = 0.3f
    const val resultColumns = 4
    const val resultCardWidth = 410f
    const val resultCardHeight = 231f
    const val resultCardCornerRadius = 12f
    const val resultColumnGap = 40f
    const val resultTitleSize = 31f
    const val topResultsTitleSize = 38f
}
