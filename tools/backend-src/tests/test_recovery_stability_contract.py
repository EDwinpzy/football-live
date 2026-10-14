import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


class RecoveryStabilityContractTest(unittest.TestCase):
    def test_cancelled_live_resolution_does_not_publish_failure_or_retry(self):
        for client in ('android', 'internal/mobile'):
            text = (ROOT / client / 'app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt').read_text(encoding='utf-8')
            start = text.index('private suspend fun bootLive(fresh: Boolean)')
            end = text.index('\n    private ', start + 1)
            body = text[start:end]
            self.assertNotIn('runCatching { Graph.live.liveStreamSources', body)
            self.assertIn('throw cancelled', body)
            self.assertLess(body.index('throw cancelled'), body.index('sources.isEmpty()'))
            self.assertLess(body.index('currentCoroutineContext().ensureActive()'), body.index('sources.isEmpty()'))

    def test_short_restarts_do_not_reset_recovery_ladder(self):
        for client in ('android', 'internal/mobile'):
            text = (ROOT / client / 'app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt').read_text(encoding='utf-8')
            start = text.index('private suspend fun onSessionStarted')
            end = text.index('\n    private ', start + 1)
            self.assertNotIn('recoveryAttempt = 0', text[start:end])
            self.assertIn('if (playing && playbackProgressed)', text)
            self.assertIn('PlaybackRecoveryCoordinationPolicy.hasPlaybackProgress(', text)
            self.assertIn('recoveryInFlight = stallRecoveryJob?.isActive == true', text)
            start = text.index('if (healthyTicks >= HEALTH_HEALTHY_TICKS)')
            self.assertIn('recoveryAttempt = 0', text[start:start + 200])

    def test_vod_reload_reprepares_the_current_resolved_session_url(self):
        for client in ('android', 'internal/mobile'):
            text = (ROOT / client / 'app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt').read_text(encoding='utf-8')
            reload_start = text.index('RecoveryAction.RELOAD_CURRENT ->')
            refresh_start = text.index('RecoveryAction.REFRESH_CURRENT ->', reload_start)
            reload_branch = text[reload_start:refresh_start]
            self.assertIn('startRecoverySession(', reload_branch)
            self.assertIn('current,', reload_branch)
            self.assertIn('if (state.isLive) 0L else resume', reload_branch)
            self.assertIn('reuseResolvedUrl = true', reload_branch)
            self.assertNotIn('seekTo(', reload_branch)
            self.assertNotIn('.play()', reload_branch)

            start_session = text.index('private suspend fun startSession(')
            end_session = text.index('\n    private ', start_session + 1)
            session_body = text[start_session:end_session]
            reuse_start = session_body.index('val reuseVODSessionUrl')
            reuse_end = session_body.index('// Do not leave an older episode/source URL eligible', reuse_start)
            reuse_guard = session_body[reuse_start:reuse_end]
            self.assertIn('ResolvedSessionUrlReusePolicy.vodUrl(', reuse_guard)
            self.assertIn('requestedLine = lineIdx', reuse_guard)
            self.assertIn('activeLine = _ui.value.activeLine', reuse_guard)
            self.assertIn('sessionUrl = sessionUrl', reuse_guard)
            self.assertNotIn('startedThisSession', reuse_guard)
            self.assertIn('if (reuseResolvedUrl && isVod && reuseVODSessionUrl == null)', session_body)
            self.assertIn('sessionUrl = ""', session_body)
            self.assertLess(session_body.index('sessionUrl = ""'), session_body.index('sessionUrl = playUrl'))
            self.assertIn('reuseVODSessionUrl ?: url', session_body)
            self.assertIn('sessionUrl = playUrl', session_body)
            self.assertIn('prepare(PrepareRequest(playUrl, startPos))', session_body)

            reuse_policy = (ROOT / client / 'app/src/main/java/com/qiubo/optimaltv/playback/PlaybackRecoveryPolicy.kt').read_text(encoding='utf-8')
            policy_start = reuse_policy.index('object ResolvedSessionUrlReusePolicy')
            policy_end = reuse_policy.index('\n}', policy_start) + 2
            policy_body = reuse_policy[policy_start:policy_end]
            self.assertIn('requestedLine == activeLine', policy_body)
            self.assertIn('it.isNotBlank()', policy_body)
            self.assertNotIn('startedThisSession', policy_body)

            recovery_session = text.index('private suspend fun startRecoverySession(')
            recovery_session_end = text.index('\n    private ', recovery_session + 1)
            recovery_session_body = text[recovery_session:recovery_session_end]
            self.assertIn('reuseResolvedUrl: Boolean = false', recovery_session_body)
            self.assertIn('startSession(line, resume, reuseResolvedUrl = reuseResolvedUrl)', recovery_session_body)

        mobile = (ROOT / 'internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt').read_text(encoding='utf-8')
        error_branch = mobile[mobile.index('st.state == EnginePlayState.ERROR &&'):mobile.index('\n                }\n            }', mobile.index('st.state == EnginePlayState.ERROR &&'))]
        self.assertIn('(startedThisSession || awaitingPrepare)', error_branch)
        self.assertIn('startSession(cur, if (pos > 1_000) pos else 0L, reuseResolvedUrl = true)', mobile)

    def test_switching_source_must_not_crash_when_the_previous_resolve_is_cancelled(self):
        """换源取消上一轮解析时，被打断的 IO 不得被包成业务异常再抛。

        实机复现（TV 3.0.16 / 326，2026-10-14，emulator-5556 连按换源第 2 轮）：

            FATAL EXCEPTION: main
            Process: com.qiubo.optimaltv.public
            com.qiubo.optimaltv.data.repo.SelectedSourceRefreshException: 无法刷新所选信号源
                at VodRepository$resolvePlayUrl$2.invokeSuspend(VodRepository.kt:1668)
            Suppressed: DiagnosticCoroutineContextException: [StandaloneCoroutine{Cancelling}]
            Caused by: java.io.InterruptedIOException: interrupted
                at okio.Timeout.throwIfReached
                at ... VodApiSource.fetchPlayJson(VodApiSource.kt:107)

        `runInterruptible` 打断阻塞中的 OkHttp 读，抛的是 `InterruptedIOException`
        （`IOException`，不是 `CancellationException`），被 `catch (failure: Exception)`
        接住后包成 `SelectedSourceRefreshException` 抛出。此时协程**已经取消**，
        在已取消的协程里抛非取消异常不会有人接 → 冒到线程未捕获处理器 → 闪退。
        修法是每个会抛业务异常的分支先 `ensureActive()`：取消优先，取消时只抛
        `CancellationException`（正常取消，不崩）。
        """
        android = (ROOT / 'android/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt').read_text(encoding='utf-8')
        self.assertIn('import kotlinx.coroutines.currentCoroutineContext', android)
        self.assertIn('import kotlinx.coroutines.ensureActive', android)

        start = android.index('suspend fun resolvePlayUrl(')
        end = android.index('// ---------- 目录磁盘快照', start)
        body = android[start:end]

        # 取消分支必须先于任何业务异常转换，且四个 catch 分支都要有取消闸门。
        self.assertEqual(body.count('currentCoroutineContext().ensureActive()'), 3)
        for wrapper in (
            'throw SelectedSourceRefreshException("刷新后所选信号源仍不可用"',
            'throw SelectedSourceRefreshException("无法刷新所选信号源"',
        ):
            index = body.index(wrapper)
            self.assertLess(
                body.rindex('currentCoroutineContext().ensureActive()', 0, index), index,
                '%s 之前必须先判取消' % wrapper)

        # 移动版是另一份实现：catch 全兜底且不抛，取消时返回空串，同样不会崩。
        mobile = (ROOT / 'internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt').read_text(encoding='utf-8')
        mobile_start = mobile.index('suspend fun resolvePlayUrl(')
        mobile_end = mobile.index('// ---------- 目录磁盘快照', mobile_start)
        mobile_body = mobile[mobile_start:mobile_end]
        self.assertIn('catch (cancelled: CancellationException)', mobile_body)
        self.assertNotIn('throw SelectedSourceRefreshException', mobile_body)


if __name__ == '__main__':
    unittest.main()
