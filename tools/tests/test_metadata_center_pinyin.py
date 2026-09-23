import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend-src"))

from metadata_center.model import normalize_text  # noqa: E402
from metadata_center.pinyin import PinyinEngine  # noqa: E402


class PinyinTests(unittest.TestCase):
    def setUp(self):
        self.engine = PinyinEngine()

    def test_single_reading_title(self):
        roman = self.engine.romanize("霸王别姬")
        self.assertEqual(roman.pinyin, "bawangbieji")
        self.assertEqual(roman.initials, "bwbj")

    def test_phrase_override_beats_character_default(self):
        roman = self.engine.romanize("长安十二时辰")
        self.assertEqual(roman.pinyin, "changanshiershichen")
        self.assertEqual(roman.initials, "casesc")
        # The legacy single-reading map would produce zhang...; keep it searchable.
        self.assertIn("zhanganshiershichen", roman.pinyin_alts)

    def test_polyphonic_variants_are_indexed(self):
        roman = self.engine.romanize("重庆")
        self.assertEqual(roman.pinyin, "chongqing")
        self.assertIn("zhongqing", roman.pinyin_alts)
        self.assertEqual(roman.initials, "cq")

    def test_latin_and_digits_survive(self):
        roman = self.engine.romanize("Spider-Man 3")
        self.assertEqual(roman.pinyin, "spiderman3")
        self.assertEqual(roman.initials, "spiderman3")

    def test_variant_list_is_bounded(self):
        roman = self.engine.romanize("重长行乐还都便藏会差")
        self.assertLessEqual(len(roman.pinyin_alts), 8)

    def test_normalization_matches_device_index(self):
        import media_index  # noqa: E402  (tools/backend-src)

        for value in ("霸王别姬", "Spider-Man: No Way Home", "  长安 十二时辰 ", "ＯＮＥ"):
            self.assertEqual(normalize_text(value), media_index.normalize_text(value))


if __name__ == "__main__":
    unittest.main()
