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


if __name__ == '__main__':
    unittest.main()
