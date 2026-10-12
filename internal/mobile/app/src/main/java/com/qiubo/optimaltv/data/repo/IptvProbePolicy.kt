package com.qiubo.optimaltv.data.repo

/** A successful HTTP response must contain a playlist or a recognizable media container. */
internal object IptvProbePolicy {
    const val MAX_PLAYLIST_BYTES = 32 * 1024

    fun isPlaylistUrl(url: String): Boolean = url.substringBefore('?').endsWith(".m3u8", true)
    fun isPlaylistContentType(type: String?): Boolean = type?.lowercase()?.let {
        "mpegurl" in it || "m3u" in it
    } == true
    fun isPlaylist(body: ByteArray, type: String?, url: String): Boolean =
        body.toString(Charsets.UTF_8).trimStart('\uFEFF', ' ', '\r', '\n').startsWith("#EXTM3U")
    fun firstPlaylistReference(body: ByteArray): String? = body.toString(Charsets.UTF_8)
        .lineSequence().map(String::trim).firstOrNull { it.isNotEmpty() && !it.startsWith('#') }
    fun isCompletePlaylistResponse(body: ByteArray, status: Int, contentRange: String?): Boolean {
        if (body.size > MAX_PLAYLIST_BYTES) return false
        if (status == 200) return true
        if (status != 206) return false
        val match = Regex("""bytes\s+(\d+)-(\d+)/(\d+)""", RegexOption.IGNORE_CASE)
            .matchEntire(contentRange?.trim().orEmpty()) ?: return false
        val start = match.groupValues[1].toLongOrNull() ?: return false
        val end = match.groupValues[2].toLongOrNull() ?: return false
        val total = match.groupValues[3].toLongOrNull() ?: return false
        return start == 0L && end + 1 == total && end - start + 1 == body.size.toLong()
    }
    fun isLivePlaylist(body: ByteArray): Boolean =
        body.size <= MAX_PLAYLIST_BYTES && !body.toString(Charsets.UTF_8).contains("#EXT-X-ENDLIST")
    fun isUsableMediaPayload(body: ByteArray, type: String?): Boolean {
        if (body.size < 188) return false
        val prefix = body.take(64).toByteArray().toString(Charsets.UTF_8).trimStart().lowercase()
        if (prefix.startsWith('<') || prefix.startsWith('{') || prefix.startsWith("#extm3u")) return false
        if (type?.lowercase()?.let { "html" in it || "json" in it } == true) return false
        if (body.take(3).toByteArray().toString(Charsets.US_ASCII) == "FLV") return true
        if (body.copyOfRange(4, 8).toString(Charsets.US_ASCII) in listOf("ftyp", "styp", "moof", "sidx")) return true
        return (0 until minOf(188, body.size - 376)).any { offset ->
            body[offset] == 0x47.toByte() && body[offset + 188] == 0x47.toByte() && body[offset + 376] == 0x47.toByte()
        }
    }
}
