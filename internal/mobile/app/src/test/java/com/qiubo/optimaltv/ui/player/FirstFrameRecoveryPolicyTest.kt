package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class FirstFrameRecoveryPolicyTest {

    @Test
    fun waitsDuringTheHealthyWindow() {
        assertEquals(FirstFrameRecoveryPolicy.Action.WAIT, FirstFrameRecoveryPolicy.actionFor(0L))
        assertEquals(FirstFrameRecoveryPolicy.Action.WAIT, FirstFrameRecoveryPolicy.actionFor(2_999L))
    }

    @Test
    fun preparesBackupBetween3And5Seconds() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.PREPARE_BACKUP,
            FirstFrameRecoveryPolicy.actionFor(3_000L),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.PREPARE_BACKUP,
            FirstFrameRecoveryPolicy.actionFor(4_999L),
        )
    }

    @Test
    fun escalatesBetween5And7Seconds() {
        assertEquals(FirstFrameRecoveryPolicy.Action.ESCALATE, FirstFrameRecoveryPolicy.actionFor(5_000L))
        assertEquals(FirstFrameRecoveryPolicy.Action.ESCALATE, FirstFrameRecoveryPolicy.actionFor(6_999L))
    }

    @Test
    fun escalatesBetween7And15Seconds() {
        // 7s 之后仍可再升级一次（第二次换线/降级），到 15s 才硬放弃。
        assertEquals(FirstFrameRecoveryPolicy.Action.ESCALATE, FirstFrameRecoveryPolicy.actionFor(7_000L))
        assertEquals(FirstFrameRecoveryPolicy.Action.ESCALATE, FirstFrameRecoveryPolicy.actionFor(14_999L))
    }

    @Test
    fun givesUpAtHardTimeout() {
        assertEquals(FirstFrameRecoveryPolicy.Action.GIVE_UP, FirstFrameRecoveryPolicy.actionFor(15_000L))
        assertEquals(FirstFrameRecoveryPolicy.Action.GIVE_UP, FirstFrameRecoveryPolicy.actionFor(40_000L))
    }

    @Test
    fun classifiesDecoderFailuresAsDecoder() {
        assertEquals(
            FirstFrameRecoveryPolicy.FailureClass.DECODER,
            FirstFrameRecoveryPolicy.classify("MediaCodec AVC High 8x8 硬解失败"),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.FailureClass.DECODER,
            FirstFrameRecoveryPolicy.classify("ExoPlaybackException.DECODING_FAILED"),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Remedy.SWITCH_ENGINE,
            FirstFrameRecoveryPolicy.remedyFor("no suitable decoder for video/avc"),
        )
    }

    @Test
    fun classifiesNetworkFailuresAsNetwork() {
        assertEquals(
            FirstFrameRecoveryPolicy.FailureClass.NETWORK,
            FirstFrameRecoveryPolicy.classify("SocketTimeoutException: timeout"),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.FailureClass.NETWORK,
            FirstFrameRecoveryPolicy.classify("Unable to resolve host www.example.com"),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Remedy.SWITCH_SOURCE,
            FirstFrameRecoveryPolicy.remedyFor("Connection reset by peer"),
        )
    }

    @Test
    fun decoderSignalWinsOverNetworkSignalWhenBothPresent() {
        // 真实形态："decoder initialization failed after network reset"
        // 判错方向 = 反复换同一条源，永远出不了画面，所以解码特征必须优先。
        val reason = "decoder init failed after network reset"
        assertEquals(FirstFrameRecoveryPolicy.FailureClass.DECODER, FirstFrameRecoveryPolicy.classify(reason))
        assertNotEquals(
            FirstFrameRecoveryPolicy.FailureClass.NETWORK,
            FirstFrameRecoveryPolicy.classify(reason),
        )
    }

    @Test
    fun unknownReasonFallsBackToSwitchingSource() {
        // UNKNOWN 换源而不是换引擎：换引擎代价更高，且多数故障其实是取流问题。
        assertEquals(
            FirstFrameRecoveryPolicy.FailureClass.UNKNOWN,
            FirstFrameRecoveryPolicy.classify("some opaque failure"),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Remedy.SWITCH_SOURCE,
            FirstFrameRecoveryPolicy.remedyFor("some opaque failure"),
        )
    }
}
