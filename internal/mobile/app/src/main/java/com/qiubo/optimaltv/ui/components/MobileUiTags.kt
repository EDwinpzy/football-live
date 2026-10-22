package com.qiubo.optimaltv.ui.components

/** Stable semantic tags used by the football and player UI tests. */
object MobileUiTags {
    const val MainTabBar = "mobile-main-tab-bar"
    const val BackButton = "mobile-back-button"
    const val PlayerSeek = "player-portrait-seek"
    const val PlayerPlayPause = "player-portrait-play-pause"
    const val PlayerFullscreen = "player-portrait-fullscreen"
    const val PlayerMore = "player-portrait-more"

    fun mainTab(key: String) = "mobile-tab-$key"
}
