package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo
import org.junit.Assert.assertEquals
import org.junit.Test

class LineSpeedTestPolicyTest {
    @Test fun resultTextShowsProgressLatencyAndUnavailableState() {
        assertEquals("", LineSpeedTestPolicy.resultText(null))
        assertEquals("测速中…", LineSpeedTestPolicy.resultText(LineSpeedTestResult(isTesting = true)))
        assertEquals("246ms", LineSpeedTestPolicy.resultText(LineSpeedTestResult(latencyMs = 246)))
        assertEquals("不可用", LineSpeedTestPolicy.resultText(LineSpeedTestResult(failed = true)))
    }

    @Test fun resultKeyStaysWithTheSourceAcrossListReordering() {
        val line = LineInfo("vod:1", "线路一", "", "detail|source-a|3")
        assertEquals("detail|source-a|3", LineSpeedTestPolicy.key(line))
    }
}
