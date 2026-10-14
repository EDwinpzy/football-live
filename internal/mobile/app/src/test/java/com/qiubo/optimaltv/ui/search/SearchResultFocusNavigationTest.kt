package com.qiubo.optimaltv.ui.search

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SearchResultFocusNavigationTest {
    private fun target(index: Int, count: Int, direction: SearchResultFocusDirection) =
        mobileSearchResultFocusTarget(index, count, columns = 3, direction = direction)

    @Test
    fun oneResultHasNoGridNeighbor() {
        SearchResultFocusDirection.values().forEach { direction ->
            assertNull(target(index = 0, count = 1, direction = direction))
        }
    }

    @Test
    fun twoResultsStayOnTheSameRow() {
        assertEquals(1, target(0, 2, SearchResultFocusDirection.Right))
        assertNull(target(1, 2, SearchResultFocusDirection.Right))
        assertNull(target(0, 2, SearchResultFocusDirection.Left))
        assertNull(target(0, 2, SearchResultFocusDirection.Down))
    }

    @Test
    fun threeResultsNavigateHorizontallyAndStopAtTheRowEdges() {
        assertEquals(1, target(0, 3, SearchResultFocusDirection.Right))
        assertEquals(2, target(1, 3, SearchResultFocusDirection.Right))
        assertNull(target(2, 3, SearchResultFocusDirection.Right))
        assertEquals(1, target(2, 3, SearchResultFocusDirection.Left))
    }

    @Test
    fun twentyResultsNavigateRowsAndClampToThePartialLastRow() {
        assertEquals(3, target(0, 20, SearchResultFocusDirection.Down))
        assertEquals(15, target(18, 20, SearchResultFocusDirection.Up))
        assertEquals(19, target(17, 20, SearchResultFocusDirection.Down))
        assertEquals(19, target(18, 20, SearchResultFocusDirection.Right))
        assertNull(target(19, 20, SearchResultFocusDirection.Right))
        assertNull(target(18, 20, SearchResultFocusDirection.Down))
        assertNull(target(19, 20, SearchResultFocusDirection.Down))
    }

    @Test
    fun invalidPositionsAndColumnCountsHaveNoTarget() {
        assertNull(mobileSearchResultFocusTarget(-1, 3, 3, SearchResultFocusDirection.Right))
        assertNull(mobileSearchResultFocusTarget(3, 3, 3, SearchResultFocusDirection.Left))
        assertNull(mobileSearchResultFocusTarget(0, 1, 0, SearchResultFocusDirection.Down))
    }
}
