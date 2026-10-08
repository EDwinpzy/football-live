package com.qiubo.optimaltv.ui.favorites

import org.junit.Assert.assertEquals
import org.junit.Test

class FavoritesDownTargetTest {
    @Test fun disabledLicenseEmptyPageTargetsUpdate() {
        assertEquals(FavoritesDownTarget.UPDATE, favoritesDownTarget(false, false, false))
    }

    @Test fun disabledLicenseFavoritesTargetContent() {
        assertEquals(FavoritesDownTarget.CONTENT, favoritesDownTarget(false, true, false))
    }

    @Test fun disabledLicenseHistoryTargetsContent() {
        assertEquals(FavoritesDownTarget.CONTENT, favoritesDownTarget(false, false, true))
    }

    @Test fun disabledLicenseBothRowsTargetContent() {
        assertEquals(FavoritesDownTarget.CONTENT, favoritesDownTarget(false, true, true))
    }

    @Test fun enabledLicenseAlwaysTargetsVisibleMemberAction() {
        for (favorites in listOf(false, true)) for (history in listOf(false, true)) {
            assertEquals(FavoritesDownTarget.MEMBER, favoritesDownTarget(true, favorites, history))
        }
    }
}
