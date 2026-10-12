package com.qiubo.optimaltv.playback

import java.net.URI
import java.util.UUID

/** Only our loopback HLS relay is normalized; direct URLs and other engines stay unchanged. */
internal object VlcRelaySession {
    fun playbackUrl(url: String, session: String = UUID.randomUUID().toString().replace("-", "")): String {
        val uri = runCatching { URI(url) }.getOrNull() ?: return url
        if (uri.host !in setOf("127.0.0.1", "localhost") || uri.path != "/api/relay") return url
        if (!session.matches(Regex("[0-9a-f]{32}"))) return url
        val base = url.substringBefore('#')
        return base + (if (uri.rawQuery.isNullOrEmpty()) "?" else "&") + "vlc_session=$session"
    }
}
