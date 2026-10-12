package com.qiubo.optimaltv.ui.tv

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TvPlaybackBackPolicyTest {
    @Test
    fun firstBackIsNotMisreadAsDoubleTapDuringEarlyDeviceUptime() {
        assertFalse(TvPlaybackBackPolicy.isDoubleBack(0L, 1_500L))
        assertTrue(TvPlaybackBackPolicy.isDoubleBack(1_000L, 1_500L))
        assertFalse(TvPlaybackBackPolicy.isDoubleBack(1_000L, 3_000L))
    }

    @Test
    fun doubleBackFromPlaybackExitsTheApp() {
        assertEquals(
            TvPlaybackBackAction.EXIT_APP,
            TvPlaybackBackPolicy.action(
                playing = true,
                sidebarVisible = false,
                channelColumnFocused = false,
                isDoubleBack = true,
            ),
        )
    }

    @Test
    fun firstBackFromPlaybackShowsHintWithoutChangingPage() {
        assertEquals(
            TvPlaybackBackAction.SHOW_EXIT_HINT,
            TvPlaybackBackPolicy.action(
                playing = true,
                sidebarVisible = false,
                channelColumnFocused = false,
                isDoubleBack = false,
            ),
        )
    }

    @Test
    fun sidebarBackKeepsItsTwoLevelNavigation() {
        assertEquals(
            TvPlaybackBackAction.RETURN_TO_GROUPS,
            TvPlaybackBackPolicy.action(true, true, true, true),
        )
        assertEquals(
            TvPlaybackBackAction.CLOSE_SIDEBAR,
            TvPlaybackBackPolicy.action(true, true, false, true),
        )
    }

    @Test
    fun backsConsumedBySidebarClearPendingDoubleBackWindow() {
        var lastBackAt = TvPlaybackBackPolicy.nextBackAtMs(
            TvPlaybackBackAction.SHOW_EXIT_HINT,
            nowMs = 1_000L,
        )
        assertTrue(TvPlaybackBackPolicy.isDoubleBack(lastBackAt, 1_100L))

        // First sidebar BACK returns from the channel column to its group list.
        lastBackAt = TvPlaybackBackPolicy.nextBackAtMs(
            TvPlaybackBackAction.RETURN_TO_GROUPS,
            nowMs = 1_100L,
        )
        assertEquals(0L, lastBackAt)
        // Second sidebar BACK closes the sidebar. Neither tap may count toward playback exit.
        lastBackAt = TvPlaybackBackPolicy.nextBackAtMs(
            TvPlaybackBackAction.CLOSE_SIDEBAR,
            nowMs = 1_200L,
        )
        assertEquals(0L, lastBackAt)

        assertFalse(TvPlaybackBackPolicy.isDoubleBack(lastBackAt, 1_300L))
        assertEquals(
            TvPlaybackBackAction.SHOW_EXIT_HINT,
            TvPlaybackBackPolicy.action(
                playing = true,
                sidebarVisible = false,
                channelColumnFocused = false,
                isDoubleBack = TvPlaybackBackPolicy.isDoubleBack(lastBackAt, 1_300L),
            ),
        )
    }

    @Test
    fun browseBackStillUsesNavigationStack() {
        assertEquals(
            TvPlaybackBackAction.POP_BROWSE,
            TvPlaybackBackPolicy.action(false, false, false, true),
        )
    }
}
