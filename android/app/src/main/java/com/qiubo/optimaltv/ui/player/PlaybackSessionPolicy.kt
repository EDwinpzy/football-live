package com.qiubo.optimaltv.ui.player

/** Pure guards for asynchronous playback work that may finish after a line or engine changes. */
internal object PlaybackSessionPolicy {
    enum class LiveEndAction { RESTART_CURRENT, STOP }

    const val MAX_LIVE_END_RECOVERIES = 1

    fun isCurrent(
        requestGeneration: Int,
        currentGeneration: Int,
        requestLine: Int,
        activeLine: Int,
        requestEngineEpoch: Int,
        currentEngineEpoch: Int,
    ): Boolean = requestGeneration == currentGeneration &&
        requestLine == activeLine &&
        requestEngineEpoch == currentEngineEpoch

    /** Retry a prematurely-ended live window once; a second end must not hot-loop forever. */
    fun actionAfterLiveEnd(recoveryCount: Int): LiveEndAction =
        if (recoveryCount < MAX_LIVE_END_RECOVERIES) LiveEndAction.RESTART_CURRENT
        else LiveEndAction.STOP
}
