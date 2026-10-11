package com.qiubo.optimaltv.ui.home

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/** TV 影视分类单列平铺；文字沿用旧滚轮的字号、字重、颜色和选中放大效果。 */
@Composable
internal fun LibraryCategoryTiles(
    categories: List<Pair<String, String>>,
    selectedIndex: Int,
    scale: Float,
    focusRequesters: List<FocusRequester>,
    onSelect: (Int) -> Unit,
    onEnterContent: () -> Unit,
    onExitToHero: () -> Unit,
    modifier: Modifier = Modifier,
) {
    if (categories.isEmpty()) return

    Column(modifier = modifier.fillMaxSize()) {
        categories.forEachIndexed { index, category ->
            val selected = selectedIndex == index
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier
                    .dpadFocusable(
                        externalFocusRequester = focusRequesters.getOrNull(index),
                        scaleFocused = 1f,
                        drawRing = false,
                        onFocusedChange = { if (it) onSelect(index) },
                        navOverride = { direction ->
                            val target = HomeLibraryUiPolicy.categoryGridTargetIndex(
                                index = index,
                                direction = direction,
                                count = categories.size,
                                columns = 1,
                            )
                            if (target != null) {
                                runCatching { focusRequesters[target].requestFocus() }
                                true
                            } else {
                                when (direction) {
                                    NAV_LEFT -> {
                                        onExitToHero()
                                        true
                                    }
                                    NAV_RIGHT -> {
                                        onEnterContent()
                                        true
                                    }
                                    NAV_UP, NAV_DOWN -> true
                                    else -> false
                                }
                            }
                        },
                        onClick = onEnterContent,
                    )
                    .fillMaxWidth()
                    .height(80f.sx(scale))
                    .semantics {
                        contentDescription = category.second
                        stateDescription = category.second
                        this.selected = selected
                    },
            ) {
                Text(
                    text = category.second,
                    color = if (selected) OtvColors.White else OtvColors.White.copy(alpha = 0.96f),
                    fontSize = 28f.sxs(scale),
                    fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.graphicsLayer {
                        // The old wheel used 1.30 at its selected center position.
                        val selectedScale = if (selected) 1.30f else 1f
                        scaleX = selectedScale
                        scaleY = selectedScale
                    },
                )
            }
        }
    }
}
