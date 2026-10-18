package com.qiubo.optimaltv.ui.player

/** One seek after a remote repeat burst; preview advances without flooding the decoder. */
internal object RemoteSeekPolicy {
    const val SETTLE_MS = 250L

    fun acceleratedDelta(deltaMs: Long, heldMs: Long): Long = deltaMs * when {
        heldMs >= 8_000 -> 12L
        heldMs >= 6_000 -> 8L
        heldMs >= 4_000 -> 4L
        heldMs >= 2_000 -> 2L
        else -> 1L
    }

    fun target(positionMs: Long, pendingMs: Long?, deltaMs: Long, durationMs: Long): Long? {
        // Unknown/invalid short timelines must not turn a forward seek into seekTo(0).
        if (durationMs <= 1_000L) return null
        val max = durationMs - 1_000L
        val requestedBase = pendingMs ?: positionMs
        // A transient short manifest must never clamp a minute-long position back to its head.
        if (requestedBase > durationMs || (deltaMs > 0 && requestedBase > max)) return null
        val base = requestedBase.coerceIn(0L, max)
        return if (deltaMs >= 0) base + deltaMs.coerceAtMost(max - base)
        else base + deltaMs.coerceAtLeast(-base)
    }
}
