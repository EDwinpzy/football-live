package com.qiubo.optimaltv.ui.favorites

import org.junit.Assert.assertEquals
import org.junit.Test

class FavoritesDownTargetTest {
    @Test fun unavailableMemberActionEmptyPageTargetsUpdate() {
        assertEquals(FavoritesDownTarget.UPDATE, favoritesDownTarget(false, false, false))
    }

    @Test fun unavailableMemberActionWithFavoritesTargetsContent() {
        assertEquals(FavoritesDownTarget.CONTENT, favoritesDownTarget(false, true, false))
    }

    @Test fun unavailableMemberActionWithHistoryTargetsContent() {
        assertEquals(FavoritesDownTarget.CONTENT, favoritesDownTarget(false, false, true))
    }

    @Test fun unavailableMemberActionWithBothRowsTargetsContent() {
        assertEquals(FavoritesDownTarget.CONTENT, favoritesDownTarget(false, true, true))
    }

    @Test fun visibleMemberActionRemainsReachableBeforeContent() {
        for (favorites in listOf(false, true)) for (history in listOf(false, true)) {
            assertEquals(FavoritesDownTarget.MEMBER, favoritesDownTarget(true, favorites, history))
        }
    }
}
