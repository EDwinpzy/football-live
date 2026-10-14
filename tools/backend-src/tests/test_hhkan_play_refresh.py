import concurrent.futures
import http.server
import json
import os
import pathlib
import sys
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import proxy


def payload(label):
    return {"line": label, "sources": [{"name": label, "url": "https://media.example/%s.m3u8" % label}]}


class QuietHandler(proxy.Handler):
    def log_message(self, *args):
        pass


class HhkanPlayRefreshTest(unittest.TestCase):
    def setUp(self):
        proxy._PLAY_PARSE_CACHE.clear()
        proxy._PLAY_PARSE_PENDING.clear()
        proxy._MCMS_VIDEOLIST_CACHE.clear()
        proxy._MCMS_VIDEOLIST_PENDING.clear()

    def tearDown(self):
        self.setUp()

    def request(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return json.load(response)

    def start_server(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)

    def test_http_fresh_bypasses_cached_link_and_updates_following_requests(self):
        self.start_server()
        key = (71234, 1, 0)
        proxy._play_parse_put(key, payload("old"))
        with patch.object(proxy, "_hhkan_origin_call", side_effect=lambda fn: fn()), \
                patch.object(proxy.hhkan, "get_page", return_value=payload("new")) as upstream, \
                patch.object(proxy.hhkan, "parse_play_page", side_effect=lambda p: p):
            self.assertEqual(self.request("/hhkan/play/71234/1/0")["line"], "old")
            upstream.assert_not_called()
            self.assertEqual(self.request("/hhkan/play/71234/1/0?fresh=1")["line"], "new")
            self.assertEqual(self.request("/hhkan/play/71234/1/0")["line"], "new")
            upstream.assert_called_once_with("/play/71234-1-0.html")

    def test_failed_forced_retry_does_not_leave_old_link_cached(self):
        self.start_server()
        key = (71234, 1, 0)
        proxy._play_parse_put(key, payload("old"))
        with patch.object(proxy, "_hhkan_origin_call", side_effect=lambda fn: fn()), \
                patch.object(proxy.hhkan, "get_page", side_effect=OSError("source unavailable")), \
                patch.object(proxy._HHKAN_SNAPSHOT, "detail", return_value={}), \
                patch.object(proxy, "enrich_detail_sources"):
            result = self.request("/hhkan/play/71234/1/0?fresh=1")
        self.assertIn("error", result)
        self.assertIsNone(proxy._play_parse_get(key))
        self.assertEqual(proxy._PLAY_PARSE_PENDING, {})

    def test_http_fresh_reaches_cross_site_source(self):
        self.start_server()
        with patch.object(proxy, "mcms_enriched_play", return_value=payload("fresh")) as resolve:
            result = self.request("/hhkan/play/71234/100/4?fresh=1")
        self.assertEqual(result["line"], "fresh")
        resolve.assert_called_once_with(71234, 100, 4, fresh=True)

    def test_older_inflight_parse_cannot_overwrite_new_success_or_failed_retry(self):
        for fresh_fails in (False, True):
            with self.subTest(fresh_fails=fresh_fails):
                self.setUp()
                entered, release = threading.Event(), threading.Event()
                key = (71234, 1, 0)

                def fetch(_path):
                    if threading.current_thread().name.startswith("old-parse"):
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError("test old parse was not released")
                        return payload("old")
                    if fresh_fails:
                        raise OSError("fresh source failed")
                    return payload("new")

                with patch.object(proxy, "_hhkan_origin_call", side_effect=lambda fn: fn()), \
                        patch.object(proxy.hhkan, "get_page", side_effect=fetch), \
                        patch.object(proxy.hhkan, "parse_play_page", side_effect=lambda p: p), \
                        concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-parse") as executor:
                    old = executor.submit(proxy._hhkan_native_play, *key)
                    try:
                        self.assertTrue(entered.wait(2))
                        if fresh_fails:
                            with self.assertRaises(OSError):
                                proxy._hhkan_native_play(*key, fresh=True)
                        else:
                            self.assertEqual(proxy._hhkan_native_play(*key, fresh=True)["line"], "new")
                    finally:
                        release.set()
                    self.assertEqual(old.result(timeout=2)["line"], "old")
                current = proxy._play_parse_get(key)
                self.assertEqual(current["line"] if current else None, None if fresh_fails else "new")
                self.assertEqual(proxy._PLAY_PARSE_PENDING, {})

    def test_cross_site_videolist_cache_is_refreshed_only_when_requested(self):
        with patch.object(proxy, "_mcms_fetch_site", side_effect=[{"list": [{"id": 3, "url": "old"}]},
                                                                  {"list": [{"id": 3, "url": "new"}]}]) as fetch:
            self.assertEqual(proxy._mcms_videolist_item(0, 3)["url"], "old")
            self.assertEqual(proxy._mcms_videolist_item(0, 3)["url"], "old")
            self.assertEqual(proxy._mcms_videolist_item(0, 3, fresh=True)["url"], "new")
            self.assertEqual(proxy._mcms_videolist_item(0, 3)["url"], "new")
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(proxy._MCMS_VIDEOLIST_PENDING, {})

    def test_refresh_detail_bypasses_ranked_and_page_cache_and_disables_http_cache(self):
        self.start_server()
        vid = 71234
        cache_key = "/hhkan/detail/%d?refresh=1&enrich=1" % vid
        stale = {"id": vid, "title": "旧剧集详情", "sources": []}
        proxy._HHKAN_PAGE_CACHE[cache_key] = (time.time(), stale)
        self.addCleanup(proxy._HHKAN_PAGE_CACHE.pop, cache_key, None)
        episodes = [{"ep": "第%d集" % index, "pid": 9, "vid": index - 1}
                    for index in range(1, 22)]
        refresh_values = []

        def enrich(detail, requested_vid, refresh=False):
            refresh_values.append(refresh)
            detail["sources"] = [{"name": "更新线路", "episodes": episodes}]

        with patch.object(proxy, "_detail_ranked_get", return_value=stale) as ranked_get, \
                patch.object(proxy, "_hhkan_origin_call", side_effect=lambda producer: producer()), \
                patch.object(proxy.hhkan, "get_page", return_value="fresh detail") as upstream, \
                patch.object(proxy.hhkan, "parse_detail", return_value={
                    "id": vid, "title": "正在更新的剧", "sources": []}), \
                patch.object(proxy, "enrich_detail_sources", side_effect=enrich):
            with urllib.request.urlopen(self.base + cache_key, timeout=3) as response:
                payload = json.load(response)
                cache_control = response.headers.get("Cache-Control")
                cache_status = response.headers.get("X-Cache")

        ranked_get.assert_not_called()
        upstream.assert_called_once_with("/detail/%d.html" % vid)
        self.assertEqual(refresh_values, [True])
        self.assertEqual(len(payload["sources"][0]["episodes"]), 21)
        self.assertEqual(cache_control, "no-store")
        self.assertEqual(cache_status, "MISS")

    def test_detail_enrichment_forwards_refresh_to_search_and_videolist_cache(self):
        vid = 71235
        eps = [{"ep": "第%d集" % index} for index in range(1, 22)]
        ranked_group = {"name": "更新线路", "probe_h": 720, "pid": 100, "eps": eps}
        detail = {"id": vid, "title": "正在更新的剧", "sources": []}
        with patch.dict(os.environ, {"OTV_ENRICH": "1"}), \
                patch.object(proxy, "_probe_native_sources"), \
                patch.object(proxy, "_enrich_find_sites", return_value={0: 991}) as find_sites, \
                patch.object(proxy, "_mcms_videolist_item", return_value={"id": 991}) as videolist, \
                patch.object(proxy, "_mcms_groups", return_value=[{}]), \
                patch.object(proxy, "_rank_mcms_groups", return_value=([ranked_group], 0)), \
                patch.object(proxy, "_detail_ranked_put"):
            proxy.enrich_detail_sources(detail, vid, refresh=True)

        find_sites.assert_called_once_with("正在更新的剧", fresh=True)
        videolist.assert_called_once_with(0, 991, timeout=3.5, fresh=True)
        self.assertEqual(len(detail["sources"][-1]["episodes"]), 21)

    def test_fresh_enrichment_search_bypasses_cached_site_match(self):
        title = "实时更新剧"
        proxy._enrich_search_cache[title] = (time.time(), {0: 111})
        self.addCleanup(proxy._enrich_search_cache.pop, title, None)
        with patch.object(proxy, "MCMS_SITES", [proxy.MCMS_SITES[0]]), \
                patch.object(proxy, "_mcms_fetch_site", return_value={"list": [
                    {"vod_name": title, "vod_id": 222}
                ]}) as fetch:
            result = proxy._enrich_find_sites(title, fresh=True)

        self.assertEqual(result, {0: 222})
        fetch.assert_called_once()

    def test_cross_site_old_inflight_list_cannot_replace_fresh_signed_link(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(*_args, **_kwargs):
            if threading.current_thread().name.startswith("old-list"):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test old list was not released")
                return {"list": [{"id": 3, "url": "old"}]}
            return {"list": [{"id": 3, "url": "new"}]}

        with patch.object(proxy, "_mcms_fetch_site", side_effect=fetch), \
                concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-list") as executor:
            old = executor.submit(proxy._mcms_videolist_item, 0, 3)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(proxy._mcms_videolist_item(0, 3, fresh=True)["url"], "new")
            finally:
                release.set()
            self.assertEqual(old.result(timeout=2)["url"], "old")
            self.assertEqual(proxy._mcms_videolist_item(0, 3)["url"], "new")


if __name__ == "__main__":
    unittest.main()
