package com.qiubo.optimaltv.ui.player

/** VOD stays on its selected source and retries until the viewer leaves; live keeps its own flow. */
internal object VodRetryPolicy {
    const val RETRY_DELAY_MS = 2_000L
    const val SEEK_POSITION_TOLERANCE_MS = 3_000L

    fun retriesUntilUserLeaves(isLive: Boolean): Boolean = !isLive

    fun shouldRetryBootAfterAttempt(
        isLive: Boolean,
        hasFatalMessage: Boolean,
        attemptFailed: Boolean,
    ): Boolean = retriesUntilUserLeaves(isLive) && (hasFatalMessage || attemptFailed)

    fun shouldUseStartupWatchdog(isLive: Boolean): Boolean = isLive

    fun retryPositionMs(currentPositionMs: Long, pendingSeekTargetMs: Long?): Long =
        (pendingSeekTargetMs ?: currentPositionMs).coerceAtLeast(0L)

    fun resolvedEpisodeStartPositionMs(
        targetEpisodeIndex: Int,
        currentEpisodeIndex: Int,
        currentPositionMs: Long,
        pendingSeekTargetMs: Long?,
    ): Long = if (targetEpisodeIndex == currentEpisodeIndex) {
        retryPositionMs(currentPositionMs, pendingSeekTargetMs)
    } else {
        0L
    }

    fun reachedSeekTarget(actualPositionMs: Long, targetPositionMs: Long): Boolean =
        kotlin.math.abs(actualPositionMs - targetPositionMs) <= SEEK_POSITION_TOLERANCE_MS
}

/** Keep episode changes on the source the viewer selected, even when another source has the episode. */
internal object VodEpisodeSourcePolicy {
    fun matchingLineIndex(selectedSourceIdentity: String?, candidateIdentities: List<String?>): Int? {
        val selected = selectedSourceIdentity?.takeIf(String::isNotBlank) ?: return null
        val matches = candidateIdentities.indices.filter { candidateIdentities[it] == selected }
        return matches.singleOrNull()
    }
}
