package com.qiubo.optimaltv.ui.detail

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.data.model.formatTime
import com.qiubo.optimaltv.data.model.SourceAvailability
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.components.DetailActionButton
import com.qiubo.optimaltv.ui.components.MobileUiTags
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import java.net.URLEncoder

/**
 * 详情页（v1.18 与 TV 版 1:1 同款，一屏式，1920×1080 设计坐标系）：
 * 上部 = 海报(320×485) + 信息列（片名46 Bold / meta+评分 / 主演 / 简介3行 / 续播提示 / 按钮行 /
 * 选集区（仅真实分集内容；电影的国语/中字等版本交给播放器自动选源）/
 * >20 集分页 tab 每页 20 集 6 列）。详情页不暴露信号源选择。
 * 整体在 weight(1f) 盒内垂直居中（需求 #13）；内容总高按 ≤1080 设计，不滚动不出屏。
 * 触控交互：按钮/真实选集格点按。
 */
@Composable
fun DetailScreen(nav: NavController, vodIdArg: String) {
    val vodId = java.net.URLDecoder.decode(vodIdArg, "UTF-8")
    val vm: DetailViewModel = viewModel(
        key = "detail:$vodId",
        factory = simpleFactory { DetailViewModel(vodId) },
    )
    val ui by vm.ui.collectAsStateWithLifecycle()
    val fav by vm.isFavorite.collectAsStateWithLifecycle()
    val douban by vm.douban.collectAsStateWithLifecycle()
    val s = rememberUiScale()

    val item = ui.item
    if (item == null) {
        Box(Modifier.fillMaxSize().background(OtvColors.Bg), contentAlignment = Alignment.Center) {
            Text("加载中…", color = OtvColors.White50, fontSize = 28f.sxs(s))
        }
        return
    }

    // 竖屏（2026-09-07 竖屏优化）：海报+信息纵向堆叠（海报 240）、整体可滚动、
    // 侧距 80→40、按钮行均分——与网页版 portrait 分支同规格；横屏保持一屏居中
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        Column(
            Modifier
                .fillMaxSize()
                .then(if (portrait) Modifier.verticalScroll(rememberScrollState()) else Modifier)
                .padding(horizontal = (if (portrait) 40f else 80f).sx(s)),
        ) {
            // 需求 #13：横屏详情页内容上下居中（一屏内）；竖屏顶部起排随内容滚动
            Box(
                if (portrait) Modifier.fillMaxWidth().padding(top = 60f.sx(s), bottom = 40f.sx(s)) else Modifier.weight(1f),
                contentAlignment = if (portrait) Alignment.TopStart else Alignment.Center,
            ) {
                Column {
                    /** 海报（横屏 320 / 竖屏 240，5:7 比例不变） */
                    @Composable fun posterBox(w: Float) = Box(
                        Modifier
                            .width(w.sx(s))
                            .aspectRatio(232f / 352f)
                            .clip(RoundedCornerShape(16f.sx(s))),
                    ) {
                        PosterPlaceholder(seed = item.id.hashCode(), Modifier.matchParentSize())
                        if (item.posterUrl.isNotBlank()) {
                            AsyncImage(
                                model = item.posterUrl,
                                contentDescription = item.title,
                                contentScale = ContentScale.Crop,
                                modifier = Modifier.matchParentSize(),
                            )
                        }
                        // 豆瓣海报叠层（原版预探测替换：加载失败透明露出下层 hhkan 图）
                        douban?.poster?.takeIf { it.isNotBlank() }?.let { dp ->
                            AsyncImage(
                                model = dp,
                                contentDescription = null,
                                contentScale = ContentScale.Crop,
                                modifier = Modifier.matchParentSize(),
                            )
                        }
                        Box(
                            Modifier
                                .matchParentSize()
                                .background(Brush.verticalGradient(listOf(Color.Transparent, Color(0x99000000)))),
                        )
                    }

                    /** 信息列（片名/meta/简介/按钮/选集；横屏在海报右、竖屏在海报下） */
                    @Composable fun infoCol(m: Modifier) = Column(m) {
                        // 片名（醒目，两行封顶）
                        Text(
                            item.title.ifBlank { "未命名影片" },
                            color = OtvColors.White, fontSize = 46f.sxs(s), fontWeight = FontWeight.Bold,
                            lineHeight = 56f.sxs(s), maxLines = 2, overflow = TextOverflow.Ellipsis,
                        )
                        Spacer(Modifier.height(12f.sx(s)))
                        // meta 行：2026 / 美国 / 爱情片 + 评分
                        val metaLine = item.meta.ifBlank {
                            (listOf(item.year, item.area).filter { it.isNotBlank() } + item.tags).joinToString(" / ")
                        }
                        val dr = douban?.rating?.toDoubleOrNull()
                        val ratingLine = when {
                            dr != null && dr > 0 -> "豆瓣 %.1f".format(dr)
                            item.rating > 0 -> "评分 %.1f".format(item.rating)
                            else -> ""
                        }
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            if (metaLine.isNotBlank()) {
                                Text(
                                    metaLine, color = OtvColors.White70, fontSize = 27f.sxs(s),
                                    maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false),
                                )
                            }
                            if (ratingLine.isNotBlank()) {
                                Spacer(Modifier.width(18f.sx(s)))
                                Text("★ $ratingLine", color = Color(0xFFFFD60A), fontSize = 27f.sxs(s), fontWeight = FontWeight.SemiBold)
                            }
                        }
                        if (item.actors.isNotBlank()) {
                            Spacer(Modifier.height(10f.sx(s)))
                            Text(
                                "主演  " + item.actors,
                                color = OtvColors.White55, fontSize = 24f.sxs(s),
                                maxLines = 1, overflow = TextOverflow.Ellipsis,
                            )
                        }
                        Spacer(Modifier.height(12f.sx(s)))
                        Text(
                            (douban?.desc?.takeIf { it.isNotBlank() } ?: item.desc).ifBlank { "暂无简介" },
                            color = OtvColors.White70, fontSize = 26f.sxs(s),
                            lineHeight = 37f.sxs(s), maxLines = 3, overflow = TextOverflow.Ellipsis,
                        )
                        Spacer(Modifier.height(20f.sx(s)))
                        // 续播提示
                        if (ui.resumeEpIndex >= 0) {
                            Text(
                                "上次看到第${ui.resumeEpIndex + 1}集 ${formatTime(ui.resumePositionMs)}",
                                color = Color(0xFFFFD60A), fontSize = 24f.sxs(s),
                            )
                            Spacer(Modifier.height(14f.sx(s)))
                        }
                        // 主按钮行（原版 Go To Show 规格：min 280 / h64 / r9.7 / 29.3 Bold）
                        Row(horizontalArrangement = Arrangement.spacedBy(18f.sx(s))) {
                            val playLabel = when (ui.sourceAvailability) {
                                SourceAvailability.MATCHING -> "正在查找片源"
                                SourceAvailability.UNAVAILABLE -> "暂无片源"
                                SourceAvailability.AVAILABLE -> if (ui.resumeEpIndex >= 0) "继续播放 第${ui.resumeEpIndex + 1}集" else "立即播放"
                            }
                            DetailActionButton(
                                text = playLabel,
                                primary = true,
                                fill = portrait,
                                s = s,
                                testTag = MobileUiTags.DetailPlay,
                                enabled = ui.sourceAvailability == SourceAvailability.AVAILABLE,
                            ) {
                                val ep = if (ui.resumeEpIndex >= 0) ui.resumeEpIndex else 0
                                // 详情页固定传入好好看主条目；补充源由播放器内部自动路由。
                                val resumeArg = if (ui.resumeEpIndex >= 0) "?resumeMs=${ui.resumePositionMs}" else ""
                                nav.navigate("player/" + URLEncoder.encode(vodId, "UTF-8") + "/$ep" + resumeArg)
                            }
                            DetailActionButton(
                                text = if (fav) "★ 已收藏" else "☆ 收藏",
                                primary = false,
                                fill = portrait,
                                s = s,
                                testTag = MobileUiTags.DetailFavorite,
                            ) { vm.toggleFavorite() }
                            // 2026-09-19 用户需求：去掉「从头看」按钮（清除进度改由播放器长按/面板承担，逻辑保留）
                        }

                        // 选集区（需求⑧：紧贴按钮行下方；电影单集不渲染；
                        // >20 集分页 tab（每页 20 集 6 列 ≤4 行），页签超宽横向滚动）
                        if (shouldShowEpisodePicker(item)) {
                            Spacer(Modifier.height(24f.sx(s)))
                            if (item.episodes.size > 20) {
                                val pageCount = (item.episodes.size + 19) / 20
                                var page by remember(item.id) {
                                    mutableIntStateOf(
                                        if (ui.resumeEpIndex >= 0) ui.resumeEpIndex / 20 else 0,
                                    )
                                }
                                LazyRow(
                                    horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                                    modifier = Modifier.padding(bottom = 14f.sx(s)),
                                ) {
                                    items(pageCount) { p ->
                                        val from = p * 20 + 1
                                        val to = minOf((p + 1) * 20, item.episodes.size)
                                        EpisodeCellFixed(label = "$from-$to", highlight = p == page, width = 120f.sx(s), s = s) { page = p }
                                    }
                                }
                                val pageEps = item.episodes.drop(page * 20).take(20)
                                EpisodeGrid(pageEps, ui.resumeEpIndex, s) { ep ->
                                    nav.navigate(
                                        "player/" + URLEncoder.encode(vodId, "UTF-8") +
                                            "/${ep.index}",
                                    )
                                }
                            } else {
                                EpisodeGrid(item.episodes, ui.resumeEpIndex, s) { ep ->
                                    nav.navigate(
                                        "player/" + URLEncoder.encode(vodId, "UTF-8") +
                                            "/${ep.index}",
                                    )
                                }
                            }
                        }
                    }

                    // 横屏 = 海报左信息右（320+48）；竖屏 = 海报上信息下（240+24）
                    if (portrait) {
                        Column {
                            posterBox(240f)
                            Spacer(Modifier.height(24f.sx(s)))
                            infoCol(Modifier.fillMaxWidth())
                        }
                    } else {
                        Row {
                            posterBox(320f)
                            Spacer(Modifier.width(48f.sx(s)))
                            infoCol(Modifier.weight(1f).padding(top = 8f.sx(s)))
                        }
                    }

                }
            }
        }
        // 移动① 修复：返回钮最后声明 = 绘制在最上层；放最外层 Box——竖屏可滚动结构
        // 下不随内容滚走（与网页版 fixed 返回钮一致）
        MobileBackButton(Modifier.align(Alignment.TopStart)) { nav.popBackStack() }
    }
}

/** 电影的 HD中字/HD国语/正片是播放版本，不是可交互的选集。 */
internal fun shouldShowEpisodePicker(item: com.qiubo.optimaltv.data.model.VodItem): Boolean {
    if (item.episodes.size <= 1) return false
    return when (item.categoryId.substringAfterLast(':')) {
        "1" -> false
        "2", "3", "4", "6" -> true
        else -> {
            val seriesMeta = Regex("电视剧|连续剧|网剧|短剧|动漫|综艺|美剧|英剧|韩剧|日剧|泰剧")
            val episodicName = Regex("第?\\d+[\\s]*[集期话章]|更新至|[上中下]集")
            seriesMeta.containsMatchIn(item.meta) || item.episodes.size > 4 ||
                item.episodes.any { episodicName.containsMatchIn(it.name) }
        }
    }
}

/** 选集网格：每行 6 格等宽（行 gap14 格 gap20）；触控点格进播放。
 *  v1.19 竖屏自适应（总体#2）：列数按屏宽算（横屏公式自然=6）。 */
@Composable
private fun EpisodeGrid(
    eps: List<com.qiubo.optimaltv.data.model.Episode>,
    resumeEpIndex: Int,
    s: Float,
    onPick: (com.qiubo.optimaltv.data.model.Episode) -> Unit,
) {
    val cols = com.qiubo.optimaltv.ui.theme.rememberRowColumns(cardW = 277f, gap = 20f, sidePad = 80f)
    Column(verticalArrangement = Arrangement.spacedBy(14f.sx(s))) {
        eps.chunked(cols).forEach { rowEps ->
            Row(
                horizontalArrangement = Arrangement.spacedBy(20f.sx(s)),
            ) {
                rowEps.forEach { ep ->
                    EpisodeCell(
                        label = ep.name,
                        highlight = ep.index == resumeEpIndex,
                        width = null,
                        s = s,
                    ) { onPick(ep) }
                }
                // 补齐空位保持格子等宽
                repeat(cols - rowEps.size) { Spacer(Modifier.weight(1f)) }
            }
        }
    }
}

/** 分页签（固定宽度；非 RowScope 版本，供 LazyRow 页签行使用，需求 影视#4） */
@Composable
private fun EpisodeCellFixed(
    label: String,
    highlight: Boolean,
    width: androidx.compose.ui.unit.Dp,
    s: Float,
    onClick: () -> Unit,
) {
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .width(width)
            .clip(RoundedCornerShape(12f.sx(s)))
            .tapCard(onClick)
            .background(
                if (highlight) AccentBlue.copy(alpha = 0.55f) else Color(0x24FFFFFF),
                RoundedCornerShape(12f.sx(s)),
            )
            .padding(vertical = 17f.sx(s)),
    ) {
        Text(label, color = OtvColors.White, fontSize = 25f.sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
private fun androidx.compose.foundation.layout.RowScope.EpisodeCell(
    label: String,
    highlight: Boolean,
    width: androidx.compose.ui.unit.Dp?,
    s: Float,
    onClick: () -> Unit,
) {
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .then(if (width != null) Modifier.width(width) else Modifier.weight(1f))
            .clip(RoundedCornerShape(12f.sx(s)))
            .tapCard(onClick)
            .background(
                if (highlight) AccentBlue.copy(alpha = 0.55f) else Color(0x24FFFFFF),
                RoundedCornerShape(12f.sx(s)),
            )
            .padding(vertical = 17f.sx(s)),
    ) {
        Text(label, color = OtvColors.White, fontSize = 25f.sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

/** 简易 VM 工厂 */
fun <T : ViewModel> simpleFactory(creator: () -> T): androidx.lifecycle.ViewModelProvider.Factory =
    object : androidx.lifecycle.ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <VM : ViewModel> create(modelClass: Class<VM>): VM = creator() as VM
    }
