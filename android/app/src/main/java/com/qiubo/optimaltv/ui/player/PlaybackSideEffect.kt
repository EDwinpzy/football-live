package com.qiubo.optimaltv.ui.player

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive

/** Optional prefetch/persistence failures must not terminate the active player. */
internal suspend fun isolatePlaybackSideEffect(
    onFailure: (Exception) -> Unit,
    block: suspend () -> Unit,
) {
    try {
        block()
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (failure: Exception) {
        // A blocking API may report its own exception after session cancellation.
        currentCoroutineContext().ensureActive()
        onFailure(failure)
    }
}
