package com.qiubo.optimaltv.ui.paywall

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.unit.Dp
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import com.qiubo.optimaltv.license.LicenseManager
import com.qiubo.optimaltv.license.LicenseState

/**
 * v1.17 会员/卡密付费的跨端共享层（TV 版与移动版字节级一致，tools/sync_shared.py 监管）：
 * 套餐目录、发卡链接、二维码绘制与状态行文案。
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
