package com.qiubo.optimaltv.playback

enum class PlaybackKind { LIVE, VOD }

enum class RecoveryAction {
    RELOAD_CURRENT,
    REFRESH_CURRENT,
    SWITCH_SOURCE,
    RECREATE_ENGINE,
    RESET_RUNTIME,
    GIVE_UP,
}

/** Pure recovery ladder shared by TV/mobile player coordinators. */
object PlaybackRecoveryPolicy {
    const val STALL_THRESHOLD_MS = 8_000L
    private const val RESUME_REWIND_MS = 2_000L

    fun isStalled(durationMs: Long): Boolean = durationMs >= STALL_THRESHOLD_MS

    fun action(kind: PlaybackKind, attempt: Int): RecoveryAction = when (kind) {
        PlaybackKind.LIVE -> when (attempt) {
            0 -> RecoveryAction.REFRESH_CURRENT
            1 -> RecoveryAction.SWITCH_SOURCE
            2 -> RecoveryAction.RECREATE_ENGINE
            3 -> RecoveryAction.RESET_RUNTIME
            else -> RecoveryAction.GIVE_UP
        }
        PlaybackKind.VOD -> when (attempt) {
            0 -> RecoveryAction.RELOAD_CURRENT
            1 -> RecoveryAction.REFRESH_CURRENT
            2 -> RecoveryAction.SWITCH_SOURCE
            3 -> RecoveryAction.RECREATE_ENGINE
            else -> RecoveryAction.GIVE_UP
        }
    }

    /** A live source gets one fresh same-source retry before we move on. */
    fun liveLineFailureAction(retryCount: Int, hasAlternate: Boolean): RecoveryAction = when {
        retryCount <= 0 -> RecoveryAction.REFRESH_CURRENT
        hasAlternate -> RecoveryAction.SWITCH_SOURCE
        else -> RecoveryAction.GIVE_UP
    }

    fun resumePosition(positionMs: Long): Long = (positionMs - RESUME_REWIND_MS).coerceAtLeast(0L)

    class Generation {
        private var current = 0L

        fun next(): Long = ++current
        fun accepts(value: Long): Boolean = value == current
    }
}

/** Live HLS offset policy shared by both Android clients. */
object LiveWindowPolicy {
    const val SMALL_WINDOW_MAX_MS = 12_000L

    fun isSmall(windowMs: Long): Boolean = windowMs in 1..SMALL_WINDOW_MAX_MS

    /** Keep more of a short football playlist in reserve against a missed poll/segment. */
    fun targetOffsetMs(windowMs: Long): Long =
        if (isSmall(windowMs)) (windowMs * 3 / 5).coerceIn(1_200L, 8_000L) else 15_000L

    fun minOffsetMs(windowMs: Long): Long =
        if (isSmall(windowMs)) (windowMs / 8).coerceIn(300L, 1_000L) else 8_000L

    fun maxOffsetMs(windowMs: Long): Long =
        if (isSmall(windowMs)) (windowMs * 2 / 3).coerceIn(2_500L, 8_000L) else 30_000L
}

