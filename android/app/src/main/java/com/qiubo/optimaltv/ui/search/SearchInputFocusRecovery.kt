package com.qiubo.optimaltv.ui.search

/** Restores a vanished input node only until another region takes focus. Main-thread only. */
internal class SearchInputFocusRecovery<T> {
    private var lostAt = 0L
    private var target: T? = null

    fun onInputLost(now: Long, requester: T) {
        lostAt = now
        target = requester
    }

    fun onFocusGained() {
        target = null
    }

    fun takeRestoreTarget(now: Long): T? {
        val pending = target
        target = null
        return pending?.takeIf { now - lostAt in 0L until 600L }
    }
}
