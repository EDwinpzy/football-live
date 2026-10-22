package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.model.MatchChannel

/** Helpers for backend-discovered live sources. The backend owns availability and ordering. */
internal object LiveSourcePolicy {
    /** Keep the backend's preferred ordering, including A2/migu/Q3/C4 whenever it returns them. */
    fun orderedIds(sourceIds: Collection<String>): List<String> = sourceIds.toList()

    /** Reuse the last selected source only while this match still exposes it as playable. */
    fun preferred(remembered: String?, available: Collection<String>): String =
        remembered?.takeIf { it.isNotBlank() && it in available } ?: available.firstOrNull().orEmpty()

    /** Match-list channels are informational; playable lines are resolved separately on player open. */
    fun sanitizeChannels(channels: List<MatchChannel>): List<MatchChannel> =
        channels.filter { it.src.isNotBlank() }.distinctBy { it.src }
}
