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

if __name__ == '__main__':
    unittest.main()
