package com.qiubo.optimaltv.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import coil.compose.AsyncImage
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/** 稳定的 UI 测试/无障碍语义标识；文案变化不会让自动化定位失效。 */
object MobileUiTags {
    const val MainTabBar = "mobile-main-tab-bar"
    const val SearchTab = "mobile-tab-search"
    const val BackButton = "mobile-back-button"
    const val FilterToggle = "mobile-vod-filter-toggle"
    const val DetailPlay = "detail-action-play"
    const val DetailFavorite = "detail-action-favorite"
    const val DetailRestart = "detail-action-restart"
    const val PlayerSeek = "player-portrait-seek"
    const val PlayerPlayPause = "player-portrait-play-pause"
    const val PlayerFullscreen = "player-portrait-fullscreen"
    const val PlayerMore = "player-portrait-more"

    fun mainTab(key: String) = "mobile-tab-$key"
    fun category(id: String) = "vod-category-${id.ifBlank { "all" }}"
    fun filter(label: String, value: String) = "vod-filter-$label-${value.ifBlank { "all" }}"
    fun card(id: String) = "vod-card-$id"
    fun cardPoster(id: String) = "vod-card-$id-poster"
}

/** 详情页主操作按钮，保留生产视觉并暴露稳定语义供自动化验证。 */
@Composable
fun RowScope.DetailActionButton(
    text: String,
    primary: Boolean,
    fill: Boolean,
    s: Float,
    testTag: String,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .then(if (fill) Modifier.weight(1f) else Modifier)
            .clip(RoundedCornerShape(9.7f.sx(s)))
            .testTag(testTag)
            .semantics { role = Role.Button }
            .then(if (enabled) Modifier.tapCard(onClick) else Modifier)
            .background(
                if (primary) OtvColors.White.copy(alpha = 0.5f) else Color(0x2EFFFFFF),
                RoundedCornerShape(9.7f.sx(s)),
            )
            .widthIn(min = if (fill) 0f.sx(s) else 280f.sx(s))
            .height(64f.sx(s))
            .padding(horizontal = 46f.sx(s)),
    ) {
        Text(
            text,
            fontSize = 29f.sxs(s),
            fontWeight = FontWeight.Bold,
            color = if (!enabled) OtvColors.White50 else if (primary) OtvColors.Bg else OtvColors.White,
        )
    }
}

/**
 * 影视海报卡的单一生产实现：首页、全部、搜索共用，避免三个页面样式/评分规则漂移。
 * 2026-09-17 用户要求：卡片下方不再显示豆瓣评分与年份（竖屏/横屏一致），只留片名。
 */
@Composable
fun MobileVodCard(
    item: VodItem,
    s: Float,
    compact: Boolean,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Column(modifier.fillMaxWidth().testTag(MobileUiTags.card(item.id))) {
        Box(
            Modifier
                .clip(RoundedCornerShape(12f.sx(s)))
                .testTag(MobileUiTags.cardPoster(item.id))
                .tapCard(onClick)
                .fillMaxWidth()
                .aspectRatio(232f / 352f),
        ) {
            PosterPlaceholder(seed = item.id.hashCode(), modifier = Modifier.fillMaxSize())
            if (item.posterUrl.isNotBlank()) {
                AsyncImage(
                    model = item.posterUrl,
                    contentDescription = item.title,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
            if (item.remark.isNotBlank()) {
                Text(
                    item.remark,
                    color = OtvColors.White,
                    fontSize = (if (compact) 16f else 20f).sxs(s),
                    lineHeight = (if (compact) 16f else 20f).sxs(s),
                    maxLines = 1,
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(6f.sx(s))
                        .background(Color(0xCC0E0E0F), RoundedCornerShape(4f.sx(s)))
                        .padding(horizontal = 7f.sx(s), vertical = 1f.sx(s)),
                )
            }
        }
        Spacer(Modifier.height(10f.sx(s)))
        Text(
            item.title,
            color = OtvColors.White,
            fontSize = (if (compact) 20f else 24f).sxs(s),
            fontWeight = FontWeight.Medium,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** 首页影视分类：竖屏固定 3×2，横屏单行横向排列。 */
@Composable
fun VodCategorySelector(
    categories: List<Pair<String, String>>,
    selectedId: String,
    portrait: Boolean,
    s: Float,
    sidePadding: Dp,
    onSelect: (String) -> Unit,
    onAll: () -> Unit,
) {
    @Composable
    fun Chip(id: String, name: String, modifier: Modifier, landscape: Boolean) {
        val active = id == selectedId
        val isAll = id.isBlank()
        Box(
            contentAlignment = Alignment.Center,
            modifier = modifier
                .clip(RoundedCornerShape((if (landscape) 32f else 28f).sx(s)))
                .testTag(MobileUiTags.category(id))
                .semantics { selected = active; role = Role.Tab }
                .tapCard { if (isAll) onAll() else onSelect(id) }
                .background(OtvColors.ChipBg, RoundedCornerShape((if (landscape) 32f else 28f).sx(s)))
                .height((if (landscape) 64f else 56f).sx(s))
                .then(if (landscape) Modifier.padding(horizontal = if (isAll) 34f.sx(s) else 30f.sx(s)) else Modifier),
        ) {
            Text(
                name,
                color = if (active) OtvColors.White else OtvColors.White.copy(alpha = 0.6f),
                fontSize = (if (landscape) 29f else 25f).sxs(s),
                fontWeight = FontWeight.Medium,
                maxLines = 1,
            )
        }
    }

    if (portrait) {
        Column(
            verticalArrangement = Arrangement.spacedBy(12f.sx(s)),
            modifier = Modifier.fillMaxWidth().padding(horizontal = sidePadding).padding(top = 40f.sx(s)),
        ) {
            categories.chunked(3).forEach { categoryRow ->
                Row(
                    horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    categoryRow.forEach { (id, name) -> Chip(id, name, Modifier.weight(1f), landscape = false) }
                    repeat(3 - categoryRow.size) { Spacer(Modifier.weight(1f)) }
                }
            }
        }
    } else {
        LazyRow(
            horizontalArrangement = Arrangement.spacedBy(18f.sx(s)),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = sidePadding),
            modifier = Modifier.fillMaxWidth().padding(top = 40f.sx(s)),
        ) {
            items(categories, key = { it.first.ifBlank { "all" } }) { (id, name) ->
                Chip(id, name, Modifier, landscape = true)
            }
        }
    }
}

/** 全部页筛选行：竖屏使用可换行 FlowRow，横屏维持单行横向滚动。 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun VodFilterRow(
    label: String,
    chips: List<Pair<String, String>>,
    selected: String,
    s: Float,
    portrait: Boolean,
    onSelect: (String) -> Unit,
) {
    val selectedPillMinWidth = 72f.sx(s)

    @Composable
    fun FilterChip(value: String, name: String) {
        val active = selected == value
        // 「轨」样式（2026-09-19 设计稿 v2 方案①）：未选=裸文字（白 55%），选中=白胶囊黑字加粗；
        // 颜色随选中态 200ms 渐变，模拟胶囊滑入感（tapCard 自带按压 0.96 缩放）
        val chipBg by animateColorAsState(
            if (active) OtvColors.White else Color.Transparent, label = "filterChipBg",
        )
        val chipFg by animateColorAsState(
            if (active) OtvColors.Bg else OtvColors.White55, label = "filterChipFg",
        )
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .clip(RoundedCornerShape(percent = 50))
                .testTag(MobileUiTags.filter(label, value))
                .semantics { this.selected = active; role = Role.Tab }
                .tapCard { onSelect(value) }
                .then(if (active) Modifier.widthIn(min = selectedPillMinWidth) else Modifier)
                .background(chipBg, RoundedCornerShape(percent = 50))
                .padding(horizontal = 12f.sx(s), vertical = 4f.sx(s)),
        ) {
            Text(
                name,
                color = chipFg,
                fontSize = 16f.sxs(s),
                fontWeight = if (active) FontWeight.Bold else FontWeight.Medium,
                maxLines = 1,
            )
        }
    }

    if (portrait) {
        Row(
            verticalAlignment = Alignment.Top,
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 6f.sx(s)),
        ) {
            Text(
                label,
                color = OtvColors.White.copy(alpha = 0.35f),
                fontSize = 16f.sxs(s),
                fontWeight = FontWeight.Medium,
                modifier = Modifier
                    .width(52f.sx(s))
                    .padding(top = 4f.sx(s)),
            )
            Spacer(Modifier.width(8f.sx(s)))
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(4f.sx(s)),
                verticalArrangement = Arrangement.spacedBy(14f.sx(s)),
                modifier = Modifier.weight(1f),
            ) { chips.forEach { (value, name) -> FilterChip(value, name) } }
        }
    } else {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(vertical = 4f.sx(s)),
        ) {
            Text(
                label,
                color = OtvColors.White.copy(alpha = 0.35f),
                fontSize = 17f.sxs(s),
                fontWeight = FontWeight.Medium,
                modifier = Modifier.width(52f.sx(s)),
            )
            Spacer(Modifier.width(8f.sx(s)))
            Row(
                horizontalArrangement = Arrangement.spacedBy(2f.sx(s)),
                modifier = Modifier.horizontalScroll(rememberScrollState()),
            ) { chips.forEach { (value, name) -> FilterChip(value, name) } }
        }
    }
}
