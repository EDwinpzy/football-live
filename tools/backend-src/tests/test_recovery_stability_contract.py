import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


class RecoveryStabilityContractTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
