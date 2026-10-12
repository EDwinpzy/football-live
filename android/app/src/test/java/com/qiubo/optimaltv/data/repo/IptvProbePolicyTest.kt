package com.qiubo.optimaltv.data.repo

import org.junit.Assert.*
import org.junit.Test

class IptvProbePolicyTest {
    @Test fun rejectsHttp200ErrorBodies() {
        assertFalse(IptvProbePolicy.isUsableMediaPayload("<html>unavailable</html>".repeat(20).toByteArray(), "video/mp2t"))
        assertFalse(IptvProbePolicy.isUsableMediaPayload(ByteArray(800), "text/html"))
        assertFalse(IptvProbePolicy.isUsableMediaPayload(ByteArray(800), "video/mp2t"))
    }
    @Test fun acceptsContainersAndResolvesPlaylistEntry() {
        val ts = ByteArray(188 * 4).also { for (i in 0..3) it[i * 188] = 0x47 }
        assertTrue(IptvProbePolicy.isUsableMediaPayload(ts, "application/octet-stream"))
        val mp4 = ByteArray(800).also { "ftyp".toByteArray().copyInto(it, 4) }
        assertTrue(IptvProbePolicy.isUsableMediaPayload(mp4, null))
        val list = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nsmall/index.m3u8\n".toByteArray()
        assertTrue(IptvProbePolicy.isPlaylist(list, null, "https://test/live"))
        assertEquals("small/index.m3u8", IptvProbePolicy.firstPlaylistReference(list))
        assertNull(IptvProbePolicy.firstPlaylistReference("#EXTM3U\n".toByteArray()))
    }

    @Test fun acceptsOnlyCompletePlaylistResponses() {
        val body = "#EXTM3U\n#EXTINF:5,\nsegment.ts\n".toByteArray()
        assertTrue(IptvProbePolicy.isCompletePlaylistResponse(body, 200, null))
        assertTrue(IptvProbePolicy.isCompletePlaylistResponse(
            body, 206, "bytes 0-${body.size - 1}/${body.size}",
        ))
        assertFalse(IptvProbePolicy.isCompletePlaylistResponse(
            body, 206, "bytes 0-${body.size - 1}/${body.size + 1}",
        ))
        assertFalse(IptvProbePolicy.isCompletePlaylistResponse(body, 206, null))
    }

    @Test fun rejectsLongEndedAndOversizedPlaylists() {
        val longVod = ("#EXTM3U\n" + "#EXTINF:5,\nsegment.ts\n".repeat(300) + "#EXT-X-ENDLIST\n").toByteArray()
        assertTrue(longVod.indexOfSubsequence("#EXT-X-ENDLIST".toByteArray()) > 4096)
        assertFalse(IptvProbePolicy.isLivePlaylist(longVod))

        val oversized = ByteArray(IptvProbePolicy.MAX_PLAYLIST_BYTES + 1) { 'x'.code.toByte() }
        assertFalse(IptvProbePolicy.isCompletePlaylistResponse(oversized, 200, null))
        assertFalse(IptvProbePolicy.isLivePlaylist(oversized))
    }

    private fun ByteArray.indexOfSubsequence(needle: ByteArray): Int {
        if (needle.isEmpty()) return 0
        return indices.firstOrNull { start ->
            start + needle.size <= size && needle.indices.all { this[start + it] == needle[it] }
        } ?: -1
    }
}
