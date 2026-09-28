package com.qiubo.optimaltv.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.TextPrimary

@Composable
fun PosterCard(
    item: VodItem,
    width: Dp = 180.dp,
    onClick: () -> Unit,
) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.width(width)) {
        Box(
            modifier = Modifier
                .width(width)
                .aspectRatio(5f / 7f)
                .dpadFocusable(onClick = onClick)
                .clip(RoundedCornerShape(14.dp)),
        ) {
            // 占位渐变垫底，图片加载失败时自然露出
            val seed = item.id.hashCode()
            PosterPlaceholder(seed = seed, modifier = Modifier.matchParentSize())
            if (item.posterUrl.isNotBlank()) {
                AsyncImage(
                    model = item.posterUrl,
                    contentDescription = item.title,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
            // 底部渐变 + 标题
            Box(
                Modifier
                    .matchParentSize()
                    .background(Brush.verticalGradient(listOf(Color.Transparent, Color(0xCC000000)))),
            )
            Text(
                text = item.title,
                style = MaterialTheme.typography.bodyMedium,
                color = TextPrimary,
                maxLines = 2,
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .padding(10.dp),
            )
            // remark 角标（原版 vcard .remark：右下 黑底白字）
            // 需求②：字号加大（12→16）+ 行高压到与字号同高——黑底长方形更扁
            if (item.remark.isNotBlank()) {
                Text(
                    text = item.remark,
                    color = Color.White,
                    fontSize = 16.sp,
                    lineHeight = 16.sp,
                    maxLines = 1,
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(6.dp)
                        .background(Color(0xCC0E0E0F), RoundedCornerShape(4.dp))
                        .padding(horizontal = 7.dp, vertical = 1.dp),
                )
            }
            if (item.rating > 0) {
                Text(
                    text = "%.1f".format(item.rating),
                    style = MaterialTheme.typography.labelMedium,
                    color = Color.White,
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(8.dp)
                        .background(AccentBlue.copy(alpha = 0.85f), RoundedCornerShape(8.dp))
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                )
            }
        }
    }
}

/** 首页横向推荐行（方案 §3.4 首页：推荐行×横向滚动） */
@Composable
fun SectionRow(title: String, items: List<VodItem>, onOpen: (VodItem) -> Unit) {
    if (items.isEmpty()) return
    Column(modifier = Modifier.padding(vertical = 10.dp)) {
        Text(
            text = title,
            style = MaterialTheme.typography.titleMedium,
            color = TextPrimary,
            modifier = Modifier.padding(start = 48.dp, bottom = 12.dp),
        )
        LazyRow(
            contentPadding = PaddingValues(horizontal = 48.dp),
            horizontalArrangement = Arrangement.spacedBy(22.dp),
        ) {
            // 方案 §11.1：LazyRow 的 item 必须用**稳定业务 key**（影片 id），不能用
            // index。原先这里是 `items(items.size)` —— 完全没有 key，Compose 退化成按
            // 位置复用：这一行的数据整体刷新时，焦点/滚动/图片加载状态会留在**原来那个
            // 位置**上而不是留在那部影片上，表现为「换了内容焦点跳到别的卡片」。
            items(items.size, key = { i -> items[i].id }) { i ->
                PosterCard(item = items[i]) { onOpen(items[i]) }
            }
        }
    }
}
