package com.qiubo.optimaltv.ui.player

/** Rules for the ordinary movie/series retry loop; live and football replays keep their own flows. */
internal object VodRetryPolicy {
    const val RETRY_DELAY_MS = 1_000L
    const val SEEK_POSITION_TOLERANCE_MS = 3_000L

    fun shouldRetry(isLive: Boolean): Boolean = !isLive

    fun shouldRetryBootAfterAttempt(
        isLive: Boolean,
        hasFatalMessage: Boolean,
        attemptFailed: Boolean,
    ): Boolean = shouldRetry(isLive) && (hasFatalMessage || attemptFailed)

    fun shouldUseStartupWatchdog(isLive: Boolean): Boolean = isLive

    /** A not-yet-observed seek target wins over a stale engine position. */
    fun resumePositionMs(pendingSeekMs: Long?, currentPositionMs: Long): Long =
        (pendingSeekMs ?: currentPositionMs).coerceAtLeast(0L)

    fun reachedSeekTarget(actualPositionMs: Long, targetPositionMs: Long): Boolean =
        kotlin.math.abs(actualPositionMs - targetPositionMs) <= SEEK_POSITION_TOLERANCE_MS

    /** Locate the selected source in a refreshed episode list; never substitute another source. */
    fun matchingSourceIndex(sourceIdentity: String?, candidates: List<String?>): Int? =
        sourceIdentity?.takeIf(String::isNotBlank)
            ?.let { identity -> candidates.indexOf(identity).takeIf { it >= 0 } }
}

/** A cancelled selection invalidates every delayed retry from the previous selection. */
internal class VodRetryGeneration {
    private var current = 0L

    fun begin(): Long = ++current

    fun invalidate(): Long = ++current

    fun isCurrent(token: Long): Boolean = token == current
}
