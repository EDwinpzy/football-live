import unittest

import numpy as np

from compare_live_delay import estimate_added_delay


def feature(value):
    return np.full((36, 64), value, dtype=np.float32)


class EstimateAddedDelayTests(unittest.TestCase):
    def test_subtracts_raw_decoder_baseline_latency(self):
        # The raw decoder's newest output is already 12 seconds behind wall time.
        # An app frame matching that head has zero additional delay.
        reference = [(index, float(index), feature(index % 255)) for index in range(10, 31)]
        estimate = estimate_added_delay(reference, 32.0, feature(30))
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["addedDelaySeconds"], 0.0)
        self.assertEqual(estimate["referenceFrameAgeSeconds"], 2.0)

    def test_reports_six_seconds_behind_raw_head(self):
        reference = [(index, 41.0, feature(index % 255)) for index in range(31)]
        estimate = estimate_added_delay(reference, 41.1, feature(0))
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["addedDelaySeconds"], 6.0)
        self.assertTrue(estimate["distinctMatch"])

    def test_segment_burst_timing_does_not_shrink_frame_offset(self):
        # A complete segment may decode in a short burst. Frame content time,
        # not host arrival spacing, must still show the six-second difference.
        reference = [(index, 10.0, feature(index % 255)) for index in range(31)]
        estimate = estimate_added_delay(reference, 10.1, feature(0))
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["addedDelaySeconds"], 6.0)
        self.assertTrue(estimate["distinctMatch"])

    def test_static_or_ambiguous_images_are_not_a_strong_match(self):
        reference = [(index, float(index), feature(7)) for index in range(1, 21)]
        estimate = estimate_added_delay(reference, 21.0, feature(7))
        self.assertIsNotNone(estimate)
        self.assertFalse(estimate["distinctMatch"])

    def test_records_reference_burst_through_screenshot_end(self):
        reference = [(i, 10.0 if i < 20 else 10.4, feature(i)) for i in range(31)]
        estimate = estimate_added_delay(reference, 10.2, feature(10), 10.5)
        self.assertEqual(estimate["referenceHeadFrameIndex"], 19)
        self.assertEqual(estimate["referenceHeadFrameIndexAtScreenshotEnd"], 30)


if __name__ == "__main__":
    unittest.main()
