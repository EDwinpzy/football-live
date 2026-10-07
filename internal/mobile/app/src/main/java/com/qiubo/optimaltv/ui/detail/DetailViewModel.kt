package com.qiubo.optimaltv.ui.detail

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry
import com.qiubo.optimaltv.data.db.ProgressEntity
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.model.SourceAvailability
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

data class DetailUiState(
    val item: VodItem? = null,
    val lines: List<VodItem> = emptyList(),   // 同名影片的跨源线路
    val activeLineId: String = "",
    val resumeEpIndex: Int = -1,
    val resumePositionMs: Long = 0,
    val sourceAvailability: SourceAvailability = SourceAvailability.MATCHING,
    /** 实际生效的取源层级（L0_last_good / L1_cloud_resolver / L2_local_detail / none）。 */
    val resolveTier: String = "none",
)

class DetailViewModel(private val vodId: String) : ViewModel() {

    private val _ui = MutableStateFlow(DetailUiState())
    val ui: StateFlow<DetailUiState> = _ui.asStateFlow()

    val isFavorite: StateFlow<Boolean> = Graph.db.vodDao().isFavoriteFlow(vodId)
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), false)

    /** 豆瓣刮削（原版：海报/评分/简介增强，字段空不替换） */
    private val _douban = MutableStateFlow<com.qiubo.optimaltv.data.repo.DoubanInfo?>(null)
    val douban: StateFlow<com.qiubo.optimaltv.data.repo.DoubanInfo?> = _douban.asStateFlow()

    init {
        // v1.19 流畅度：collect 挪 IO 派发——处理体里有全目录 flatten 匹配 + Room 往返，
        // 旧版跑在 viewModelScope 默认 Main 上
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            Graph.repo.state.collect { st ->
                // 目录外条目（实时搜索结果）兜底：hhkan: 前缀构造裸 item 走懒解析
                val item = st.catalog?.groups?.values?.flatten()?.firstOrNull { it.id == vodId }
                    ?: when {
                        vodId.startsWith("hhkan:") -> com.qiubo.optimaltv.data.model.VodItem(
                            id = vodId, sourceId = "hhkan", title = "", categoryId = "", detailRef = vodId,
                        )
                        vodId.startsWith("douban:") -> com.qiubo.optimaltv.data.model.VodItem(
                            id = vodId, sourceId = "douban", title = "", categoryId = "",
                            detailRef = vodId.removePrefix("douban:"),
                        )
                        else -> return@collect
                    }
                if (_ui.value.item == item) return@collect
                val lines = st.catalog?.groups?.get(item.normTitle).orEmpty()
                /* 2026-09-17 详情提速：目录条目自带标题/海报/评分/简介，先把这一版上屏
                   （主按钮落「正在查找片源」），再等下面的 resolveDetail 完成多源匹配。
                   旧版要等整段匹配跑完才第一次赋值 → 详情页只有一个「加载中…」，
                   冷路径实测 12-30s（多源搜索 + 18 条线路测速）。 */
                _ui.value = DetailUiState(
                    item = item,
                    lines = lines,
                    activeLineId = lines.firstOrNull()?.id ?: vodId,
                    sourceAvailability = SourceAvailability.MATCHING,
                )
                // 懒解析源（hhkan）：补全选集/简介后重算跨源线路
                /* 2026-09-28 取源提速：详情元数据与片源解析【并行】发起。
                   旧版串行 = 详情往返 + 线路解析往返，首帧片源要连等两趟网络。
                   两趟都只依赖 item 本身、互不依赖，故可并发；
                   合并口径不变（先上屏元数据、再补片源），UI 时序无感。 */
                val session = PlaybackTelemetry.Session(item.id)
                session.markDetailStart()
                // 并行预取第 1 集线路：既暖连接/后端热缓存，也在「从第 1 集起播」这个
                // 最常见路径上省掉一趟网络。若续播集号不是 1，下面会按真实集号重取。
                val prefetched = coroutineScope {
                    val detailJob = async {
                        if (item.detailRef.isNotBlank()) Graph.repo.resolveDetail(vodId) else null
                    }
                    val linesJob = async {
                        runCatching { Graph.repo.resolveLinesWithResolver(item, 0) }.getOrNull()
                    }
                    val resolvedDetail = detailJob.await()
                    val resolvedLines = linesJob.await()
                    session.markDetailDone()
                    (resolvedDetail ?: item) to resolvedLines
                }
                val full = prefetched.first
                val prefetchedLines = prefetched.second
                // 云端 Resolver 命中首选线路即记「拿到第一个片源」时刻。
                prefetchedLines?.resolve?.takeIf { it.usable }?.let { hit ->
                    session.emit(
                        PlaybackTelemetry.Phase.RESOLVE_FIRST_SOURCE,
                        sourceId = hit.preferred?.sourceId.orEmpty(),
                        contentId = hit.preferred?.contentId.orEmpty(),
                    )
                }
                val fullLines = if (full != item) {
                    st.catalog?.groups?.get(full.normTitle).orEmpty().ifEmpty { lines }
                } else lines
                // 续播（v1.19 性能+语义双修）：
                //  旧版①对每条线路每集【串行】getProgress——线路×集可达上千次 Room 往返，
                //      详情页「继续播放」状态秒级才出来；现每线路一次批量查询（progressOfVod）。
                //  旧版②按「集编号最大」取——昨天看到第 5 集、今天回看第 2 集会错跳第 5 集；
                //      现按 updatedAt 取【最近观看】的那集。
                var resumeEp = -1
                var resumePos = 0L
                var resumeAt = 0L
                /* 目录条目本身不带选集（豆瓣源 parseItems 不填 episodes），旧版只遍历
                   fullLines+lines → 每轮都 `line.episodes.isEmpty() → continue`，
                   续播判定永远落空、主按钮永远「立即播放」（2026-09-17 用户报障）。
                   把 resolveDetail 回来的 full（带选集）放进同一批扫描即可。 */
                for (line in (listOf(full) + fullLines + lines).distinctBy { it.id }) {
                    if (line.episodes.isEmpty()) continue
                    Graph.db.vodDao().progressOfVod(line.id).forEach { p ->
                        if (p.positionMs > 1000 && p.updatedAt > resumeAt && p.epIndex in line.episodes.indices) {
                            resumeEp = p.epIndex
                            resumePos = p.positionMs
                            resumeAt = p.updatedAt
                        }
                    }
                }
                _ui.value = DetailUiState(
                    item = full,
                    lines = fullLines,
                    activeLineId = fullLines.firstOrNull()?.id ?: vodId,
                    resumeEpIndex = resumeEp,
                    resumePositionMs = resumePos,
                    sourceAvailability = if (full.episodes.isEmpty()) SourceAvailability.UNAVAILABLE else SourceAvailability.AVAILABLE,
                    resolveTier = prefetchedLines?.tier ?: "none",
                )
                // 缓存版本先上屏后强制回源；新增集数只替换详情数据，不重置续播位置或焦点。
                val refreshed = if (item.detailRef.isNotBlank()) {
                    Graph.repo.resolveDetail(vodId, forceRefresh = true)
                } else null
                if (refreshed != null && refreshed != full) {
                    val refreshedLines = fullLines.map { line ->
                        if (line.id == refreshed.id) refreshed else line
                    }.ifEmpty { listOf(refreshed) }
                    _ui.value = _ui.value.copy(
                        item = refreshed,
                        lines = refreshedLines,
                        sourceAvailability = if (refreshed.episodes.isEmpty()) {
                            SourceAvailability.UNAVAILABLE
                        } else {
                            SourceAvailability.AVAILABLE
                        },
                    )
                }
                // 豆瓣刮削：标题非空且未拉过 → 拉一次（预探测语义：字段空不替换）
            }
        }
    }

    fun selectLine(vodIdOfLine: String) {
        _ui.value = _ui.value.copy(activeLineId = vodIdOfLine)
    }

    fun toggleFavorite() {
        val item = _ui.value.item ?: return
        viewModelScope.launch {
            if (isFavorite.value) {
                Graph.db.vodDao().removeFavorite(item.id)
            } else {
                Graph.db.vodDao().addFavorite(
                    com.qiubo.optimaltv.data.db.FavoriteEntity(
                        vodId = item.id,
                        title = item.title,
                        posterUrl = item.posterUrl,
                        createdAt = System.currentTimeMillis(),
                    ),
                )
            }
        }
    }

    /** 从头看：清掉该影片全部集进度 */
    fun clearProgress() {
        viewModelScope.launch {
            Graph.db.vodDao().clearProgressOfVod(vodId)
            _ui.value = _ui.value.copy(resumeEpIndex = -1, resumePositionMs = 0)
        }
    }
}
