import pathlib
import sys
import unittest
from unittest.mock import patch


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class HhkanSourceEnrichmentTest(unittest.TestCase):
    def test_cross_site_groups_are_added_and_ranked_for_playback(self):
        detail = {
            "id": 374577,
            "title": "西游记",
            "sources": [{"name": "蓝光1", "episodes": [{"pid": 1, "vid": 0}]}],
        }
        groups = [
            {"name": "bfzy·1080p", "pid": 100, "probe_h": 1080,
             "eps": [{"ep": "第1集", "url": "https://media.example/1080.m3u8"}]},
            {"name": "bfzy·480p", "pid": 101, "probe_h": 480,
             "eps": [{"ep": "第1集", "url": "https://media.example/480.m3u8"}]},
        ]
        with patch.object(proxy, "_probe_native_sources"), \
                patch.object(proxy, "_enrich_find_sites", return_value={0: 9001}), \
                patch.object(proxy, "_mcms_videolist_item", return_value={"id": 9001}), \
                patch.object(proxy, "_mcms_groups", return_value=[{"name": "1080p"}, {"name": "480p"}]), \
                patch.object(proxy, "_rank_mcms_groups", return_value=(groups, None)), \
                patch.object(proxy, "_detail_ranked_put"):
            proxy.enrich_detail_sources(detail, 374577)

        self.assertEqual([source["name"] for source in detail["sources"]],
                         ["蓝光1", "bfzy·1080p", "bfzy·480p"])
        self.assertEqual(detail["sources"][1]["episodes"][0],
                         {"ep": "第1集", "pid": 100, "vid": 0})
        self.assertEqual(detail["sources"][2]["episodes"][0]["pid"], 101)


if __name__ == "__main__":
    unittest.main()
