package com.qiubo.optimaltv.ui.player

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.qiubo.optimaltv.data.cast.DlnaDevice
import com.qiubo.optimaltv.data.prefs.AspectMode
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.TextPrimary

/**
 * 「更多」区段内容（2026-09-19 v2 设计稿定稿）：影片信息 + 简介 + 选集 + 换源 + 倍速 + 画幅 + 投屏。
 * 横屏右侧抽屉（MorePanel）与竖屏视频下方常驻内容区（PortraitPlayerPanel）共用同一份区段，
 * 视觉 / 数据 / 动作 1:1；动作全部以回调注入，便于无 ViewModel 的自动化回归。
 */
@Composable
internal fun PlayerMoreSections(
    ui: PlayerUiState,
    onPickEpisode: (Int) -> Unit,
    onSwitchLine: (Int) -> Unit,
    onSetSpeed: (Float) -> Unit,
    onSetAspect: (Int) -> Unit,
    onStartCast: (DlnaDevice) -> Unit,
    onStopCast: () -> Unit,
    onRescanCast: () -> Unit,
    posterWidth: Dp = 96.dp,
    posterHeight: Dp = 136.dp,
) {
    // 打开即扫一次局域网，设备自动列在「投屏」下方（用户要求：不用再点一次才出列表）
    LaunchedEffect(Unit) { onRescanCast() }
    // 影片信息
    Row(verticalAlignment = Alignment.Top) {
        if (ui.posterUrl.isNotBlank()) {
            AsyncImage(
                model = ui.posterUrl,
                contentDescription = ui.title,
                contentScale = ContentScale.Crop,
                modifier = Modifier
                    .width(posterWidth)
                    .height(posterHeight)
                    .clip(RoundedCornerShape(10.dp)),
            )
            Spacer(Modifier.width(18.dp))
        }
        Column(Modifier.weight(1f)) {
            Text(
                ui.title.ifBlank { "影片" },
                color = TextPrimary, style = MaterialTheme.typography.titleMedium,
                maxLines = 2, overflow = TextOverflow.Ellipsis,
            )
            if (ui.meta.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(ui.meta, color = TextPrimary.copy(alpha = 0.6f), style = MaterialTheme.typography.bodySmall, maxLines = 2)
            }
            if (ui.lines.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    "线路${ui.activeLine + 1}/${ui.lines.size} · ${ui.engineName}",
                    color = TextPrimary.copy(alpha = 0.5f), style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
    if (ui.desc.isNotBlank()) {
        Spacer(Modifier.height(14.dp))
        Text(
            ui.desc, color = TextPrimary.copy(alpha = 0.7f), style = MaterialTheme.typography.bodySmall,
            maxLines = 4, overflow = TextOverflow.Ellipsis,
        )
    }
    Spacer(Modifier.height(20.dp))
    // 选集（需求⑨修订：电影不显示选集区，电视剧/动漫/短剧保留；
    // ≥20 集按 10 集/页 + 分页 chip；<20 集全量展示）
    if (!ui.isMovie && ui.epCount > 1) {
        SectionLabel("选择集数")
        val pageSize = EpisodeMenuPolicy.RANGE_SIZE
        val pageCount = (ui.epCount + pageSize - 1) / pageSize
        var page by remember(ui.epIndex / pageSize) { mutableIntStateOf(ui.epIndex / pageSize) }
        if (EpisodeMenuPolicy.hasRanges(ui.epCount)) Row(
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier
                .padding(bottom = 10.dp)
                .horizontalScroll(rememberScrollState()),
        ) {
            for (p in 0 until pageCount) {
                val from = p * pageSize + 1
                val to = minOf((p + 1) * pageSize, ui.epCount)
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(10.dp))
                        .tapCard { page = p }
                        .background(
                            if (p == page) AccentBlue else Color(0x24FFFFFF),
                            RoundedCornerShape(10.dp),
                        )
                        .padding(horizontal = 14.dp, vertical = 8.dp),
                ) {
                    Text("第$from-${to}集", color = Color.White, style = MaterialTheme.typography.labelMedium)
                }
            }
        }
        // 2026-09-19 集数不全修复：展示范围收口到 EpisodeMenuPolicy.visibleRange
        //（<20 集全量；旧版 11~19 集落在无 chip 的第 2 页，前 10 集够不着；TV 版同修）
        val pageEps = EpisodeMenuPolicy.visibleRange(ui.epCount, page)
        LazyVerticalGrid(
            columns = GridCells.Fixed(4),
            modifier = Modifier.height(((pageEps.count() + 3) / 4 * 52).dp),
        ) {
            items(pageEps.toList()) { i ->
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .padding(4.dp)
                        .clip(RoundedCornerShape(10.dp))
                        .tapCard { onPickEpisode(i) }
                        .background(
                            if (i == ui.epIndex) AccentBlue else Color(0x24FFFFFF),
                            RoundedCornerShape(10.dp),
                        )
                        .padding(vertical = 10.dp),
                ) {
                    Text(
                        EpisodeMenuPolicy.episodeLabel(i),
                        color = Color.White, style = MaterialTheme.typography.labelMedium,
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
        Spacer(Modifier.height(18.dp))
    }
    // 换源（horizontalScroll：14+ 条线路不裁切）
    if (ui.lines.size > 1) {
        SectionLabel("换源线路")
        Row(
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier.horizontalScroll(rememberScrollState()),
        ) {
            ui.lines.forEachIndexed { idx, line ->
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(10.dp))
                        .tapCard { onSwitchLine(idx) }
                        .background(
                            if (idx == ui.activeLine) AccentBlue else Color(0x24FFFFFF),
                            RoundedCornerShape(10.dp),
                        )
                        .padding(horizontal = 14.dp, vertical = 9.dp),
                ) {
                    Text(line.label, color = Color.White, style = MaterialTheme.typography.labelMedium, maxLines = 1)
                }
            }
        }
        Spacer(Modifier.height(18.dp))
    }
    // 倍速（直播不可变速，不渲染本区）
    if (!ui.isLive) {
        SectionLabel("倍速")
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            PlayerViewModel.SPEED_STEPS.forEach { sp ->
                val active = kotlin.math.abs(sp - ui.speed) < 0.01f
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(10.dp))
                        .tapCard { onSetSpeed(sp) }
                        .background(
                            if (active) AccentBlue else Color(0x24FFFFFF),
                            RoundedCornerShape(10.dp),
                        )
                        .padding(horizontal = 12.dp, vertical = 9.dp),
                ) {
                    Text("%.1fx".format(sp), color = Color.White, style = MaterialTheme.typography.labelMedium)
                }
            }
        }
        Spacer(Modifier.height(18.dp))
    }
    // 画幅
    SectionLabel("画幅")
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        AspectMode.entries.forEach { mode ->
            val active = mode.ordinal == ui.aspectOrdinal
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier
                    .clip(RoundedCornerShape(10.dp))
                    .tapCard { onSetAspect(mode.ordinal) }
                    .background(
                        if (active) AccentBlue else Color(0x24FFFFFF),
                        RoundedCornerShape(10.dp),
                    )
                    .padding(horizontal = 12.dp, vertical = 9.dp),
            ) {
                Text(mode.label, color = Color.White, style = MaterialTheme.typography.labelMedium)
            }
        }
    }
    Spacer(Modifier.height(18.dp))
    // 投屏（2026-09-17 用户要求）：入口挪到「画幅」下面，可投屏设备自动列在下方，
    // 不再需要先点一次按钮才出列表、也不弹独立底部 sheet。
    SectionLabel("投屏")
    val otherCastDevices = ui.castDevices.filter { it.uuid != ui.castDevice?.uuid }
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        // 投屏中设备：置顶显示断开
        ui.castDevice?.let { dev ->
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(Color(0x240A84FF))
                    .clickable { onStopCast() }
                    .padding(horizontal = 18.dp, vertical = 14.dp),
            ) {
                CastGlyph(24.dp, AccentBlue)
                Spacer(Modifier.width(12.dp))
                Text(
                    "正在投屏 · ${dev.name}", color = AccentBlue, modifier = Modifier.weight(1f),
                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
                Text("断开", color = TextPrimary.copy(alpha = 0.7f), style = MaterialTheme.typography.labelMedium)
            }
        }
        otherCastDevices.forEach { dev ->
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(Color(0x24FFFFFF))
                    .clickable { onStartCast(dev) }
                    .padding(horizontal = 18.dp, vertical = 14.dp),
            ) {
                CastGlyph(24.dp, TextPrimary.copy(alpha = 0.7f))
                Spacer(Modifier.width(12.dp))
                Text(
                    dev.name, color = TextPrimary, modifier = Modifier.weight(1f),
                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
                Text("投屏", color = Color.White, style = MaterialTheme.typography.labelMedium)
            }
        }
        if (otherCastDevices.isEmpty() && ui.castDevice == null) {
            val castHint = ui.castError
            Text(
                when {
                    ui.castScanning -> "正在扫描局域网…"
                    castHint != null -> castHint
                    else -> "未发现设备（电视需与本机同一 Wi-Fi）"
                },
                color = if (castHint != null && !ui.castScanning) Color(0xFFFFB84D)
                        else TextPrimary.copy(alpha = 0.55f),
                style = MaterialTheme.typography.bodySmall,
            )
        }
        Row(
            horizontalArrangement = Arrangement.Center,
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(12.dp))
                .border(0.5.dp, Color(0x33FFFFFF), RoundedCornerShape(12.dp))
                .clickable { onRescanCast() }
                .padding(horizontal = 18.dp, vertical = 12.dp),
        ) {
            Text("重新扫描", color = TextPrimary.copy(alpha = 0.8f), style = MaterialTheme.typography.labelMedium)
        }
    }
    Spacer(Modifier.height(28.dp))
}

@Composable
internal fun SectionLabel(text: String) {
    Text(
        text, color = TextPrimary, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold,
        modifier = Modifier.padding(bottom = 10.dp),
    )
}

/** 标准 DLNA 投屏图标（Cast 风格：双波纹 + 实心屏角）——投屏胶囊/状态卡共用 */
@Composable
internal fun CastGlyph(size: Dp, color: Color) {
    Canvas(Modifier.size(size)) {
        val s = this.size.width
        drawArc(
            color = color,
            startAngle = 200f, sweepAngle = 100f, useCenter = false,
            topLeft = Offset(s * 0.08f, s * 0.30f),
            size = androidx.compose.ui.geometry.Size(s * 0.84f, s * 0.84f),
            style = Stroke(width = s * 0.09f, cap = StrokeCap.Round),
        )
        drawArc(
            color = color,
            startAngle = 200f, sweepAngle = 100f, useCenter = false,
            topLeft = Offset(s * 0.24f, s * 0.46f),
            size = androidx.compose.ui.geometry.Size(s * 0.52f, s * 0.52f),
            style = Stroke(width = s * 0.09f, cap = StrokeCap.Round),
        )
        drawRoundRect(
            color = color,
            topLeft = Offset(s * 0.42f, s * 0.66f),
            size = androidx.compose.ui.geometry.Size(s * 0.18f, s * 0.24f),
            cornerRadius = CornerRadius(s * 0.05f),
        )
    }
}
