import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import proxy


def media_payload(label):
    return {
        "line": label,
        "sources": [{"name": label, "url": "https://media.example/%s.m3u8" % label}],
    }


class HhkanEpisodeFallbackTest(unittest.TestCase):
    VOD_ID = 385934

    def original_detail(self):
        return {
            "id": self.VOD_ID,
            "title": "美人余",
            "sources": [{
                "name": "好好看",
                "episodes": [
                    {"ep": "第1集", "pid": 1, "vid": 2189566},
                    {"ep": "第10集", "pid": 1, "vid": 2189575},
                    {"ep": "第11集", "pid": 1, "vid": 2189576},
                ],
            }],
        }

    def test_cached_detail_returns_deep_copy_from_ranked_cache(self):
        detail = self.original_detail()
        with patch.dict(proxy._DETAIL_RANKED_CACHE,
                        {self.VOD_ID: (time.time(), detail)}), \
                patch.object(proxy, "_hhkan_metadata_cache") as disk_cache, \
                patch.object(proxy._HHKAN_SNAPSHOT, "detail") as snapshot_detail:
            result = proxy._hhkan_cached_detail(self.VOD_ID)

        self.assertEqual(result, detail)
        self.assertIsNot(result, detail)
        self.assertIsNot(result["sources"], detail["sources"])
        result["sources"][0]["episodes"].clear()
        self.assertEqual(len(detail["sources"][0]["episodes"]), 3)
        disk_cache.assert_not_called()
        snapshot_detail.assert_not_called()

    def test_cached_detail_accepts_stale_page_metadata(self):
        key = "/hhkan/detail/%d?enrich=1" % self.VOD_ID
        detail = self.original_detail()
        # Deliberately old: episode identity is still useful while the source is down.
        disk = Mock()
        disk.get.return_value = None
        with patch.dict(proxy._DETAIL_RANKED_CACHE, {}), \
                patch.dict(proxy._HHKAN_PAGE_CACHE, {key: (1.0, detail)}), \
                patch.object(proxy, "_hhkan_metadata_cache", return_value=disk), \
                patch.object(proxy._HHKAN_SNAPSHOT, "detail") as snapshot_detail:
            result = proxy._hhkan_cached_detail(self.VOD_ID)

        self.assertEqual(result, detail)
        self.assertIsNot(result, detail)
        snapshot_detail.assert_not_called()

    def test_cached_detail_falls_back_to_snapshot(self):
        detail = self.original_detail()
        disk = Mock()
        disk.get.return_value = None
        with patch.dict(proxy._DETAIL_RANKED_CACHE, {}), \
                patch.dict(proxy._HHKAN_PAGE_CACHE, {}), \
                patch.object(proxy, "_hhkan_metadata_cache", return_value=disk), \
                patch.object(proxy._HHKAN_SNAPSHOT, "detail", return_value=detail):
            result = proxy._hhkan_cached_detail(self.VOD_ID)

        self.assertEqual(result, detail)
        self.assertIsNot(result, detail)

    def test_episode_11_maps_by_native_vid_and_tries_every_matching_backup(self):
        detail = self.original_detail()
        first_backup = {
            "name": "备用甲",
            "episodes": [
                {"ep": "第10集", "pid": 100, "vid": 0},
                {"ep": "第11集", "pid": 100, "vid": 1},
            ],
        }
        # Same episodes in another order: the requested episode is at index zero.
        second_backup = {
            "name": "备用乙",
            "episodes": [
                {"ep": "11", "pid": 150, "vid": 0},
                {"ep": "第10集", "pid": 150, "vid": 1},
            ],
        }

        def enrich(cached, _vodid, **_kwargs):
            cached["sources"].extend([first_backup, second_backup])

        with patch.object(proxy, "_hhkan_cached_detail", return_value=detail), \
                patch.object(proxy, "enrich_detail_sources", side_effect=enrich), \
                patch.object(proxy, "mcms_enriched_play",
                             side_effect=[RuntimeError("备用甲暂不可用"), media_payload("备用乙")]) as play:
            result = proxy._hhkan_fallback_play(self.VOD_ID, 1, 2189576)

        self.assertEqual(result["line"], "备用乙")
        self.assertEqual(
            [call.args[:3] for call in play.call_args_list],
            [(self.VOD_ID, 100, 1), (self.VOD_ID, 150, 0)],
        )

    def test_unknown_native_episode_returns_empty_sources_without_guessing(self):
        detail = self.original_detail()
        with patch.object(proxy, "_hhkan_cached_detail", return_value=detail), \
                patch.object(proxy, "enrich_detail_sources") as enrich, \
                patch.object(proxy, "mcms_enriched_play") as play:
            result = proxy._hhkan_fallback_play(self.VOD_ID, 1, 99999999)

        self.assertEqual(result.get("sources"), [])
        self.assertTrue(result.get("error"))
        enrich.assert_not_called()
        play.assert_not_called()

    def test_title_uses_local_metadata_without_calling_upstream(self):
        with patch.object(proxy, "_hhkan_cached_detail", return_value=self.original_detail()), \
                patch.object(proxy, "_hhkan_origin_call") as upstream:
            title = proxy._hhkan_title_of(self.VOD_ID)

        self.assertEqual(title, "美人余")
        upstream.assert_not_called()

    def test_episode_map_persists_without_urls_and_restores_after_memory_clear(self):
        detail = self.original_detail()
        for episode in detail["sources"][0]["episodes"]:
            episode["url"] = "https://signed.example/expired.m3u8?token=secret"
        detail["sources"].append({"name": "补充线", "episodes": [
            {"ep": "第11集", "pid": 100, "vid": 10,
             "url": "https://supplement.example/should-not-persist.m3u8"},
        ]})

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"OTV_DATA_DIR": directory}), \
                patch.dict(proxy._DETAIL_RANKED_CACHE, {}), \
                patch.dict(proxy._HHKAN_PAGE_CACHE, {}), \
                patch.object(proxy._HHKAN_SNAPSHOT, "detail", return_value={}):
            proxy._hhkan_remember_detail(detail)
            persisted_file = next((pathlib.Path(directory) / "hhkan_metadata").glob("*.json"))
            persisted_text = persisted_file.read_text(encoding="utf-8")
            stored_value = json.loads(persisted_text)["value"]

            # Both process-local caches are empty here, so recovery must use disk.
            restored = proxy._hhkan_cached_detail(self.VOD_ID)

        self.assertNotIn("url", persisted_text)
        self.assertNotIn("secret", persisted_text)
        self.assertEqual(restored["title"], "美人余")
        self.assertEqual(restored["sources"][0]["episodes"][-1]["vid"], 2189576)
        self.assertTrue(all(set(ep) == {"ep", "pid", "vid"}
                            for source in stored_value["sources"]
                            for ep in source["episodes"]))
        self.assertEqual(len(stored_value["sources"]), 1)

    def test_persistent_episode_metadata_retains_at_most_200_entries(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"OTV_DATA_DIR": directory}):
            cache = proxy._hhkan_metadata_cache()
            for index in range(202):
                key = str(40000 + index)
                cache.put(key, {"id": int(key), "title": "旧剧", "sources": []})
                timestamp = 1_000_000 + index
                os.utime(cache._path(key), (timestamp, timestamp))

            proxy._hhkan_remember_detail(self.original_detail())
            files = list(cache.directory.glob("*.json"))

        self.assertEqual(len(files), 200)
        self.assertFalse(cache._path("40000").exists())
        self.assertFalse(cache._path("40001").exists())
        self.assertFalse(cache._path("40002").exists())

    def test_empty_backup_sources_continues_to_next_matching_backup(self):
        detail = self.original_detail()
        detail["sources"].extend([
            {"name": "空线", "episodes": [
                {"ep": "第11集", "pid": 100, "vid": 1},
            ]},
            {"name": "可用线", "episodes": [
                {"ep": "11", "pid": 150, "vid": 0},
            ]},
        ])
        with patch.object(proxy, "_hhkan_cached_detail", return_value=detail), \
                patch.object(proxy, "enrich_detail_sources"), \
                patch.object(proxy, "mcms_enriched_play",
                             side_effect=[{"line": "空线", "sources": []},
                                          media_payload("可用线")]) as play:
            result = proxy._hhkan_fallback_play(self.VOD_ID, 1, 2189576)

        self.assertEqual(result["line"], "可用线")
        self.assertEqual(play.call_count, 2)

    def test_http_play_route_preserves_selected_source_when_native_upstream_fails(self):
        handler = object.__new__(proxy.Handler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        with patch.object(proxy, "_hhkan_native_play", side_effect=OSError("source blocked")) as native, \
                patch.object(proxy, "_hhkan_fallback_play") as fallback:
            handler._hhkan_api(proxy.urlparse("/hhkan/play/%d/1/2189576" % self.VOD_ID))

        native.assert_called_once_with(self.VOD_ID, 1, 2189576, fresh=False)
        fallback.assert_not_called()
        self.assertEqual(json.loads(handler.wfile.getvalue()), {
            "line": "", "sources": [], "error": "当前所选线路暂不可达",
        })

    def test_mcms_episode_index_out_of_range_raises(self):
        item = {"vod_play_from": "主线", "vod_play_url": "第1集$https://media.example/1.m3u8"}
        with patch.object(proxy, "_hhkan_title_of", return_value="美人余"), \
                patch.object(proxy, "_enrich_find_sites", return_value={0: 8123}), \
                patch.object(proxy, "_mcms_videolist_item", return_value=item):
            with self.assertRaises(RuntimeError):
                proxy.mcms_enriched_play(self.VOD_ID, 100, 2189576)


if __name__ == "__main__":
    unittest.main()
