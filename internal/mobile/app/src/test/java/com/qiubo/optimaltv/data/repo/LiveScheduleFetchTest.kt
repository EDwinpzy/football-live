package com.qiubo.optimaltv.data.repo

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class LiveScheduleFetchTest {
    @Test
    fun matchesRequestsUseOneDirectPath() {
        assertEquals("/api/matches?fresh=1", LiveRepository.matchesRequestPath())
    }

    @Test
    fun successfulDirectEmptyListIsAccepted() {
        assertEquals(0, LiveRepository.matchesArray(JSONObject("""{"matches":[]}""")).length())
    }

    @Test
    fun errorPayloadFailsTheDirectRequestWithoutProxyRetry() {
        assertThrows(IllegalStateException::class.java) {
            LiveRepository.matchesArray(JSONObject("""{"error":"feed unavailable","matches":[]}"""))
        }
    }

    @Test
    fun missingMatchesFieldIsRejected() {
        val error = assertThrows(IllegalStateException::class.java) {
            LiveRepository.matchesArray(JSONObject("{}"))
        }

        assertEquals("赛程接口未返回比赛列表", error.message)
    }
}
