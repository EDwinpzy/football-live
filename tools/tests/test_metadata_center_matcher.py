import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metadata_center.matcher import MATCH_THRESHOLD, group_media, merge_group, score_pair  # noqa: E402
from metadata_center.model import NormalizedMedia  # noqa: E402


def movie(**kwargs):
    payload = {
        "provider": "douban",
        "external_id": "1291546",
        "title": "霸王别姬",
        "year": 1993,
        "directors": ["陈凯歌"],
        "actors": ["张国荣", "张丰毅"],
        "runtime_minutes": 171,
    }
    payload.update(kwargs)
    return NormalizedMedia(**payload)


class MatcherTests(unittest.TestCase):
    def test_shared_external_id_wins(self):
        left = movie(provider="douban", external_ids={"imdb": "tt0106332"})
        right = movie(provider="tmdb", external_id="10997", external_ids={"imdb": "tt0106332"})
        score, reason = score_pair(left, right)
        self.assertEqual(score, 100.0)
        self.assertIn("shared_external_id", reason)

    def test_title_year_director_scores_above_threshold(self):
        left = movie()
        right = movie(provider="tmdb", external_id="10997", actors=[])
        score, reason = score_pair(left, right)
        self.assertGreaterEqual(score, MATCH_THRESHOLD)
        self.assertIn("title", reason)
        self.assertIn("year", reason)
        self.assertIn("director", reason)

    def test_title_only_stays_below_review_threshold(self):
        left = movie()
        right = movie(provider="tmdb", external_id="999", year=2003, directors=["别人"], actors=[],
                      runtime_minutes=90)
        score, _ = score_pair(left, right)
        self.assertLess(score, 60.0)

    def test_same_provider_duplicates_are_not_collapsed(self):
        left = movie(external_id="1", year=1993)
        right = movie(external_id="2", year=1993)
        outcome = group_media([left, right])
        self.assertEqual(len(outcome.groups), 2)

    def test_cross_provider_group_merges_once(self):
        left = movie(external_ids={"imdb": "tt0106332"})
        right = movie(provider="tmdb", external_id="10997", overview="detail", has_detail=True,
                      external_ids={"imdb": "tt0106332", "tmdb": "10997"})
        outcome = group_media([left, right])
        self.assertEqual(len(outcome.groups), 1)
        merged = merge_group(outcome.groups[0])
        self.assertEqual(merged.overview, "detail")
        self.assertEqual(merged.external_ids["tmdb"], "10997")
        self.assertEqual(merged.external_ids["douban"], "1291546")


if __name__ == "__main__":
    unittest.main()
