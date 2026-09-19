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
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.navigation.NavController
import com.qiubo.optimaltv.license.ActivateResult
import com.qiubo.optimaltv.license.LicenseManager
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * v1.20 激活输码页（移动版）：
 * 氛围径向光晕 + 金属标徽章页头 + OTV- 固定前缀 + 分段码位格（5+5）。
 *
 * 2026-09-19 版式重构（老板需求①③）：
 *  - **竖屏专属分支**：屏内 Apple TV 键帽键盘**移除**（老板需求②：改用系统输入法），
 *    码位格缩小到 10 位一行放得下；页头标题区**保持左对齐**（需求③例外），
 *    页头以下（码位 / 提示 / 按钮 / 标语）整块**水平居中**。
 *  - **横屏分支**：与 TV 版 1:1 同款（s = 屏宽/1920），同样按需求③把标题区以下居中。
 *
 * 码位格整块即输入热区：点按呼出系统输入法（BasicTextField 隐藏承载，光标随输入
 * 实时落在当前码位）；输入过滤为卡密字母表并截断到 10 位。输满高亮「激活卡密」。
 */
@Composable
fun ActivateScreen(nav: NavController) {
    val s = rememberUiScale()
    var input by remember { mutableStateOf("") }
    var result by remember { mutableStateOf<String?>(null) }
    var resultOk by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    val fieldFocus = remember { FocusRequester() }
    val ime = LocalSoftwareKeyboardController.current

    val portrait = LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT

    // 2026-09-19 需求②：竖屏码位格必须收窄才放得下 10 位一行。
    // 竖屏 s = 屏宽dp/750，104 设计px 的格子在此尺度下有 ~57dp 宽，10 格 + 前缀
    // = 703dp 对 329dp 可用宽（2.1 倍溢出）。这里给竖屏单独一档「码位缩放系数」，
    // 与页面其它尺寸解耦。
    //
    // 2026-09-19 需求⑤（老板第二轮）：「OTV- 要和下面剩下的密钥放一行，不要分两行」。
    // 原先竖屏把「OTV-」降为码位**上方**的小字标签（独占一行），现改为与横屏同款、
    // 回到码位行**行内**（见下方码位 Row 里的前缀块）。等价于行宽新增
    // 「OTV- 字宽 + 前缀间距」≈ 62.7 设计px：
    //   · 旧行宽 650.0（10 格 56 + 组内 8×6 + 横杠两侧 2×10 + 横杠 18）；
    //   · 新需求 712.7 对可用宽 690.3 ⟹ **溢出 22.4 设计px**。
    // 因此竖屏档整体收一档（实测标定，非拍脑袋）：
    //   格宽 56 → 54、组内间距 6 → 4、前缀/横杠间距 10 → 8、横杠宽 18 → 16、
    //   OTV- 字号 20 → 26（太小读不清，26 是行内可辨下限）、前缀与首格间距 12 → 10。
    // 实测落地：整行 56..1025px = **672.9 设计px**，对 690.3 余 **17.4 设计px**（2.5%）。
    //   ⚠️ 余量偏紧：OTV- 实测占 55.6 设计px（比估值宽 20），换字体/换 ROM 有顶边风险。
    //   再收只能在**字号**上动（格宽 54 已接近码位可辨认下限），非必要不要再挤。
    val slotW = if (portrait) 54f else 104f
    val slotH = if (portrait) 72f else 134f
    val slotGap = if (portrait) 4f else 10f
    val slotFont = if (portrait) 34f else 46f
    // 行内「OTV-」字号：竖屏 26（与副标题 20 拉开，但不抢码位 34 的视觉），横屏保持 46。
    val prefixFont = if (portrait) 26f else 46f

    BackHandler { nav.popBackStack() }

    // 激活成功 → 停留展示结果后自动返回（会员页/门控页状态即时刷新）
    // 总体#1（2026-09-03）：激活成功自动进入足球界面（popUpTo 起点页直达）
    LaunchedEffect(resultOk) {
        if (resultOk) {
            kotlinx.coroutines.delay(1000)
            com.qiubo.optimaltv.ui.components.navigateToTab(nav, "live")
        }
    }

    // 移动#1：进页自动落焦隐藏输入框 → 系统输入法直接弹出，点码位可随时唤回
    // 2026-09-19 需求②修复：requestFocus 后系统键盘**不保证**弹出（WindowInsetsController
    // 在无物理键盘的设备上对 1dp×alpha0 的隐藏输入框会拒弹），这里补一次显式 show，
    // 再延迟复查——仍未聚焦就重试一次 requestFocus，形成「聚焦 + show」双保险。
    LaunchedEffect(Unit) {
        kotlinx.coroutines.delay(300)
        runCatching { fieldFocus.requestFocus() }
        kotlinx.coroutines.delay(120)
        runCatching { ime?.show() }
        // 部分 ROM 首帧 show 会被吞（IME 服务尚未 attach）：300ms 后补一发
        kotlinx.coroutines.delay(300)
        runCatching { ime?.show() }
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        // 氛围光晕：码位区主光 + 右上品牌蓝晕（纯装饰）
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

        // 2026-09-19 需求③：**页头保持左对齐**，页头以下整块水平居中。
        // 做法：外层 Column 左对齐（页头用），下半部分包一个 fillMaxWidth +
        // CenterHorizontally 的子 Column —— 居中参照系是整屏宽，不是页头宽度。
        // 竖屏套 verticalScroll，并用 imePadding 让系统输入法弹出时把内容顶上去而不是遮住。
        val padStart = if (portrait) 30f else 90f
        val padEnd = if (portrait) 30f else 60f
        val padTop = if (portrait) 96f else 130f

        Column(
            horizontalAlignment = Alignment.Start,
            // 竖屏：上下各留一份弹性余量（weight 1 的空 Spacer），把「页头 + 码位 + 按钮」
            // 整块压在竖向中段——键盘收起时不会全都挤在屏幕顶 1/3、下方留一大片死白。
            verticalArrangement = if (portrait) Arrangement.Center else Arrangement.Top,
            modifier = Modifier
                .fillMaxSize()
                .padding(start = padStart.sx(s), end = padEnd.sx(s), top = padTop.sx(s))
                .imePadding()
                .then(if (portrait) Modifier.verticalScroll(rememberScrollState()) else Modifier),
        ) {
            // ---- 页头：金属标徽章 + 标题（需求③：唯一保持左对齐的区域）----
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .size((if (portrait) 72f else 112f).sx(s))
                        .background(Color(0x14FFFFFF), RoundedCornerShape((if (portrait) 20f else 30f).sx(s)))
                        .border(1.2f.sx(s), Color(0x1FFFFFFF), RoundedCornerShape((if (portrait) 20f else 30f).sx(s))),
                ) {
                    Image(
                        painter = painterResource(com.qiubo.optimaltv.R.drawable.my_tv_logo),
                        contentDescription = null,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier.size((if (portrait) 54f else 84f).sx(s)),
                    )
                }
                Spacer(Modifier.width((if (portrait) 18f else 30f).sx(s)))
                Column {
                    Text(
                        "激活卡密", color = OtvColors.White,
                        fontSize = (if (portrait) 34f else 46f).sxs(s), fontWeight = FontWeight.Bold,
                    )
                    Spacer(Modifier.height((if (portrait) 6f else 10f).sx(s)))
                    Text(
                        if (portrait) "输入 10 位卡密"
                        else "输入购买获得的 10 位卡密（OTV-XXXXX-XXXXX）",
                        color = OtvColors.White50,
                        fontSize = (if (portrait) 20f else 24f).sxs(s),
                    )
                }
            }

            Spacer(Modifier.height((if (portrait) 30f else 52f).sx(s)))

            // ================= 以下整块水平居中（需求③） =================
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier.fillMaxWidth(),
            ) {
                // ---- 分段码位：OTV- 前缀 + 5+5，中横杠 ----
                // 2026-09-19 需求⑤：**竖屏也把「OTV-」放在行内**，与十位码位同一行
                // （老板原话「OTV- 要和下面剩下的密钥放一行，不要分两行」）。
                // 旧版竖屏把前缀降为码位上方小字标签独占一行，现已移除；
                // 前缀块在下面的码位 Row 里改为无条件渲染。
                BasicTextField(
                    value = input,
                    onValueChange = { raw ->
                        input = raw.uppercase().filter { it in CODE_ALPHABET }.take(10)
                    },
                    textStyle = androidx.compose.ui.text.TextStyle(color = Color.Transparent),
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Ascii,
                        capitalization = KeyboardCapitalization.Characters,
                        autoCorrect = false,
                        imeAction = ImeAction.Done,
                    ),
                    // 输满 10 位 = 收键盘 + 直接提交，省一次「点按钮」的抬手
                    keyboardActions = KeyboardActions(
                        onDone = {
                            ime?.hide()
                            if (input.length == 10 && !busy) {
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
                        },
                    ),
                    modifier = Modifier
                        .focusRequester(fieldFocus)
                        .width(1.dp)
                        .height(1.dp)
                        .alpha(0f),
                )

                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.tapCard { runCatching { fieldFocus.requestFocus(); ime?.show() } },
                ) {
                    // 2026-09-19 需求⑤：前缀改为**两端同款、无条件行内渲染**。
                    // 竖屏用收窄档（字号 26 / 尾距 10），横屏沿用旧档（46 / 18）——
                    // 改前这段被 `if (!portrait)` 包着，竖屏根本走不到。
                    Text(
                        "OTV-",
                        color = OtvColors.White50,
                        fontSize = prefixFont.sxs(s),
                        fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.padding(end = (if (portrait) 10f else 18f).sx(s)),
                    )
                    repeat(5) { i ->
                        CodeSlot(
                            ch = input.getOrNull(i), active = input.length == i, s = s,
                            w = slotW, h = slotH, font = slotFont,
                        )
                        if (i < 4) Spacer(Modifier.width(slotGap.sx(s)))
                    }
                    Spacer(Modifier.width((if (portrait) 8f else 16f).sx(s)))
                    Box(
                        Modifier
                            .width((if (portrait) 16f else 26f).sx(s))
                            .height((if (portrait) 4f else 6f).sx(s))
                            .alpha(0.8f)
                            .background(Color(0x38FFFFFF), RoundedCornerShape(3f.sx(s))),
                    )
                    Spacer(Modifier.width((if (portrait) 8f else 16f).sx(s)))
                    repeat(5) { i ->
                        CodeSlot(
                            ch = input.getOrNull(5 + i), active = input.length == 5 + i, s = s,
                            w = slotW, h = slotH, font = slotFont,
                        )
                        if (i < 4) Spacer(Modifier.width(slotGap.sx(s)))
                    }
                }

                Spacer(Modifier.height((if (portrait) 26f else 46f).sx(s)))

                // ---- 激活按钮 + 结果 ----
                Row(
                    horizontalArrangement = Arrangement.spacedBy(18f.sx(s)),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    val ready = input.length == 10 && !busy
                    AcKey("激活卡密", s, wide = true, enabled = ready,
                        w = if (portrait) 240f else 300f,
                    ) {
                        if (ready) {
                            ime?.hide()
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
                                maxLines = 1, overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                }

                Spacer(Modifier.height((if (portrait) 14f else 20f).sx(s)))
                Text(
                    if (portrait) "激活后立即生效 · 一码可绑 2 台设备"
                    else "点按上方码位输入 · 激活后立即生效 · 一码可绑 2 台设备",
                    color = OtvColors.White30,
                    fontSize = (if (portrait) 18f else 22f).sxs(s),
                )
            }
        }
        // 移动① 修复：内容之后声明 = 最上层可点
        MobileBackButton { nav.popBackStack() }
    }
}

/** 卡密输入字母表（与生成器一致：剔除 0/O/1/I） */
private val CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

/**
 * 单个码位格：已填大字 / 当前位亮框（外圈微光）+脉冲光标 / 空位暗点。
 * w/h/font 由调用方给（竖屏收窄档 / 横屏与 TV 版同款档），本组件只负责画。
 */
@Composable
private fun CodeSlot(ch: Char?, active: Boolean, s: Float, w: Float, h: Float, font: Float) {
    val shape = RoundedCornerShape((w * 0.21f).sx(s))
    val pulse = rememberInfiniteTransition(label = "caret").animateFloat(
        initialValue = 0.35f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(650), RepeatMode.Reverse),
        label = "caretA",
    )
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .width(w.sx(s))
            .height(h.sx(s))
            .then(if (active) Modifier.border((w * 0.048f).sx(s), Color(0x12FFFFFF), shape) else Modifier)
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
                fontSize = font.sxs(s),
                fontWeight = FontWeight.SemiBold,
            )
            active -> Box(
                Modifier
                    .width((w * 0.034f).sx(s))
                    .height((h * 0.34f).sx(s))
                    .background(
                        Color.White.copy(alpha = pulse.value),
                        RoundedCornerShape(2f.sx(s)),
                    ),
            )
            else -> Box(Modifier.size((w * 0.087f).sx(s)).background(Color(0x2EFFFFFF), CircleShape))
        }
    }
}

/** 宽胶囊 CTA（启用=白底黑字；禁用=暗底）；触控点按。w 由调用方给（竖屏收窄档）。 */
@Composable
private fun AcKey(
    label: String,
    s: Float,
    wide: Boolean = false,
    enabled: Boolean = true,
    w: Float = 300f,
    onTap: () -> Unit,
) {
    val h = if (wide) 76f else 62f
    val shape = RoundedCornerShape((h / 2).sx(s))   // 高的一半：真胶囊
    val bg: Brush = when {
        wide && enabled -> Brush.verticalGradient(listOf(Color(0xE6FFFFFF), Color(0xE6FFFFFF)))
        wide -> Brush.verticalGradient(listOf(Color(0x16FFFFFF), Color(0x16FFFFFF)))
        else -> Brush.verticalGradient(listOf(Color(0x24FFFFFF), Color(0x12FFFFFF)))
    }
    val stroke = if (wide && enabled) null else Color(0x1FFFFFFF)
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .tapCard { if (enabled) onTap() }
            .background(bg, shape)
            .then(if (stroke != null) Modifier.border(1f.sx(s), stroke, shape) else Modifier)
            .width(w.sx(s))
            .height(h.sx(s)),
    ) {
        Text(
            label,
            color = when {
                wide && enabled -> OtvColors.Bg
                wide -> OtvColors.White30
                else -> OtvColors.White70
            },
            fontSize = 30f.sxs(s),
            fontWeight = FontWeight.SemiBold,
        )
    }
}
