import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import vlc_hls


def playlist(start, *, count=3, prefix="seg", dseq=None, leading_disc=False):
    header = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
    if dseq is not None:
        header += "#EXT-X-DISCONTINUITY-SEQUENCE:%d\n" % dseq
    header += "#EXT-X-MEDIA-SEQUENCE:%d\n" % start
    if leading_disc:
        header += "#EXT-X-DISCONTINUITY\n"
    return header + "".join(
        "#EXTINF:2,\n%s-%d.ts\n" % (prefix, index)
        for index in range(start, start + count))


class VlcHlsContinuityTests(unittest.TestCase):
    def setUp(self):
        with vlc_hls._lock:
            vlc_hls._windows.clear()
        self.session = "a" * 32
        self.url = "https://source.example/live.m3u8"

    def normalize(self, text, session=None):
        return vlc_hls.normalize(text, self.url, session or self.session)

    def test_missed_window_is_compacted_and_marked_discontinuous(self):
        first = self.normalize(playlist(100))
        second = self.normalize(playlist(150))
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", first)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:3", second)
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:0", second)
        self.assertIn("#EXT-X-DISCONTINUITY\n#EXTINF:2,\nseg-150.ts", second)

    def test_contiguous_window_advances_without_a_synthetic_discontinuity(self):
        self.normalize(playlist(100))
        contiguous = self.normalize(playlist(103))
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:3", contiguous)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n", contiguous)

    def test_overlap_preserves_synthetic_sequence_and_same_window_is_stable(self):
        self.normalize(playlist(100))
        latest = self.normalize(playlist(101))
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", latest)
        self.assertEqual(latest, self.normalize(playlist(101)))
        self.assertEqual(latest, self.normalize(playlist(100)))

    def test_sliding_past_synthetic_discontinuity_carries_discontinuity_sequence(self):
        first = self.normalize(playlist(100, dseq=4))
        gap = self.normalize(playlist(150, dseq=4))
        after_gap = self.normalize(playlist(151, dseq=4))
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:4", first)
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:4", gap)
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:5", after_gap)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n", after_gap)

    def test_source_leading_discontinuity_is_not_duplicated_for_a_gap(self):
        self.normalize(playlist(100))
        gap = self.normalize(playlist(150, leading_disc=True))
        self.assertEqual(1, gap.count("#EXT-X-DISCONTINUITY\n"))
        self.assertIn("#EXTINF:2,\nseg-150.ts", gap)

    def test_stale_and_shorter_same_start_windows_do_not_rewind(self):
        self.normalize(playlist(100))
        latest = self.normalize(playlist(102))
        self.assertEqual(latest, self.normalize(playlist(100)))
        self.assertEqual(latest, self.normalize(playlist(102, count=2)))

    def test_non_overlapping_sequence_reset_fails_closed(self):
        self.normalize(playlist(100))
        self.normalize(playlist(150))
        with self.assertRaisesRegex(ValueError, "sequence reset or stale window"):
            self.normalize(playlist(0))

    def test_missing_source_dseq_is_inferred_when_source_discontinuity_slides_out(self):
        first = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            "#EXT-X-MEDIA-SEQUENCE:100\n"
            "#EXTINF:2,\nseg-100.ts\n"
            "#EXT-X-DISCONTINUITY\n"
            "#EXTINF:2,\nseg-101.ts\n"
            "#EXTINF:2,\nseg-102.ts\n"
        )
        second = playlist(102)
        self.normalize(first)
        output = self.normalize(second)
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:1", output)

    def test_aes128_implicit_iv_is_preserved_after_sequence_rewrite(self):
        encrypted = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            "#EXT-X-MEDIA-SEQUENCE:100\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="keys/key,blue.bin"\n'
            "#EXTINF:2,\nseg-100.ts\n"
            "#EXTINF:2,\nseg-101.ts\n"
        )
        output = self.normalize(encrypted)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", output)
        self.assertIn(
            '#EXT-X-KEY:METHOD=AES-128,URI="keys/key,blue.bin",IV=0x%032x\nseg-100.ts' % 100,
            output)
        self.assertIn(
            '#EXT-X-KEY:METHOD=AES-128,URI="keys/key,blue.bin",IV=0x%032x\nseg-101.ts' % 101,
            output)

    def test_implicit_iv_after_a_gap_uses_original_not_compacted_sequence(self):
        encrypted = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            "#EXT-X-MEDIA-SEQUENCE:100\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=key.bin\n"
            "#EXTINF:2,\nseg-100.ts\n"
        )
        self.normalize(encrypted)
        after_gap = encrypted.replace("MEDIA-SEQUENCE:100", "MEDIA-SEQUENCE:150") \
            .replace("seg-100", "seg-150")
        output = self.normalize(after_gap)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", output)
        self.assertIn("IV=0x%032x\nseg-150.ts" % 150, output)

    def test_explicit_iv_and_non_identity_encryption_are_not_rewritten(self):
        explicit = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            "#EXT-X-MEDIA-SEQUENCE:100\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x1234\n'
            "#EXTINF:2,\nseg-100.ts\n"
        )
        output = self.normalize(explicit)
        self.assertEqual(1, output.count("#EXT-X-KEY:"))
        self.assertIn("IV=0x1234", output)

        non_identity = explicit.replace(
            'METHOD=AES-128,URI="key.bin",IV=0x1234',
            'METHOD=AES-128,URI="key.bin",KEYFORMAT="com.example",IV=0x1234')
        self.setUp()
        non_identity = non_identity.replace(",IV=0x1234", "")
        output = self.normalize(non_identity)
        self.assertEqual(1, output.count("#EXT-X-KEY:"))
        self.assertNotIn(",IV=0x", output)

    def test_key_rotation_and_method_none_apply_per_segment(self):
        encrypted = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:30\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=first.key\n"
            "#EXTINF:2,\nseg-30.ts\n"
            "#EXT-X-KEY:METHOD=NONE\n"
            "#EXTINF:2,\nseg-31.ts\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=second.key\n"
            "#EXTINF:2,\nseg-32.ts\n"
        )
        output = self.normalize(encrypted)
        self.assertIn("URI=first.key,IV=0x%032x\nseg-30.ts" % 30, output)
        self.assertIn("#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\nseg-31.ts", output)
        self.assertIn("URI=second.key,IV=0x%032x\nseg-32.ts" % 32, output)

    def test_independent_players_have_independent_windows(self):
        self.normalize(playlist(100))
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0",
                      self.normalize(playlist(150), session="b" * 32))

    def test_vod_master_unmarked_delta_and_implicit_sequence_playlists_are_unchanged(self):
        text = playlist(100)
        self.assertEqual(text, vlc_hls.normalize(text, self.url, None))
        self.assertEqual(text, vlc_hls.normalize(text, self.url, "invalid"))
        self.assertEqual(text + "#EXT-X-ENDLIST\n",
                         self.normalize(text + "#EXT-X-ENDLIST\n"))
        self.assertEqual(text.replace("#EXT-X-MEDIA-SEQUENCE:100\n", ""),
                         self.normalize(text.replace("#EXT-X-MEDIA-SEQUENCE:100\n", "")))
        self.assertEqual("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.m3u8\n",
                         self.normalize("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.m3u8\n"))
        delta = text.replace("#EXT-X-MEDIA-SEQUENCE:100\n", "#EXT-X-MEDIA-SEQUENCE:100\n#EXT-X-SKIP:SKIPPED-SEGMENTS=4\n")
        self.assertEqual(delta, self.normalize(delta))

    def test_invalid_segment_duration_is_rejected_before_native_decode(self):
        with self.assertRaises(ValueError):
            self.normalize(playlist(100).replace("#EXTINF:2,", "#EXTINF:0,"))


if __name__ == "__main__":
    unittest.main()
