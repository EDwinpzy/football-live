import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evaluate_live_acceptance import evaluate_delay


class EvaluateDelayReferenceProgressTests(unittest.TestCase):
    def _evaluate(self, mutate=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            samples = [{
                "serial": "test-device", "elapsedSeconds": 10 + index * 8,
                "addedDelaySeconds": 1.0, "referenceHeadFrameIndex": 50 + index * 40,
                "referenceHeadFrameIndexAtScreenshotEnd": 51 + index * 40,
                "matchedReferenceFrameIndex": 45 + index * 40,
                "referenceHeadIdleSeconds": 1.0, "imageDifference": 1.0,
                "alternativeMargin": 4.0, "screenshotTimingSpanSeconds": 0.2,
                "distinctMatch": True,
            } for index in range(7)]
            report = {"matchId": "123", "sourceId": "source-0", "referenceFps": 5, "samples": samples}
            if mutate:
                mutate(report)
            path = root / "delay" / "initial" / "123.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(report), encoding="utf-8")
            (root / "delay-probes.json").write_text(json.dumps({"probes": [{
                "phase": "initial", "matchId": "123", "startedAt": now.isoformat(),
                "finishedAt": (now + timedelta(seconds=60)).isoformat(),
                "report": "delay/initial/123.json",
            }]}), encoding="utf-8")
            return evaluate_delay(root, {"targets": [{"id": "tv-123", "serial": "test-device", "matchId": "123"}]},
                {"allTargetsReadyAt": (now - timedelta(seconds=5)).isoformat()},
                phase="initial", max_delay=5.0, minimum_samples=3, max_growth=2.0, required_seconds=3600)

    def assert_rejected(self, mutate):
        details, failures = self._evaluate(mutate)
        self.assertFalse(details["pass"])
        self.assertTrue(failures)

    def test_advancing_reference_passes(self):
        details, failures = self._evaluate()
        self.assertTrue(details["pass"])
        self.assertFalse(failures)

    def test_reference_stall_even_with_weak_match_rejected(self):
        def mutate(report):
            report["samples"][3].update(referenceHeadIdleSeconds=12, distinctMatch=False)
        self.assert_rejected(mutate)

    def test_early_burst_then_missing_observations_rejected(self):
        self.assert_rejected(lambda r: r.update(samples=r["samples"][:3]))

    def test_head_advances_too_slowly_rejected(self):
        def mutate(report):
            for i, row in enumerate(report["samples"]):
                row.update(referenceHeadFrameIndex=50 + i * 5, referenceHeadFrameIndexAtScreenshotEnd=51 + i * 5, matchedReferenceFrameIndex=45 + i * 5)
        self.assert_rejected(mutate)

    def test_boundary_includes_capture_and_frame_uncertainty(self):
        def mutate(report):
            for row in report["samples"]:
                row.update(addedDelaySeconds=5.0, matchedReferenceFrameIndex=row["referenceHeadFrameIndex"] - 25)
        self.assert_rejected(mutate)

    def test_inconsistent_frame_offsets_rejected(self):
        self.assert_rejected(lambda r: [row.update(addedDelaySeconds=0) for row in r["samples"]])

    def test_burst_during_screenshot_counts_toward_limit(self):
        def mutate(report):
            for row in report["samples"]:
                row["referenceHeadFrameIndexAtScreenshotEnd"] = row["referenceHeadFrameIndex"] + 20
        self.assert_rejected(mutate)

    def test_invalid_measurements_rejected(self):
        for field, value in [("screenshotTimingSpanSeconds", -1), ("elapsedSeconds", float("nan")),
                             ("referenceHeadIdleSeconds", float("nan")), ("imageDifference", -1)]:
            with self.subTest(field=field):
                self.assert_rejected(lambda r: [row.update({field: value}) for row in r["samples"]])

    def test_missing_reference_freshness_rejected(self):
        self.assert_rejected(lambda r: [row.pop("referenceHeadIdleSeconds") for row in r["samples"]])


if __name__ == "__main__":
    unittest.main()
