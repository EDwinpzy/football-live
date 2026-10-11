package com.qiubo.optimaltv.data.repo

import org.json.JSONObject
import java.net.URLEncoder

internal object LiveStreamRequestPath {
    fun build(matchId: String, source: String, fresh: Boolean, footballVpn: Boolean = false): String = buildString {
        append("/api/stream/")
        append(URLEncoder.encode(matchId, "UTF-8"))
        append("?src=")
        append(URLEncoder.encode(source, "UTF-8"))
        if (fresh) append("&fresh=1")
        if (footballVpn) append("&football_vpn=1")
    }
}

/** Raw stream fields returned by /api/stream before the local relay is applied. */
internal data class LiveStreamPayload(
    val source: String,
    val name: String,
    val url: String,
    val referer: String,
)

/** Parses only server-confirmed playable HLS entries; source identifiers and labels stay dynamic. */
internal object LiveStreamPayloadParser {
    private const val DEFAULT_REFERER = "https://www.88zhibo.tv/"

    fun available(root: JSONObject): List<LiveStreamPayload> {
        val entries = root.optJSONArray("sources") ?: return emptyList()
        val seen = HashSet<String>()
        return (0 until entries.length()).mapNotNull { index ->
            val entry = entries.optJSONObject(index) ?: return@mapNotNull null
            if (!text(entry, "player").equals("m3u8", ignoreCase = true)) return@mapNotNull null
            val source = text(entry, "source")
            val url = text(entry, "url")
            if (source.isBlank() || !isHttpUrl(url) || !seen.add(source)) return@mapNotNull null
            LiveStreamPayload(
                source = source,
                name = text(entry, "name").ifBlank { source },
                url = url,
                referer = text(entry, "referer").ifBlank { DEFAULT_REFERER },
            )
        }
    }

    fun renewed(
        root: JSONObject,
        requestedSource: String,
        fallbackName: String = "",
        fallbackReferer: String = "",
    ): LiveStreamPayload? {
        if (requestedSource.isBlank()) return null
        val responseSource = text(root, "source").ifBlank { requestedSource }
        if (responseSource != requestedSource) return null
        val player = text(root, "player")
        if (player.isNotBlank() && !player.equals("m3u8", ignoreCase = true)) return null
        val url = text(root, "url").ifBlank { text(root, "m3u8") }
        if (!isHttpUrl(url)) return null
        return LiveStreamPayload(
            source = requestedSource,
            name = text(root, "name").ifBlank { fallbackName.ifBlank { requestedSource } },
            url = url,
            referer = text(root, "referer").ifBlank {
                fallbackReferer.ifBlank { DEFAULT_REFERER }
            },
        )
    }

    private fun text(json: JSONObject, key: String): String =
        if (json.isNull(key)) "" else json.optString(key).takeUnless { it == "null" }.orEmpty().trim()

    private fun isHttpUrl(value: String): Boolean =
        value.startsWith("https://", ignoreCase = true) || value.startsWith("http://", ignoreCase = true)
}
