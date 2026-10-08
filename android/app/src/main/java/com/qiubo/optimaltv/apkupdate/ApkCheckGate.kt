package com.qiubo.optimaltv.apkupdate

/** Foreground edges trigger checks; one request owns the check until it settles. */
internal class ApkCheckGate {
    private var foreground = false
    private var checking = false

    @Synchronized fun onForegroundChanged(value: Boolean): Boolean {
        val entered = value && !foreground
        foreground = value
        return entered
    }

    @Synchronized fun tryStartCheck(): Boolean {
        if (checking) return false
        checking = true
        return true
    }

    @Synchronized fun finishCheck() { checking = false }
}
