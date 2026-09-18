package com.qiubo.optimaltv.ui.paywall

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.unit.Dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavController
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.license.LicenseManager
import com.qiubo.optimaltv.license.LicenseState
import com.qiubo.optimaltv.ui.theme.OtvColors

/**
 * v1.17 会员/卡密付费的跨端共享层（TV 版与移动版字节级一致，tools/sync_shared.py 监管）：
 * 套餐目录、发卡链接、二维码绘制、状态行文案、统一播放入口门控。
 * 平台差异（焦点/触控 UI）留在各自的 PaywallScreen.kt / ActivateScreen.kt。
 */

/** 套餐目录（价格与 license_gen.py 票面 plan 对应；改价必须两端口径一致） */
data class PlanUi(val plan: String, val name: String, val price: String, val sub: String)

val PLANS = listOf(
    PlanUi("monthly", "月卡", "¥9.9", "30 天"),
    PlanUi("quarterly", "季卡", "¥29.9", "90 天"),
    PlanUi("yearly", "年卡", "¥99.9", "365 天"),
    PlanUi("lifetime", "终身", "¥128", "一次买断"),
)

/** v1.20 周卡（7 天）：仅后台签发赠送、不进 PLANS 购买列表（app 内不可购买），
 *  但激活后我的页/状态行要能正常显示套餐名——统一走本映射。 */
fun planDisplayName(plan: String): String = when (plan) {
    "weekly" -> "周卡"
    else -> PLANS.firstOrNull { it.plan == plan }?.name ?: plan
}

/** 发卡平台商品链接（M5 上架爱发电/面包多后替换为各套餐直达链接） */
object PurchaseLinks {
    private const val SHOP_HOME = "https://afdian.com"
    fun forPlan(@Suppress("UNUSED_PARAMETER") plan: String): String = SHOP_HOME
}

/**
 * 续费接口预留（2026-09-18 用户需求：移动端会员页去掉二维码，改为点击按钮续费）。
 *
 * 扫码通道下线后，会员页的续费按钮统一走这里发起。后台续费下单/支付接口接通后，只需在
 * App 启动时注入 [handler]（或直接替换 [request] 实现），会员页无需再改。
 * 返回 false = 通道尚未接通，页面据此给出「暂用卡密激活」的兜底提示。
 */
object RenewalGateway {
    var handler: ((plan: String) -> Boolean)? = null

    fun request(plan: String): Boolean = handler?.invoke(plan) ?: false
}

/** ZXing 生成 + Canvas 绘制二维码（无嵌入 View，纯 Compose；TV 大屏/移动小屏同款）。
 *  v1.20 性能修复：旧版按 512×512 请求矩阵并在 Canvas 每次重绘遍历全部 26 万像素
 *  （drawRect × 262144），聚焦切换/页面重组即掉帧卡顿（用户报障「会员界面很卡」
 *  主根因之一）。现改为 remember 一次性画进 Bitmap（编码即绘制，单次 ~10ms），
 *  Canvas 只贴图。 */
@Composable
fun QrCode(content: String, size: Dp, modifier: Modifier = Modifier) {
    val bmp = remember(content) {
        runCatching {
            val m = QRCodeWriter().encode(
                content, BarcodeFormat.QR_CODE, 512, 512,
                mapOf(EncodeHintType.MARGIN to 1),
            )
            // IntArray 批量填充 + setPixels 单次 JNI——逐像素 setPixel 是 26 万次
            // JNI 往返（低端机数百 ms），批量版全量 <10ms
            val white = 0xFFFFFFFF.toInt()
            val black = 0xFF000000.toInt()
            val px = IntArray(512 * 512) { i ->
                if (m.get(i % 512, i / 512)) black else white
            }
            android.graphics.Bitmap.createBitmap(px, 512, 512, android.graphics.Bitmap.Config.RGB_565)
        }.getOrNull()
    }
    Canvas(modifier.size(size)) {
        drawRect(Color.White, size = this.size)
        bmp?.let {
            drawImage(it.asImageBitmap(), dstSize = androidx.compose.ui.unit.IntSize(this.size.width.toInt(), this.size.height.toInt()))
        }
    }
}

/** 会员页标题旁的状态行（随授权状态实时变化） */
fun statusLine(st: LicenseState?): String = when {
    st == null -> ""
    st is LicenseState.NotActivated -> "尚未开通"
    else -> {
        val d = (st as LicenseState.Activated).data
        when {
            d.expiryAt != null && LicenseManager.effNow() >= d.expiryAt -> "已过期 · 续费后继续观看"
            !LicenseManager.isPremium() -> "需要联网校验会员状态"
            d.expiryAt == null -> "终身会员"
            else -> "会员有效 · 剩余 ${LicenseManager.daysLeft()} 天"
        }
    }
}

/**
 * 统一播放入口门控（方案 §4.5 全量门控收口点）：
 * 公测版未激活/已过期/已封禁 → 渲染本端会员页（不进播放页）；激活成功自动放行。
 * 内测版（LICENSE_ENABLED=false）直接透传，零行为差异。
 */
@Composable
fun LicensePlayerGate(nav: NavController, content: @Composable () -> Unit) {
    if (!BuildConfig.LICENSE_ENABLED) {
        content()
        return
    }
    val st by LicenseManager.state.collectAsStateWithLifecycle()
    LaunchedEffect(st) { LicenseManager.requestReverify() }
    when {
        st == null -> Box(Modifier.fillMaxSize().background(OtvColors.Bg))   // 状态加载中（启动门闸后瞬时）
        LicenseManager.isPremium() -> content()
        else -> PaywallScreen(nav)
    }
}
