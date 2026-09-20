package com.qiubo.optimaltv.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Icon
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.Dp
import com.qiubo.optimaltv.R

/**
 * 悬浮返回钮贴边留白（dp）：左/上各 12dp，与 [MobileBackButton] 内部 padding 同值。
 *
 * 需要与返回钮「顶部对齐」的页面（片库页筛选面板，2026-09-18 用户要求）直接拿它做
 * 内容顶边距，不要在页面里另写 12dp——两处各写会漂移，对齐就破了。
 */
val MobileBackButtonInset = 12.dp

/** 悬浮返回钮触控命中区边长（dp）：贴边 12 + 44 = 56 是页面顶部内容的最低安全线。 */
val MobileBackButtonTouchTarget = 44.dp

/**
 * 移动版返回按钮（总体#6 2026-09-03 加；2026-09-04 二次换装）：
 * 纯白色粗折角 ‹ 图标，无背景方块/描边（需求①：图标不要背景那个框），贴屏幕左上角。
 *
 * ⚠️ z-order 契约（移动① 修复）：本按钮必须画在页面根 Box 的【最后一个子节点】，
 * 否则会被后声明的 LazyGrid/Column 等可滚动内容盖住而点不到（clickable 收不到事件）。
 * 内部自带安全区 padding（start 12 / top 12 dp），调用方不要再叠 padding。
 * 触摸热区保持 44dp（无障碍命中），只是视觉上只剩图标。
 */
@Composable
fun MobileBackButton(
    modifier: Modifier = Modifier,
    tint: Color = Color.White,
    iconSize: Dp = 26.dp,
    onBack: () -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    Box(
        contentAlignment = Alignment.Center,
        modifier = modifier
            .padding(PaddingValues(start = MobileBackButtonInset, top = MobileBackButtonInset))
            .size(MobileBackButtonTouchTarget)
            .testTag(MobileUiTags.BackButton)
            .clickable(interactionSource = interaction, indication = null, onClick = onBack),
    ) {
        Icon(
            painter = painterResource(R.drawable.ic_back_chevron),
            contentDescription = "返回",
            tint = tint,
            modifier = Modifier.size(iconSize),
        )
    }
}
