package com.qiubo.optimaltv.ui.paywall

import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.navigation.NavController
import com.qiubo.optimaltv.license.ActivateResult
import com.qiubo.optimaltv.license.LicenseManager
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * v1.18 激活输码页（公测版）· 质感重构：
 * 氛围径向光晕 + 金属标徽章页头 + 分段码位格（当前位亮框+脉冲光标）+ 渐变键帽 + 白色胶囊 CTA。
 * 键位/激活逻辑/焦点自愈与 v1.17 完全一致（剔除 0/O/1/I、聚焦反白+放大、OK 掉焦 600ms 自愈）。
 */
@Composable
fun ActivateScreen(nav: NavController) {
    val s = rememberUiScale()
    var input by remember { mutableStateOf("") }
    var result by remember { mutableStateOf<String?>(null) }
    var resultOk by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    val scope = androidx.compose.runtime.rememberCoroutineScope()

    BackHandler { nav.popBackStack() }

    // 激活成功（总体#1 2026-09-03：自动进入足球界面）→ 停留展示结果 1s 后
    // popUpTo 起点页直达足球 tab（清返回栈，BACK 不会回输码/会员页）
    LaunchedEffect(resultOk) {
        if (resultOk) {
            delay(1000)
            com.qiubo.optimaltv.ui.components.navigateToTab(nav, "live")
        }
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        // 氛围光晕：码位区主光 + 右上品牌蓝晕（纯装饰，不参与焦点）
        Canvas(Modifier.fillMaxSize()) {
            val mainC = Offset(size.width * 0.5f, size.height * 0.34f)
            drawCircle(
                brush = Brush.radialGradient(
                    listOf(Color(0x10FFFFFF), Color.Transparent), mainC, size.width * 0.52f,
                ),
                radius = size.width * 0.52f, center = mainC,
            )
            val blueC = Offset(size.width * 0.9f, -size.height * 0.06f)
            drawCircle(
                brush = Brush.radialGradient(
                    listOf(Color(0x140A84FF), Color.Transparent), blueC, size.width * 0.36f,
                ),
                radius = size.width * 0.36f, center = blueC,
            )
        }

        // 2026-09-19 需求③（老板）：**页头保持左对齐**，页头以下整块水平居中。
        // 做法：外层 Column 左对齐（页头用），下半部分包一个 fillMaxWidth +
        // CenterHorizontally 的子 Column —— 居中参照系是整屏宽，不是页头宽度。
        Column(
            horizontalAlignment = Alignment.Start,
            modifier = Modifier.fillMaxSize().padding(start = 96f.sx(s), end = 96f.sx(s), top = 130f.sx(s)),
        ) {
            // ---- 页头：金属标徽章 + 标题（需求③：唯一保持左对齐的区域）----
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier,
            ) {
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .size(112f.sx(s))
                        .background(Color(0x14FFFFFF), RoundedCornerShape(30f.sx(s)))
                        .border(1.2f.sx(s), Color(0x1FFFFFFF), RoundedCornerShape(30f.sx(s))),
                ) {
                    Image(
                        painter = painterResource(com.qiubo.optimaltv.R.drawable.my_tv_logo),
                        contentDescription = null,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier.size(84f.sx(s)),
                    )
                }
                Spacer(Modifier.width(30f.sx(s)))
                Column {
                    Text(
                        "激活卡密", color = OtvColors.White, fontSize = 46f.sxs(s), fontWeight = FontWeight.Bold,
                    )
                    Spacer(Modifier.height(10f.sx(s)))
                    Text(
                        "购买后获得的 10 位卡密（OTV-XXXXX-XXXXX）",
                        color = OtvColors.White50, fontSize = 24f.sxs(s),
                    )
                }
            }

            Spacer(Modifier.height(52f.sx(s)))

            // ================= 以下整块水平居中（需求③） =================
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier.fillMaxWidth(),
            ) {
                // ---- 分段码位：OTV- 前缀 + 5+5，中横杠；已填大字 / 当前位亮框+脉冲光标 / 空位暗点 ----
                // v1.20（用户需求 激活#7）：码位前默认显示固定「OTV-」前缀——用户只输
                // 后 10 位，前缀由界面明示，不再靠副标题提示
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier,
                ) {
                    Text(
                        "OTV-",
                        color = OtvColors.White50, fontSize = 46f.sxs(s), fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.padding(end = 18f.sx(s)),
                    )
                    repeat(5) { i ->
                        CodeSlot(ch = input.getOrNull(i), active = input.length == i, s = s)
                        if (i < 4) Spacer(Modifier.width(10f.sx(s)))
                    }
                    Spacer(Modifier.width(16f.sx(s)))
                    Box(
                        Modifier
                            .width(26f.sx(s))
                            .height(6f.sx(s))
                            .alpha(0.8f)
                            .background(Color(0x38FFFFFF), RoundedCornerShape(3f.sx(s))),
                    )
                    Spacer(Modifier.width(16f.sx(s)))
                    repeat(5) { i ->
                        CodeSlot(ch = input.getOrNull(5 + i), active = input.length == 5 + i, s = s)
                        if (i < 4) Spacer(Modifier.width(10f.sx(s)))
                    }
                }

                Spacer(Modifier.height(50f.sx(s)))

                // ---- Apple TV 风格键盘（渐变键帽）：
                //      首行 = 「清空」白胶囊 + 小写字母 + ⌫；次行 = 数字（左右居中）----
                val firstKey = remember { FocusRequester() }
                InitialFocusEffect(firstKey, "activate-first", enabled = true)

                // 焦点自愈（搜索页同款）：OK 输入引发键节点重组会掉焦点，
                // 600ms 窗口内自动恢复到原键，连续打字焦点不中断
                var keyFocusLostAt by remember { mutableStateOf(0L) }
                var lastLostKeyFr by remember { mutableStateOf<FocusRequester?>(null) }
                val noteKeyLost: (FocusRequester?) -> Unit = { fr ->
                    keyFocusLostAt = System.currentTimeMillis(); lastLostKeyFr = fr
                }
                val noteKeyGained = { keyFocusLostAt = 0L; lastLostKeyFr = null }
                LaunchedEffect(input) {
                    if (input.isEmpty()) return@LaunchedEffect
                    kotlinx.coroutines.delay(150)
                    if (keyFocusLostAt > 0 && System.currentTimeMillis() - keyFocusLostAt < 600) {
                        val fr = lastLostKeyFr
                        keyFocusLostAt = 0; lastLostKeyFr = null
                        runCatching { (fr ?: firstKey).requestFocus() }
                    }
                }

                // horizontalAlignment：数字行 fillMaxWidth 会把本容器撑满全宽，
                // 字母行必须显式居中（否则默认 Start 贴左）
                Column(
                    verticalArrangement = Arrangement.spacedBy(22f.sx(s)),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(11f.sx(s)),
                        modifier = Modifier,
                    ) {
                        val clearFr = remember { FocusRequester() }
                        AcKey("清空", s, pill = true, focus = clearFr,
                            onFocusChange = { if (it) noteKeyGained() else noteKeyLost(clearFr) },
                        ) { input = "" }
                        CODE_LETTERS.forEachIndexed { i, k ->
                            // 键帽小写（Apple TV 风格），注入大写（卡密为大写字母表）
                            val keyFr = remember { FocusRequester() }
                            val handle = if (i == 0) firstKey else keyFr
                            AcKey(
                                k.lowercaseChar().toString(), s,
                                focus = handle,
                                onFocusChange = { if (it) noteKeyGained() else noteKeyLost(handle) },
                            ) { if (input.length < 10) input += k }
                        }
                        val bsFr = remember { FocusRequester() }
                        AcKey("⌫", s, boxed = true, focus = bsFr,
                            onFocusChange = { if (it) noteKeyGained() else noteKeyLost(bsFr) },
                        ) { if (input.isNotEmpty()) input = input.dropLast(1) }
                    }
                    // 数字行：v1.20 需求#7 补「1」（1 与 I/l 在大写字体可辨），仅剔 0；左右居中
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(11f.sx(s), Alignment.CenterHorizontally),
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        CODE_DIGITS.forEach { k ->
                            val keyFr = remember { FocusRequester() }
                            AcKey(k.toString(), s, focus = keyFr,
                                onFocusChange = { if (it) noteKeyGained() else noteKeyLost(keyFr) },
                            ) { if (input.length < 10) input += k }
                        }
                    }
                }

                // ---- 激活按钮 + 结果 ----
                Row(
                    horizontalArrangement = Arrangement.spacedBy(18f.sx(s)),
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.padding(top = 30f.sx(s)),
                ) {
                    val ready = input.length == 10 && !busy
                    val actFr = remember { FocusRequester() }
                    AcKey("激活卡密", s, wide = true, enabled = ready, focus = actFr,
                        onFocusChange = { if (it) noteKeyGained() else noteKeyLost(actFr) },
                    ) {
                        if (ready) {
                            busy = true
                            result = null
                            scope.launch {
                                when (val r = LicenseManager.activate(input)) {
                                    is ActivateResult.Ok -> {
                                        resultOk = true
                                        result = if (LicenseManager.isLifetime()) "激活成功 · 终身会员"
                                        else "激活成功 · 剩余 ${LicenseManager.daysLeft()} 天"
                                    }
                                    is ActivateResult.Error -> result = r.msg
                                }
                                busy = false
                            }
                        }
                    }
                    result?.let {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(
                                Modifier
                                    .size(12f.sx(s))
                                    .background(
                                        if (resultOk) Color(0xFF30D158) else Color(0xFFFF453A),
                                        CircleShape,
                                    ),
                            )
                            Spacer(Modifier.width(12f.sx(s)))
                            Text(
                                it,
                                color = if (resultOk) Color(0xFF30D158) else Color(0xFFFF453A),
                                fontSize = 28f.sxs(s),
                                fontWeight = FontWeight.Medium,
                            )
                        }
                    }
                }

                Spacer(Modifier.height(20f.sx(s)))
                Text(
                    "激活后立即生效 · 一码可绑 2 台设备",
                    color = OtvColors.White30,
                    fontSize = 22f.sxs(s),
                )
            }
        }
    }
}

/** 单个码位格：已填大字 / 当前位亮框（外圈微光）+脉冲光标 / 空位暗点 */
@Composable
private fun CodeSlot(ch: Char?, active: Boolean, s: Float) {
    val shape = RoundedCornerShape(22f.sx(s))
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .width(104f.sx(s))
            .height(134f.sx(s))
            .then(if (active) Modifier.border(5f.sx(s), Color(0x12FFFFFF), shape) else Modifier)
            .border(
                if (active) 1.5f.sx(s) else 1f.sx(s),
                if (active) Color(0x66FFFFFF) else Color(0x1AFFFFFF),
                shape,
            )
            .background(
                Brush.verticalGradient(listOf(Color(0x20FFFFFF), Color(0x12FFFFFF))),
                shape,
            ),
    ) {
        when {
            ch != null -> Text(
                ch.toString(),
                color = OtvColors.White,
                fontSize = 46f.sxs(s),
                fontWeight = FontWeight.SemiBold,
            )
            active -> ActiveCodeCaret(s)
            else -> Box(Modifier.size(9f.sx(s)).background(Color(0x2EFFFFFF), CircleShape))
        }
    }
}

@Composable
private fun ActiveCodeCaret(s: Float) {
    val pulse = rememberInfiniteTransition(label = "caret").animateFloat(
        initialValue = 0.35f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(650), RepeatMode.Reverse),
        label = "caretA",
    )
    Box(
        Modifier.width(3.5f.sx(s)).height(46f.sx(s))
            .graphicsLayer { alpha = pulse.value }
            .background(Color.White, RoundedCornerShape(2f.sx(s))),
    )
}

/** 卡密字母表键位（剔除 0/O/I：码里不会出现，键位更少更好按；v1.20 需求#7 补「1」） */
private val CODE_LETTERS: List<Char> = "ABCDEFGHJKMNPQRSTUVWXYZ".toList()
private val CODE_DIGITS: List<Char> = "123456789".toList()

/**
 * Apple TV 风格键帽（v1.18 渐变键帽底 + hairline 描边）：
 * 聚焦白底反色 + 放大（focusedBg 画在键帽下层，半透明键帽叠白底仍呈白）。
 * pill=「清空」常白胶囊；boxed=宽键帽（⌫）；wide=CTA 主按钮（启用时白底黑字）。
 */
@Composable
private fun AcKey(
    label: String,
    s: Float,
    focus: FocusRequester? = null,
    boxed: Boolean = false,
    pill: Boolean = false,
    wide: Boolean = false,
    enabled: Boolean = true,
    onFocusChange: ((Boolean) -> Unit)? = null,
    onTap: () -> Unit,
) {
    var f by remember { mutableStateOf(false) }
    val shape = RoundedCornerShape(
        when {
            wide -> 38f.sx(s)   // 76 高的一半：真胶囊
            pill -> 31f.sx(s)   // 62 高的一半：真胶囊
            else -> 14f.sx(s)
        },
    )
    val keycap = Brush.verticalGradient(listOf(Color(0x24FFFFFF), Color(0x12FFFFFF)))
    val bg: Brush = when {
        pill -> Brush.verticalGradient(listOf(Color.White, Color.White))
        wide && enabled -> Brush.verticalGradient(listOf(Color(0xE6FFFFFF), Color(0xE6FFFFFF)))
        wide -> Brush.verticalGradient(listOf(Color(0x16FFFFFF), Color(0x16FFFFFF)))
        else -> keycap
    }
    // 键帽 hairline；白底（胶囊/CTA 启用）不描边
    val stroke = when {
        pill -> null
        wide && enabled -> null
        else -> Color(0x1FFFFFFF)
    }
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .dpadFocusable(
                scaleFocused = when {
                    pill -> 1.12f
                    wide -> 1.05f
                    else -> 1.1f
                },
                // 常白胶囊/CTA 自带白底（聚焦只放大）；其余键聚焦反白
                focusedBg = if (pill) null else (if (enabled) OtvColors.White else Color(0x66FFFFFF)),
                focusedBgRadius = when {
                    wide -> 38f.sx(s)
                    pill -> 31f.sx(s)
                    else -> 14f.sx(s)
                },
                externalFocusRequester = focus,
                onFocusedChange = { f = it; onFocusChange?.invoke(it) },
            ) { if (enabled) onTap() }
            .background(bg, shape)
            .then(if (stroke != null) Modifier.border(1f.sx(s), stroke, shape) else Modifier)
            .width(
                when {
                    wide -> 300f.sx(s)
                    pill -> 96f.sx(s)
                    boxed -> 56f.sx(s)
                    else -> 46f.sx(s)
                },
            )
            .height(if (wide) 76f.sx(s) else 62f.sx(s)),
    ) {
        Text(
            label,
            color = when {
                pill -> OtvColors.Bg
                f && enabled -> OtvColors.Bg
                f -> Color(0x660E0E0F)
                wide && enabled -> OtvColors.Bg
                wide -> OtvColors.White30
                boxed -> OtvColors.White
                enabled -> OtvColors.White70
                else -> OtvColors.White30
            },
            fontSize = if (wide || boxed || pill) 30f.sxs(s) else 36f.sxs(s),
            fontWeight = if (wide || boxed || pill) FontWeight.SemiBold else FontWeight.Medium,
        )
    }
}
