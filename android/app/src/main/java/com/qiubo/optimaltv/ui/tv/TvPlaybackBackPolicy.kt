package com.qiubo.optimaltv.ui.tv

internal enum class TvPlaybackBackAction {
    POP_BROWSE,
    EXIT_APP,
    RETURN_TO_GROUPS,
    CLOSE_SIDEBAR,
    SHOW_EXIT_HINT,
}

/** Pure BACK routing for the TV page's browse, sidebar, and playback states. */
internal object TvPlaybackBackPolicy {
    const val DOUBLE_BACK_WINDOW_MS = 2_000L

    fun isDoubleBack(previousBackAtMs: Long, nowMs: Long): Boolean =
        previousBackAtMs > 0L && nowMs >= previousBackAtMs &&
            nowMs - previousBackAtMs < DOUBLE_BACK_WINDOW_MS

    /** Only an exit-hint BACK remains eligible as the first tap of a later double BACK. */
    fun nextBackAtMs(action: TvPlaybackBackAction, nowMs: Long): Long =
        if (action == TvPlaybackBackAction.SHOW_EXIT_HINT) nowMs else 0L

    fun action(
        playing: Boolean,
        sidebarVisible: Boolean,
        channelColumnFocused: Boolean,
        isDoubleBack: Boolean,
    ): TvPlaybackBackAction = when {
        !playing -> TvPlaybackBackAction.POP_BROWSE
        sidebarVisible && channelColumnFocused -> TvPlaybackBackAction.RETURN_TO_GROUPS
        sidebarVisible -> TvPlaybackBackAction.CLOSE_SIDEBAR
        isDoubleBack -> TvPlaybackBackAction.EXIT_APP
        else -> TvPlaybackBackAction.SHOW_EXIT_HINT
    }
}
