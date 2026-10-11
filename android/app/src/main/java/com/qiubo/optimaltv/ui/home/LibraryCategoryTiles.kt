package com.qiubo.optimaltv.ui.home

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/** TV 影视分类平铺导航，焦点沿两列网格移动；到右边缘进入当前片库内容。 */
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

    LazyVerticalGrid(
        columns = GridCells.Fixed(2),
        userScrollEnabled = false,
        horizontalArrangement = Arrangement.spacedBy(10f.sx(scale)),
        verticalArrangement = Arrangement.spacedBy(12f.sx(scale)),
        modifier = modifier.fillMaxSize(),
    ) {
        itemsIndexed(categories, key = { index, category -> "vod-category:${category.first}:$index" }) { index, category ->
            val selected = selectedIndex == index
            var focused by remember { mutableStateOf(false) }
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier
                    .dpadFocusable(
                        externalFocusRequester = focusRequesters.getOrNull(index),
                        scaleFocused = 1.04f,
                        focusedBg = OtvColors.White,
                        focusedBgRadius = 12f.sx(scale),
                        onFocusedChange = {
                            focused = it
                            if (it) onSelect(index)
                        },
                        navOverride = { direction ->
                            val target = HomeLibraryUiPolicy.categoryGridTargetIndex(
                                index = index,
                                direction = direction,
                                count = categories.size,
                            )
                            if (target != null) {
                                runCatching { focusRequesters[target].requestFocus() }
                                true
                            } else {
                                when (direction) {
                                    NAV_LEFT -> if (index % 2 == 0) {
                                        onExitToHero()
                                        true
                                    } else false
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
                    .height(72f.sx(scale))
                    .background(
                        color = if (selected && !focused) OtvColors.White.copy(alpha = 0.16f) else OtvColors.ChipBg,
                        shape = RoundedCornerShape(12f.sx(scale)),
                    )
                    .padding(horizontal = 8f.sx(scale)),
            ) {
                Text(
                    text = category.second,
                    color = if (focused) OtvColors.Bg else OtvColors.White,
                    fontSize = 20f.sxs(scale),
                    fontWeight = if (selected || focused) FontWeight.SemiBold else FontWeight.Medium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}
