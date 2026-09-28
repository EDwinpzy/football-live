import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

import build_vod_line_index  # noqa: E402


def _line(*, quality_score=97, match_score=None):
    line = {
        "id": "fixture:42:0",
        "source_id": "fixture",
        "source_name": "受控源",
        "name": "受控源·主线",
        "score": quality_score,
        "episodes": [{"index": 0, "name": "正片", "url": "https://media.example/1.m3u8"}],
    }
    if match_score is not None:
        line["match_score"] = match_score
    return line


class _Registry:
    def __init__(self, lines):
        self.lines = lines

    def resolve_subject(self, _subject, wait_for_all=False):
        return self.lines


class VodLineIndexBuilderTest(unittest.TestCase):
    def _build_line(self, line):
        subject = {
            "key": "1291001", "title": "电影甲", "year": "2024",
            "category": "movie", "season": 0,
        }
        registry = _Registry([line])
        return build_vod_line_index.resolve_one(registry, subject)[1]["lines"][0]

    def test_static_entry_preserves_independent_title_match_score_and_quality_score(self):
        line = self._build_line(_line(match_score=100))

        self.assertEqual(line["match_score"], 100)
        self.assertEqual(line["score"], 97)

    def test_static_entry_preserves_low_title_match_score_without_promoting_quality_score(self):
        line = self._build_line(_line(match_score=79))

        self.assertEqual(line["match_score"], 79)
        self.assertEqual(line["score"], 97)

    def test_missing_title_match_score_is_not_filled_from_quality_score(self):
        line = self._build_line(_line())

        self.assertNotIn("match_score", line)
        self.assertEqual(line["score"], 97)

    def test_static_episode_without_explicit_server_index_is_dropped(self):
        source_line = _line(match_score=100)
        source_line["episodes"] = [{"name": "Episode 1", "url": "https://media.example/1.m3u8"}]
        registry = _Registry([source_line])
        subject = {
            "key": "1291001", "title": "电影甲", "year": "2024",
            "category": "movie", "season": 0,
        }

        entry = build_vod_line_index.resolve_one(registry, subject)[1]

        self.assertEqual(entry["lines"], [])


if __name__ == "__main__":
    unittest.main()
