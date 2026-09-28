import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

import build_vod_line_index  # noqa: E402
import vod_sources  # noqa: E402


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


class _LiveRegistry(_Registry):
    """A registry that knows the site behind each source, as a real one does.

    ``resolve_subject`` hands back lines stamped with the registry's own id, so
    the builder has to look that id up to learn the site behind it.
    """

    def __init__(self, lines, site_url="https://www.hongniuzy2.com"):
        source_id = "10000000-0000-0000-0000-000000000007"
        stamped = []
        for line in lines:
            line = dict(line)
            line["source_id"] = source_id
            line["id"] = "%s:42:0" % source_id
            stamped.append(line)
        super().__init__(stamped)
        self.sources = (vod_sources.VodSource(
            source_id, "受控源", site_url,
            site_url + "/api.php/provide/vod/", "macms_json"),)


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

    def test_line_records_the_site_host_the_registry_will_be_joined_on(self):
        """A rebuilt index has to carry the key the published registry can be
        matched on.

        The published registry mints UUID ids, so a line that only remembers the
        local short id cannot be joined to it. Recording the site host makes the
        line resolvable no matter what the id became, or whether the source was
        renamed after the index was built.
        """
        subject = {
            "key": "1291001", "title": "电影甲", "year": "2024",
            "category": "movie", "season": 0,
        }
        registry = _LiveRegistry([_line(match_score=100)])

        line = build_vod_line_index.resolve_one(registry, subject)[1]["lines"][0]

        self.assertEqual(line["source_alias"], "hongniuzy2.com")

    def test_line_from_a_source_the_registry_cannot_locate_carries_no_alias(self):
        """A fabricated source has no site, so there is nothing to record.

        Writing a guess here would make an unjoinable line look joinable and
        turn a "no source" page into a failed playback instead.
        """
        subject = {
            "key": "1291001", "title": "电影甲", "year": "2024",
            "category": "movie", "season": 0,
        }
        registry = _Registry([_line(match_score=100)])

        line = build_vod_line_index.resolve_one(registry, subject)[1]["lines"][0]

        self.assertNotIn("source_alias", line)

    def test_a_line_keeps_its_local_id_and_also_records_the_alias(self):
        """The alias is additive: whatever id the line carries is kept.

        Diagnostics and a registry that never changed its ids both still work
        from ``source_id``; the alias is what rescues the case where it did.
        """
        subject = {
            "key": "1291001", "title": "电影甲", "year": "2024",
            "category": "movie", "season": 0,
        }
        registry = _LiveRegistry([_line(match_score=100)])

        line = build_vod_line_index.resolve_one(registry, subject)[1]["lines"][0]

        self.assertEqual(line["source_id"], "10000000-0000-0000-0000-000000000007")
        self.assertEqual(line["source_alias"], "hongniuzy2.com")


if __name__ == "__main__":
    unittest.main()
