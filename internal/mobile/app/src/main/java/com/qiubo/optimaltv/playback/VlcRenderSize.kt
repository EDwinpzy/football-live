package com.qiubo.optimaltv.playback

/** VLC's native output window is sized in physical surface pixels, not Compose dp. */
internal data class VlcRenderSize(val widthPx: Int, val heightPx: Int) {
    val packed: Long
        get() = (widthPx.toLong() shl 32) or (heightPx.toLong() and 0xFFFF_FFFFL)

    companion object {
        fun fromViewSize(widthPx: Int, heightPx: Int): VlcRenderSize? =
            if (widthPx > 0 && heightPx > 0) VlcRenderSize(widthPx, heightPx) else null
    }
}
