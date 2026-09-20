package com.qiubo.optimaltv.focus

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.graphics.Color
import android.view.KeyEvent
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.printToLog
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.LocalNavOverride
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.lazyNavContainer
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import kotlinx.coroutines.launch

/**
 * OtvNav 焦点引擎仪器测试（三层测试体系第二层，2026-09-06）。
 *
 * 方法论（compose-focus-navigation 纪律）：
 *  - 只发按键（instrumentation.sendKeyDownUpSync → 窗口分发 →
 *    Activity.dispatchKeyEvent → OtvNav.dispatch，与遥控器同链路），绝不用点击代替按键。
 *    注意：compose 的 performKeyInput 直注 Compose 层、绕过 Activity（实测引擎日志全无），
 *    测引擎必须用窗口级注入。
 *  - 断言语义（assertIsFocused），不断言颜色/缩放；
 *  - 场景几何取自真实页面（网格 3 列 / chips 40dp / 悬浮 tab 行 48dp）。
 * 覆盖：网格同列纪律、同行严格性（防斜跳）、悬浮层不作垂直候选、区域 override、
 * navGroup 封锁、scroll-reveal、UP 逃逸 topFocus、焦点销毁自愈、自愈排除 autoSelect。
 */
@kotlin.OptIn(androidx.compose.ui.test.ExperimentalTestApi::class)
class OtvNavFocusHarnessTest {

    @get:Rule
    val rule = createAndroidComposeRule<FocusHarnessActivity>()

    @Before
    fun setUp() {
        Harness.scene = null
        OtvNav.resetForTest()
    }

    private fun scene(content: @Composable () -> Unit) {
        Harness.scene = content
        android.util.Log.i("OTV-TEST", "test set scene, waiting idle")
        rule.waitForIdle()
        android.util.Log.i("OTV-TEST", "scene idle done")
    }

    private fun key(k: Int) {
        // 必须在 instrumentation（测试）线程调用：sendKeyDownUpSync 校验非主线程，
        // 注入走窗口级分发（Activity.dispatchKeyEvent → OtvNav，真遥控同路径）
        androidx.test.platform.app.InstrumentationRegistry.getInstrumentation()
            .sendKeyDownUpSync(k)
    }

    private fun awaitFocused(tag: String, timeout: Long = 5000) {
        try {
            rule.waitUntil(timeout) {
                runCatching { rule.onNodeWithTag(tag).assertIsFocused() }.isSuccess
            }
        } catch (e: Exception) {
            // 诊断：超时先区分「节点不存在」vs「存在但未聚焦」，再落盘语义树
            val exists = runCatching {
                rule.onNodeWithTag(tag).assertExists(); true
            }.getOrDefault(false)
            android.util.Log.e("OTV-TEST", "awaitFocused($tag) timeout, nodeExists=$exists")
            runCatching { rule.onRoot().printToLog("OTV-TEST") }
            throw e
        }
    }

    private fun assertFocused(tag: String) = rule.onNodeWithTag(tag).assertIsFocused()

    /** 等真实主线程延迟经过；autoSelect/scroll-reveal 都用 Dispatchers.Main 的真实时钟。 */
    private fun waitAtLeast(millis: Long) {
        val deadline = android.os.SystemClock.uptimeMillis() + millis
        rule.waitUntil(millis + 1_000) { android.os.SystemClock.uptimeMillis() >= deadline }
    }

    /** 一个可聚焦格子；scaleFocused=1f 关掉缩放弹簧（仪器测试不需要动画帧）。
     *  fr 非空 = 外置落焦句柄；initial=true 才是场景初焦目标（挂 InitialFocusEffect）
     *  —— override 定向落点（如 dest）只要 fr、不要 initial，两者不得混用。 */
    @Composable
    private fun Cell(
        tag: String,
        x: Int = 0,
        y: Int = 0,
        w: Int = 180,
        h: Int = 100,
        floating: Boolean = false,
        navGroup: String? = null,
        autoSelect: Boolean = false,
        autoSelectDelayMs: Long = 80L,
        fr: FocusRequester? = null,
        initial: Boolean = true,
        onClick: () -> Unit = {},
    ) {
        Box(
            Modifier
                .offset(x = x.dp, y = y.dp)
                .size(w.dp, h.dp)
                .background(Color(0xFF22304A))
                .testTag(tag)
                .dpadFocusable(
                    scaleFocused = 1f,
                    floating = floating,
                    navGroup = navGroup,
                    autoSelect = autoSelect,
                    autoSelectDelayMs = autoSelectDelayMs,
                    externalFocusRequester = fr,
                    onClick = onClick,
                ),
        )
        if (fr != null && initial) InitialFocusEffect(fr, tag)
    }

    /** 3×3 静态网格 + 初焦格子（initial 指定 tag） */
    @Composable
    private fun Grid(initialTag: String, initialFr: FocusRequester) {
        Box(Modifier.fillMaxSize()) {
            for (r in 0..2) for (c in 0..2) {
                val tag = "cell_$r-$c"
                Cell(tag, x = c * 200, y = r * 140, fr = initialFr.takeIf { tag == initialTag })
            }
        }
    }

    // ---------- 1) 静态网格：同列纪律与行内移动 ----------

    @Test
    fun `网格DOWN沿同列下移_不斜跳_底行封锁`() {
        scene { Grid("cell_0-0", remember { FocusRequester() }) }
        awaitFocused("cell_0-0")
        key(KeyEvent.KEYCODE_DPAD_DOWN)
        assertFocused("cell_1-0")
        key(KeyEvent.KEYCODE_DPAD_DOWN)
        assertFocused("cell_2-0")
        key(KeyEvent.KEYCODE_DPAD_DOWN) // 底行封锁：无候选无滚动，原地
        assertFocused("cell_2-0")
    }

    @Test
    fun `网格RIGHT行内右移_LEFT行首封锁`() {
        scene { Grid("cell_1-0", remember { FocusRequester() }) }
        awaitFocused("cell_1-0")
        key(KeyEvent.KEYCODE_DPAD_RIGHT)
        assertFocused("cell_1-1")
        key(KeyEvent.KEYCODE_DPAD_RIGHT)
        assertFocused("cell_1-2")
        key(KeyEvent.KEYCODE_DPAD_RIGHT) // 行尾封锁
        assertFocused("cell_1-2")
        key(KeyEvent.KEYCODE_DPAD_LEFT)
        key(KeyEvent.KEYCODE_DPAD_LEFT)
        assertFocused("cell_1-0")
        key(KeyEvent.KEYCODE_DPAD_LEFT) // 行首封锁
        assertFocused("cell_1-0")
    }

    @Test
    fun `网格UP上行_顶行UP无topFocus时原地封锁`() {
        scene { Grid("cell_1-2", remember { FocusRequester() }) }
        awaitFocused("cell_1-2")
        key(KeyEvent.KEYCODE_DPAD_UP)
        assertFocused("cell_0-2")
        key(KeyEvent.KEYCODE_DPAD_UP)
        assertFocused("cell_0-2")
    }

    // ---------- 2) 同行严格性：滚动残节点不得被水平选中 ----------

    @Test
    fun `RIGHT只走同行_与高列微叠的残节点不得劫持`() {
        scene {
            Box(Modifier.fillMaxSize()) {
                // chips 行 y=100..140（h40）；残节点 y=60..260（h200）与 chips 只叠 40dp
                // 40 < 0.5×200 → 非同行，RIGHT 不得跳上去（防乱跑核心规则）
                Cell("chipA", 0, 100, 120, 40, fr = remember { FocusRequester() })
                Cell("chipB", 140, 100, 120, 40)
                Cell("residue", 300, 60, 200, 200)
            }
        }
        awaitFocused("chipA")
        key(KeyEvent.KEYCODE_DPAD_RIGHT)
        assertFocused("chipB")
        key(KeyEvent.KEYCODE_DPAD_RIGHT) // residue 非同行 → 封锁
        assertFocused("chipB")
    }

    // ---------- 3) 悬浮层：参与水平行内导航，但绝不作为垂直候选 ----------

    @Test
    fun `悬浮行DOWN落内容_内容UP不得斜跳上悬浮行`() {
        scene {
            Box(Modifier.fillMaxSize()) {
                Cell("tab0", 0, 0, 120, 48, floating = true, fr = remember { FocusRequester() })
                Cell("tab1", 140, 0, 120, 48, floating = true)
                Cell("g0", 0, 160)
                Cell("g1", 200, 160)
                Cell("g2", 0, 300)
                Cell("g3", 200, 300)
            }
        }
        awaitFocused("tab0")
        key(KeyEvent.KEYCODE_DPAD_RIGHT) // 悬浮行内水平导航仍可用
        assertFocused("tab1")
        key(KeyEvent.KEYCODE_DPAD_DOWN) // 悬浮节点下行 → 内容首行 x 就近
        assertFocused("g1")
        key(KeyEvent.KEYCODE_DPAD_UP) // 内容 UP 不落悬浮 tab（被排除）→ 无 topFocus 原地封锁
        assertFocused("g1")
    }

    @Test
    fun `内容UP逃逸到topFocus_落tab行`() {
        scene {
            Box(Modifier.fillMaxSize()) {
                val tabFr = remember { FocusRequester() }
                SideEffect { OtvNav.topFocus = tabFr }
                Cell("tab0", 0, 0, 120, 48, floating = true, fr = tabFr)
                Cell("g0", 0, 160)
                Cell("g1", 200, 160)
            }
        }
        awaitFocused("tab0")
        key(KeyEvent.KEYCODE_DPAD_DOWN)
        // tab0 中心 x=105：g0 中心 157（距 52）< g1 中心 507 → 几何落 g0
        assertFocused("g0")
        key(KeyEvent.KEYCODE_DPAD_UP)
        assertFocused("tab0")
    }

    // ---------- 4) 区域级 override（tab DOWN 定向进内容等语义） ----------

    @Test
    fun `区域override消费DOWN_定向落焦压过几何`() {
        scene {
            Box(Modifier.fillMaxSize()) {
                val destFr = remember { FocusRequester() }
                CompositionLocalProvider(LocalNavOverride provides { dir ->
                    if (dir == NAV_DOWN) { runCatching { destFr.requestFocus() }; true } else false
                }) {
                    Cell("hero", 0, 0, 400, 200, fr = remember { FocusRequester() })
                }
                // 几何上 hero 正下方是 belowHero（DOWN 默认会落它）；
                // dest 在右侧远处——override 定向必须压过几何（dest 非初焦：initial=false）
                Cell("belowHero", 0, 240)
                Cell("dest", 600, 0, fr = destFr, initial = false)
            }
        }
        awaitFocused("hero")
        key(KeyEvent.KEYCODE_DPAD_DOWN)
        assertFocused("dest")
        assertNotFocusedNode("belowHero")
    }

    // ---------- 5) navGroup 封锁：浮窗组绝不逃逸 ----------

    @Test
    fun `navGroup内四向封锁_绝不逃逸到组外节点`() {
        scene {
            Box(Modifier.fillMaxSize()) {
                Cell("ov0", 100, 100, 120, 60, navGroup = "overlay", fr = remember { FocusRequester() })
                Cell("ov1", 240, 100, 120, 60, navGroup = "overlay")
                Cell("main0", 0, 300)
                Cell("main1", 200, 300)
            }
        }
        awaitFocused("ov0")
        key(KeyEvent.KEYCODE_DPAD_RIGHT) // 组内水平移动可用
        assertFocused("ov1")
        key(KeyEvent.KEYCODE_DPAD_DOWN) // 组下方有 main 卡也不得逃逸
        assertFocused("ov1")
        key(KeyEvent.KEYCODE_DPAD_LEFT)
        assertFocused("ov0")
        key(KeyEvent.KEYCODE_DPAD_UP) // UP 逃逸只对 group=null 开放
        assertFocused("ov0")
    }

    // ---------- 6) scroll-reveal：未组合目标瞬时滚一行后落焦 ----------

    @Test
    fun `LazyColumn_DOWN到底触发滚动找回_落下一行`() {
        scene {
            val listState = rememberLazyListState()
            val firstFr = remember { FocusRequester() }
            Box(Modifier.fillMaxSize()) {
                ProvideNavScrolls(vertical = lazyNavContainer(listState)) {
                    // 视口 360dp × 行高 100dp → item_3 半露、item_4 起未组合
                    LazyColumn(state = listState, modifier = Modifier.height(360.dp)) {
                        items(12) { i ->
                            Cell("item_$i", w = 200, h = 100, fr = firstFr.takeIf { i == 0 })
                        }
                    }
                }
            }
        }
        awaitFocused("item_0")
        key(KeyEvent.KEYCODE_DPAD_DOWN); assertFocused("item_1")
        key(KeyEvent.KEYCODE_DPAD_DOWN); assertFocused("item_2")
        key(KeyEvent.KEYCODE_DPAD_DOWN) // item_3 半露，仍几何候选
        awaitFocused("item_3")
        key(KeyEvent.KEYCODE_DPAD_DOWN) // item_4 未组合 → scroll-reveal
        awaitFocused("item_4")
    }

    @Test
    fun `hero播放钮DOWN首按直接落最近观看首卡_不会被滚动中的焦点恢复抢回`() {
        scene {
            val listState = rememberLazyListState()
            val heroFr = remember { FocusRequester() }
            val historyFr = remember { FocusRequester() }
            val scope = androidx.compose.runtime.rememberCoroutineScope()
            ProvideNavScrolls(vertical = lazyNavContainer(listState)) {
                LazyColumn(state = listState, modifier = Modifier.height(360.dp)) {
                    item(key = "hero") {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .height(280.dp)
                                .testTag("hero")
                                .dpadFocusable(
                                    scaleFocused = 1f,
                                    externalFocusRequester = heroFr,
                                    navOverride = { dir ->
                                        if (dir == NAV_DOWN) {
                                            scope.launch {
                                                runCatching { listState.scrollBy(480f) }
                                                repeat(8) {
                                                    kotlinx.coroutines.delay(60)
                                                    if (runCatching { historyFr.requestFocus() }.isSuccess) return@launch
                                                    runCatching { listState.scrollBy(300f) }
                                                }
                                            }
                                            true
                                        } else false
                                    },
                                    onClick = {},
                                ),
                        )
                    }
                    item(key = "history") {
                        Box(Modifier.height(200.dp)) {
                            Box(
                                Modifier
                                    .size(120.dp, 180.dp)
                                    .testTag("history")
                                    .dpadFocusable(
                                        scaleFocused = 1f,
                                        externalFocusRequester = historyFr,
                                        onClick = {},
                                    ),
                            )
                        }
                    }
                }
            }
            InitialFocusEffect(heroFr, "hero")
        }
        awaitFocused("hero")
        key(KeyEvent.KEYCODE_DPAD_DOWN)
        awaitFocused("history", timeout = 2500)
    }

    @Test
    fun `滚动找回期间新焦点接管_旧协程不得抢回光标`() {
        val escapeFr = FocusRequester()
        scene {
            val listState = rememberLazyListState()
            val firstFr = remember { FocusRequester() }
            Box(Modifier.fillMaxSize()) {
                ProvideNavScrolls(vertical = lazyNavContainer(listState)) {
                    LazyColumn(state = listState, modifier = Modifier.height(360.dp)) {
                        items(12) { i ->
                            Cell("item_$i", w = 200, h = 100, fr = firstFr.takeIf { i == 0 })
                        }
                    }
                }
                // 不在 LazyColumn 内：模拟标签切换/页面接管到的新焦点。
                Cell("escape", x = 600, y = 0, fr = escapeFr, initial = false)
            }
        }
        awaitFocused("item_0")
        repeat(3) { key(KeyEvent.KEYCODE_DPAD_DOWN) }
        awaitFocused("item_3")
        key(KeyEvent.KEYCODE_DPAD_DOWN) // 启动 scroll-reveal，尚未等到 50ms 重试
        androidx.test.platform.app.InstrumentationRegistry.getInstrumentation().runOnMainSync {
            escapeFr.requestFocus()
        }
        awaitFocused("escape")
        waitAtLeast(180) // 覆盖全部 3 次 scroll-reveal 的延迟窗口
        assertFocused("escape")
    }

    @Test
    fun `贴屏裁边节点主动聚焦可稳定持焦_失焦后不得再成为UP候选`() {
        val edgeFr = FocusRequester()
        val centerFr = FocusRequester()
        scene {
            Box(Modifier.fillMaxSize()) {
                // 上方仅剩 16dp 在窗口内：可被页面主动聚焦，但不得成为后续几何候选。
                Cell("edge", x = 0, y = -84, w = 120, h = 100, fr = edgeFr)
                Cell("center", x = 0, y = 100, w = 120, h = 100, fr = centerFr, initial = false)
            }
        }
        awaitFocused("edge")
        waitAtLeast(180) // 覆盖无效节点稳定器的 120ms 窗口
        assertFocused("edge")

        androidx.test.platform.app.InstrumentationRegistry.getInstrumentation().runOnMainSync {
            centerFr.requestFocus()
        }
        awaitFocused("center")
        key(KeyEvent.KEYCODE_DPAD_UP)
        assertFocused("center")
        assertNotFocusedNode("edge")
    }

    @Test
    fun `完全离屏节点被主动聚焦后_120毫秒内自愈到可见节点`() {
        val hiddenFr = FocusRequester()
        val visibleFr = FocusRequester()
        scene {
            Box(Modifier.fillMaxSize()) {
                Cell("offscreen", x = 0, y = -120, w = 120, h = 100, fr = hiddenFr, initial = false)
                Cell("visible", x = 0, y = 100, w = 120, h = 100, fr = visibleFr)
            }
        }

        awaitFocused("visible") // 先建立可见的分组锚点
        androidx.test.platform.app.InstrumentationRegistry.getInstrumentation().runOnMainSync {
            hiddenFr.requestFocus()
        }
        rule.onNodeWithTag("offscreen").assertIsFocused()
        awaitFocused("visible", timeout = 2500)
        assertNotFocusedNode("offscreen")
    }

    @Test
    fun `失焦后的延迟自动选择不得点击旧卡`() {
        scene {
            var staleClick by remember { mutableStateOf(false) }
            val firstFr = remember { FocusRequester() }
            Box(Modifier.fillMaxSize()) {
                Cell(
                    "auto",
                    x = 0,
                    y = 100,
                    autoSelect = true,
                    autoSelectDelayMs = 400,
                    fr = firstFr,
                    onClick = { staleClick = true },
                )
                Cell("next", x = 220, y = 100)
                if (staleClick) Box(Modifier.testTag("stale-auto-click"))
            }
        }
        awaitFocused("auto")
        key(KeyEvent.KEYCODE_DPAD_RIGHT)
        awaitFocused("next")
        waitAtLeast(500)
        check(runCatching { rule.onNodeWithTag("stale-auto-click").assertExists() }.isFailure) {
            "失焦后的 autoSelect 不得回调旧卡 onClick"
        }
        assertFocused("next")
    }

    // ---------- 7) 焦点销毁自愈（含 autoSelect 排除） ----------

    /** OK 键自毁的 mid + 左右邻居；anchor 中心离 right 更近 → 自愈必落 right */
    @Composable
    private fun HealScene(leftAutoSelect: Boolean) {
        var mid by remember { mutableStateOf(true) }
        val midFr = remember { FocusRequester() }
        Box(Modifier.fillMaxSize()) {
            Cell("left", 0, 100, 180, 100, autoSelect = leftAutoSelect)
            if (mid) {
                Box(
                    Modifier
                        .offset(x = 380.dp, y = 100.dp)
                        .size(180.dp, 100.dp)
                        .background(Color(0xFF22304A))
                        .testTag("mid")
                        .dpadFocusable(
                            scaleFocused = 1f,
                            externalFocusRequester = midFr,
                            onClick = { mid = false },
                        ),
                )
                InitialFocusEffect(midFr, "heal-mid")
            }
            Cell("right", 200, 100, 180, 100)
        }
    }

    @Test
    fun `聚焦节点OK自毁_按锚点就近补焦_不隐形`() {
        scene { HealScene(leftAutoSelect = false) }
        rule.waitUntil(5000) {
            runCatching { rule.onNodeWithTag("mid").assertIsFocused() }.isSuccess
        }
        key(KeyEvent.KEYCODE_DPAD_CENTER) // OK：onClick 自毁 → 焦点凭空消失 → 引擎自愈
        rule.waitUntil(5000) {
            runCatching { rule.onNodeWithTag("right").assertIsFocused() }.isSuccess
        }
    }

    @Test
    fun `自愈绝不落autoSelect节点_防劫持跳页`() {
        scene { HealScene(leftAutoSelect = true) }
        rule.waitUntil(5000) {
            runCatching { rule.onNodeWithTag("mid").assertIsFocused() }.isSuccess
        }
        key(KeyEvent.KEYCODE_DPAD_CENTER)
        rule.waitUntil(5000) {
            runCatching { rule.onNodeWithTag("right").assertIsFocused() }.isSuccess
        }
    }

    private fun assertNotFocusedNode(tag: String) {
        // 弱断言辅助：节点存在且未聚焦（用 try 反证避免引入额外依赖 API）
        rule.onNodeWithTag(tag).assertExists()
        check(runCatching { rule.onNodeWithTag(tag).assertIsFocused() }.isFailure) {
            "$tag 不应聚焦"
        }
    }
}
