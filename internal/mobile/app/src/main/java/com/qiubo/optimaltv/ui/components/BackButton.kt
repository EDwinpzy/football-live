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
import com.qiubo.optimaltv.R

/**
 * 悬浮返回钮的占位（dp）：贴边 12dp + 触控命中区 44dp = 56dp，另加 8dp 呼吸。
 *
 * 页面顶部留白小于它时，返回钮会压住首屏内容：窄横屏上按设计 px 缩放的留白
 * （如 36f.sx ≈ 20dp）比返回钮底边还高，移动片库页首行「类别」标签就被压住了。
 * 需要让位的页面取「设计值」与它的较大者，与竖屏「顶部让位返回钮」同一套策略。
 */
val MobileBackButtonClearance = 64.dp

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
    onBack: () -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    Box(
        contentAlignment = Alignment.Center,
        modifier = modifier
            .padding(PaddingValues(start = 12.dp, top = 12.dp))
            .size(44.dp)
            .testTag(MobileUiTags.BackButton)
            .clickable(interactionSource = interaction, indication = null, onClick = onBack),
    ) {
        Icon(
            painter = painterResource(R.drawable.ic_back_chevron),
            contentDescription = "返回",
            tint = tint,
            modifier = Modifier.size(26.dp),
        )
    }
}
