package com.qiubo.optimaltv.ui.search

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SearchInputFocusRecoveryTest {
    @Test
    fun remoteUpdateCannotRestoreInputAfterCategoryOrResultTakesFocus() {
        val recovery = SearchInputFocusRecovery<String>()
        // Candidate -> category -> result; remote completion arrives inside the old 600ms window.
        recovery.onInputLost(100, "keyboard")
        recovery.onFocusGained()
        recovery.onFocusGained()
        assertNull(recovery.takeRestoreTarget(250))
    }

    @Test
    fun vanishedInputRestoresItsOwnKeyOnlyOnce() {
        val recovery = SearchInputFocusRecovery<String>()
        recovery.onInputLost(100, "key-z")
        assertEquals("key-z", recovery.takeRestoreTarget(250))
        assertNull(recovery.takeRestoreTarget(300))
    }

    @Test
    fun oldLossCannotInterruptLaterNavigation() {
        val recovery = SearchInputFocusRecovery<String>()
        recovery.onInputLost(100, "keyboard")
        assertNull(recovery.takeRestoreTarget(700))
        recovery.onInputLost(800, "key-b")
        recovery.onFocusGained()
        recovery.onInputLost(900, "key-c")
        assertEquals("key-c", recovery.takeRestoreTarget(1000))
    }
}
