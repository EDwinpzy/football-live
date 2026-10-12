package com.qiubo.optimaltv.ui.search

import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
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

    @Test
    fun upMovesToTheSameColumnInThePreviousResultRow() {
        assertNull(previousSearchResultIndex(0))
        assertNull(previousSearchResultIndex(6))
        assertEquals(0, previousSearchResultIndex(7))
        assertEquals(1, previousSearchResultIndex(8))
        assertEquals(8, previousSearchResultIndex(15))
        assertNull(previousSearchResultIndex(-1))
        assertNull(previousSearchResultIndex(4, columns = 0))
    }

    @Test
    fun downMovesToTheSameColumnInTheNextResultRow() {
        assertEquals(7, nextSearchResultIndex(index = 0, itemCount = 14))
        assertEquals(13, nextSearchResultIndex(index = 6, itemCount = 14))
        assertNull(nextSearchResultIndex(index = 7, itemCount = 14))
        assertNull(nextSearchResultIndex(index = -1, itemCount = 14))
        assertNull(nextSearchResultIndex(index = 0, itemCount = 0))
        assertNull(nextSearchResultIndex(index = 0, itemCount = 14, columns = 0))
    }

    @Test
    fun aSearchResultMustBeFullyInsideTheGridViewportBeforeRequestingFocus() {
        assertTrue(isSearchResultItemFullyVisible(0, 350, 0, 500))
        assertTrue(isSearchResultItemFullyVisible(20, 480, 20, 480))
        assertFalse(isSearchResultItemFullyVisible(-1, 350, 0, 500))
        assertFalse(isSearchResultItemFullyVisible(150, 501, 0, 500))
        assertFalse(isSearchResultItemFullyVisible(100, 100, 0, 500))
    }
}
