package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.source.HhkanSource

internal class SelectedSourceRefreshException(message: String, cause: Throwable? = null) :
    IllegalStateException(message, cause)

/** Maps a manually selected Douban line/episode onto a refreshed detail response. */
internal object DoubanRefreshPlayReference {
    data class Target(val lineId: String, val episode: String)

    fun map(
        selectedLineId: String,
        selectedLineName: String,
        selectedEpisode: String,
        previous: HhkanSource.Companion.DetailInfo?,
        refreshed: HhkanSource.Companion.DetailInfo,
    ): Target? {
        val sourceId = selectedLineId.substringBefore(':').takeIf { ':' in selectedLineId }
        val previousLine = previous?.lines?.firstOrNull { line ->
            line.episodes.any { it.second == selectedLineId }
        } ?: run {
            val named = selectedLineName.takeUnless(::isAnonymousSignalName)
                ?.takeIf(String::isNotBlank)
                ?.let { name -> previous?.lines.orEmpty().filter { normalized(it.name) == normalized(name) } }
                .orEmpty()
            val byProvider = sourceId?.let { id ->
                named.filter { line -> line.episodes.any { it.second.substringBefore(':') == id } }
            }.orEmpty()
            byProvider.singleOrNull() ?: named.singleOrNull()
        }
        val previousEpisodeName = previousLine?.episodes
            ?.firstOrNull { it.second == selectedLineId && it.third == selectedEpisode }
            ?.first
            ?.takeIf(String::isNotBlank)
            ?: previousLine?.episodes
                ?.firstOrNull { it.third == selectedEpisode }
            ?.first
            ?.takeIf(String::isNotBlank)

        val selectedName = selectedLineName
            .takeUnless(::isAnonymousSignalName)
            ?.takeIf(String::isNotBlank)
            ?: previousLine?.name.orEmpty()

        val refreshedLine = refreshed.lines.firstOrNull { line ->
            line.episodes.any { it.second == selectedLineId }
        } ?: run {
            val named = selectedName.takeIf(String::isNotBlank)?.let { name ->
                refreshed.lines.filter { normalized(it.name) == normalized(name) }
            }.orEmpty()
            val byProvider = sourceId?.let { id ->
                named.filter { line -> line.episodes.any { it.second.substringBefore(':') == id } }
            }.orEmpty()
            when {
                byProvider.size == 1 -> byProvider.single()
                named.size == 1 -> named.single()
                named.isNotEmpty() -> return null
                else -> {
                    val byStableProviderId = sourceId?.let { id ->
                        refreshed.lines.filter { line -> line.episodes.any { it.second.substringBefore(':') == id } }
                    }.orEmpty()
                    byStableProviderId.singleOrNull()
                }
            }
        } ?: return null

        val targetEpisode = if (previousEpisodeName != null) {
            val sameNamedEpisodes = refreshedLine.episodes.filter {
                normalized(it.first) == normalized(previousEpisodeName)
            }
            when {
                sameNamedEpisodes.size == 1 -> sameNamedEpisodes.single()
                sameNamedEpisodes.size > 1 -> sameNamedEpisodes.firstOrNull { it.third == selectedEpisode }
                else -> null
            }
        } else {
            refreshedLine.episodes.firstOrNull { it.third == selectedEpisode }
        } ?: return null

        return Target(targetEpisode.second, targetEpisode.third)
    }

    private fun isAnonymousSignalName(name: String): Boolean =
        name.trim().matches(Regex("信号源\\s*\\d+", RegexOption.IGNORE_CASE))

    private fun normalized(value: String): String =
        value.trim().lowercase().replace(Regex("\\s+"), " ")
}
