package com.qiubo.optimaltv.ui.home

import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.spring
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.key
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/**
 * TV 分类滚轮：单一遥控器焦点节点，上下键选择分类，左右键进出片库。
 * 首尾之外留空，不循环显示另一端的分类。
 */
@Composable
internal fun LibraryCategoryWheel(
    categories: List<Pair<String, String>>,
    selectedIndex: Int,
    selectedTurn: Int,
    scale: Float,
    focusRequester: FocusRequester,
    onMove: (Int) -> Unit,
    onActivate: () -> Unit,
    onEnterLibrary: () -> Unit,
    onExitLibrary: () -> Unit,
    modifier: Modifier = Modifier,
) {
    if (categories.isEmpty()) return

    val rowHeight = 80f.sx(scale)
    val rowHeightPx = with(androidx.compose.ui.platform.LocalDensity.current) { rowHeight.toPx() }
    Box(
        modifier = modifier
            .fillMaxSize()
            .clipToBounds()
            .dpadFocusable(
                externalFocusRequester = focusRequester,
                scaleFocused = 1f,
                drawRing = false,
                navOverride = { direction ->
                    when (direction) {
                        NAV_UP -> { onMove(-1); true }
                        NAV_DOWN -> { onMove(1); true }
                        NAV_LEFT -> { onExitLibrary(); true }
                        NAV_RIGHT -> { onEnterLibrary(); true }
                        else -> false
                    }
                },
                onClick = onActivate,
            )
            .semantics {
                contentDescription = "影视分类，${categories[selectedIndex.coerceIn(categories.indices)].second}"
                stateDescription = categories[selectedIndex.coerceIn(categories.indices)].second
                selected = true
            },
        contentAlignment = Alignment.Center,
    ) {
        repeat(7) { slot ->
            val virtualPosition = selectedTurn + slot - 3
            val optionIndex = HomeLibraryUiPolicy.categoryWheelIndex(virtualPosition, categories.size) ?: return@repeat
            val title = categories[optionIndex].second
            val active = virtualPosition == selectedTurn && optionIndex == selectedIndex
            key(virtualPosition) {
                val itemOffset = remember { Animatable((slot - 3).toFloat()) }
                LaunchedEffect(slot) {
                    itemOffset.animateTo(
                        targetValue = (slot - 3).toFloat(),
                        animationSpec = spring(
                            dampingRatio = Spring.DampingRatioNoBouncy,
                            stiffness = Spring.StiffnessMedium,
                        ),
                    )
                }
                Box(
                    modifier = Modifier
                        .align(Alignment.Center)
                        .fillMaxWidth()
                        .height(rowHeight)
                        .graphicsLayer {
                            val wheelOffset = itemOffset.value
                            translationY = wheelOffset * rowHeightPx
                            alpha = HomeLibraryUiPolicy.categoryWheelAlpha(wheelOffset)
                            rotationX = (wheelOffset * 6f).coerceIn(-12f, 12f)
                            cameraDistance = 8f * density
                        },
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = title,
                        color = if (active) OtvColors.White else OtvColors.White.copy(alpha = 0.96f),
                        fontSize = 28f.sxs(scale),
                        fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
                        textAlign = TextAlign.Center,
                        maxLines = 1,
                        modifier = Modifier.graphicsLayer {
                            val wheelOffset = itemOffset.value
                            scaleX = HomeLibraryUiPolicy.categoryWheelScale(wheelOffset)
                            scaleY = HomeLibraryUiPolicy.categoryWheelScale(wheelOffset)
                        }.clearAndSetSemantics { },
                    )
                }
            }
        }
    }
}
