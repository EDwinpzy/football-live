package com.qiubo.optimaltv.ui.search

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SearchFocusPolicyTest {
    @Test
    fun retriesUntilTheResultCardReportsFocusOrTheBudgetRunsOut() {
        assertTrue(shouldRetryResultFocus(attempt = 0, focusObserved = false))
        assertTrue(shouldRetryResultFocus(attempt = 10, focusObserved = false))
        assertFalse(shouldRetryResultFocus(attempt = 0, focusObserved = true))
        assertFalse(shouldRetryResultFocus(attempt = RESULT_FOCUS_RETRY_ATTEMPTS - 1, focusObserved = false))
    }
}
