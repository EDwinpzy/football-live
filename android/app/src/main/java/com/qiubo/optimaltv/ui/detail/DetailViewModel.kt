package com.qiubo.optimaltv.ui.detail

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.db.ProgressEntity
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.model.SourceAvailability
import com.qiubo.optimaltv.data.repo.DoubanInfo
import kotlinx.coroutines.async
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
)

/**
 * 续播扫描用的候选线路集合。
 *
 * 目录条目本身不带选集（豆瓣源 parseItems 不填 episodes），只扫目录 lines 会让续播判定
 * 永远落空、主按钮永远显示「立即播放」（2026-09-17 用户报障）。所以要把
 * `resolveDetail` 回来的 full（带选集）并进同一批扫描。
 */
private fun fullLinesOf(catalogLines: List<VodItem>, full: VodItem): List<VodItem> =
    if (full.detailRef.isBlank() && full.episodes.isEmpty()) catalogLines
    else (listOf(full) + catalogLines).distinctBy { it.id }

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
                    ?: if (vodId.startsWith("douban:")) com.qiubo.optimaltv.data.model.VodItem(
                        id = vodId, sourceId = "douban", title = "", categoryId = "",
                        detailRef = vodId.removePrefix("douban:"),
                    ) else return@collect
                if (_ui.value.item == item) return@collect
                val lines = st.catalog!!.groups[item.normTitle].orEmpty()
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
                /* 2026-09-28 详情与片源并行：旧版把「补全详情」和「续播扫描」串行塞在
                   一个 collect 里——resolveDetail 要等后端把 22 个站点的搜索+探测跑完
                   （冷路径实测 12~30s），续播扫描只能在这之后才开始，白等一整轮。
                   现在起播所需的三件事并发跑：详情补全、续播扫描、豆瓣简介刮削。
                   谁先回来谁先上屏，晚到的只补空字段不覆盖已有值。 */
                val briefJob = if (item.detailRef.isNotBlank() || item.desc.isBlank()) {
                    async { runCatching { Graph.repo.vodBrief(vodId) }.getOrNull() }
                } else {
                    async { null }
                }
                // 懒解析源（hhkan）：补全选集/简介后重算跨源线路
                val full = if (item.detailRef.isNotBlank()) Graph.repo.resolveDetail(vodId) ?: item else item
                // 续播扫描与详情补全并发：只查「已经有选集」的线路（full 之后才知道哪些有），
                // 但目录里的 lines 也能查，不必等 full。
                val resumeJob = async {
                    runCatching {
                    var resumeEp = -1
                    var resumePos = 0L
                    var resumeAt = 0L
                    for (line in (listOf(full) + fullLinesOf(lines, full)).distinctBy { it.id }) {
                        if (line.episodes.isEmpty()) continue
                        Graph.db.vodDao().progressOfVod(line.id).forEach { p ->
                            if (p.positionMs > 1000 && p.updatedAt > resumeAt && p.epIndex in line.episodes.indices) {
                                resumeEp = p.epIndex
                                resumePos = p.positionMs
                                resumeAt = p.updatedAt
                            }
                        }
                    }
                    Triple(resumeEp, resumePos, resumeAt)
                    }.getOrDefault(Triple(-1, 0L, 0L))
                }
                val fullLines = if (full != item) {
                    st.catalog?.groups?.get(full.normTitle).orEmpty().ifEmpty { lines }
                } else lines
                // 续播（v1.19 性能+语义双修）：
                //  旧版①对每条线路每集【串行】getProgress——线路×集可达上千次 Room 往返，
                //      详情页「继续播放」状态秒级才出来；现每线路一次批量查询（progressOfVod）。
                //  旧版②按「集编号最大」取——昨天看到第 5 集、今天回看第 2 集会错跳第 5 集；
                //      现按 updatedAt 取【最近观看】的那集。
                val (resumeEp, resumePos, _) = resumeJob.await()
                // 简介刮削：只补空字段，brief 失败不影响详情/续播上屏。
                briefJob.await()?.let { (meta, desc, rating) ->
                    if (_douban.value?.found != true) {
                        _douban.value = DoubanInfo(
                            found = true,
                            rating = if (rating > 0) String.format("%.1f", rating) else "",
                            desc = desc,
                            poster = "",
                        )
                    }
                    if (_ui.value.item != null && _ui.value.item!!.desc.isBlank() && desc.isNotBlank()) {
                        _ui.value = _ui.value.copy(
                            item = _ui.value.item!!.copy(
                                desc = desc,
                                meta = _ui.value.item!!.meta.ifBlank { meta },
                                rating = if (_ui.value.item!!.rating > 0) _ui.value.item!!.rating else rating,
                            ),
                        )
                    }
                }
                _ui.value = DetailUiState(
                    item = full,
                    lines = fullLines,
                    activeLineId = fullLines.firstOrNull()?.id ?: vodId,
                    resumeEpIndex = resumeEp,
                    resumePositionMs = resumePos,
                    sourceAvailability = if (full.episodes.isEmpty()) SourceAvailability.UNAVAILABLE else SourceAvailability.AVAILABLE,
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
