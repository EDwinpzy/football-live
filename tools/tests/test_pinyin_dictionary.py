"""Validate the shipped native IME model before it reaches an APK."""

import struct
import unittest
from pathlib import Path


ASSET = Path(__file__).resolve().parents[2] / "android/app/src/main/assets/pinyin_dict_pinyin.dat"


class PinyinDictionaryTest(unittest.TestCase):
    def setUp(self):
        self.data = ASSET.read_bytes()
        spelling_size, spelling_count = struct.unpack_from("<II", self.data)
        self.list_offset = 13 + spelling_size * spelling_count
        self.char_count = struct.unpack_from("<I", self.data, self.list_offset)[0]
        self.positions = struct.unpack_from("<9I", self.data, self.list_offset + 4)
        self.ids = struct.unpack_from("<9I", self.data, self.list_offset + 40)
        self.chars_offset = self.list_offset + 76

    def test_list_header_has_real_single_character_and_lemma_ranges(self):
        self.assertEqual(self.positions[0], 0)
        self.assertGreater(self.positions[1], 0)
        self.assertEqual(self.ids[0], 1)
        self.assertEqual(self.positions[1], self.ids[1] - self.ids[0])
        self.assertEqual(sorted(self.positions), list(self.positions))
        self.assertEqual(sorted(self.ids), list(self.ids))
        for length in range(1, 9):
            self.assertEqual((self.positions[length] - self.positions[length - 1]) % length, 0)

    def test_single_characters_are_blank_first_and_sorted(self):
        chars = struct.unpack_from(f"<{self.char_count}H", self.data, self.chars_offset)
        self.assertEqual(chars[0], 0)
        self.assertEqual(list(chars), sorted(chars))

    def test_trie_and_ngram_consume_the_entire_model(self):
        offset = self.chars_offset + self.char_count * 4 + self.positions[-1] * 2
        roots, nodes, index_bytes, top_count = struct.unpack_from("<4I", self.data, offset)
        self.assertEqual(roots, 414)
        self.assertLess(top_count, index_bytes)
        # The model stores LE0 nodes as 16 bytes and GE1 nodes as 10 bytes.
        offset += 16 + roots * 16 + nodes * 10 + index_bytes
        lemma_count = struct.unpack_from("<I", self.data, offset)[0]
        self.assertEqual(lemma_count, self.ids[-1] + 1)
        self.assertEqual(offset + 4 + 256 * 2 + lemma_count, len(self.data))


if __name__ == "__main__":
    unittest.main()
