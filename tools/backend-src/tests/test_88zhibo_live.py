"""88zhibo football source parsing and stream resolution contracts.

All network access is replaced with local fixtures. Stream URLs use the reserved
``example`` domain and contain no signed query parameters or upstream tokens.
"""
import base64
import binascii
import json
import pathlib
import sys
import unittest
from datetime import datetime
from unittest import mock


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


def _source_response(links):
    """Build the static prefix/suffix wrapper used by the source endpoint."""
    packed = json.dumps({"links": links}, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(packed).decode("ascii").rstrip("=")
    return json.dumps({"data": "MJZaFd" + encoded + "xy"})


class ZhiBo88LiveTest(unittest.TestCase):
    def test_schedule_proxy_flag_is_scoped_to_football_request(self):
        handler = object.__new__(proxy.Handler)
        observed = []
        def receive(parsed):
            observed.append((parsed.path, proxy._active_upstream_proxy()))
        with mock.patch.object(proxy.Handler, "_do_get_with_upstream_proxy", side_effect=receive):
            for path in ("/api/matches?fresh=1&football_vpn=1", "/vod/home?football_vpn=1", "/api/matches?fresh=1"):
                handler.path = path
                handler.do_GET()
        self.assertEqual([
            ("/api/matches", proxy.FOOTBALL_PROXY_URL),
            ("/vod/home", None),
            ("/api/matches", None),
        ], observed)
        self.assertIsNone(proxy._active_upstream_proxy())

    def test_schedule_fetch_uses_football_proxy_when_requested(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = "足球赛程".encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        token = proxy._UPSTREAM_PROXY_URL.set(proxy.FOOTBALL_PROXY_URL)
        try:
            with mock.patch.object(proxy, "_url_opener", return_value=opener) as factory, \
                    mock.patch.object(proxy.urlreq, "urlopen") as direct:
                self.assertEqual("足球赛程", proxy.fetch_source())
                factory.assert_called_once_with(proxy.FOOTBALL_PROXY_URL)
                opener.open.assert_called_once()
                direct.assert_not_called()
        finally:
            proxy._UPSTREAM_PROXY_URL.reset(token)

    def test_parse_filters_non_football_keeps_four_routes_in_order_with_nested_li(self):
        page = """
        <li class="live-list-group-time">2026-10-11</li>
        <li class="group-game-item" data-id="880101">
          <span class="game-type">NBA</span>
          <span class="team-name">篮球主队</span><span class="team-name">篮球客队</span>
          <span class="category-game-time">19:00</span>
          <a href="/live/880101/play">直播</a>
        </li>
        <li class="group-game-item" data-id="880102">
          <span class="game-type">英超</span>
          <ul><li class="home"><span class="team-name">主队</span></li></ul>
          <span class="team-name">客队</span>
          <span class="category-game-time">20:00</span>
          <a href="/live/880102/play">直播</a>
        </li>
        """

        with mock.patch.object(proxy, "_now_cn", return_value=datetime(2026, 10, 11, 19, 30)):
            matches = proxy.parse_matches(page)

        self.assertEqual(["880102"], [item["match_id"] for item in matches])
        self.assertEqual(
            ["a2", "migu", "q3", "c4"],
            [channel["src"] for channel in matches[0]["channels"]],
        )
        self.assertEqual(
            ["线路A2", "线路migu", "线路Q3", "线路C4"],
            [channel["name"] for channel in matches[0]["channels"]],
        )

    def test_source_labels_keep_postponed_and_cancelled_matches_out_of_clock_projection(self):
        page = '''
        <li class="live-list-group-time">2026-10-11</li>
        <li class="group-game-item" data-id="880201">
          <span class="game-type">英超</span><span class="team-name">甲队</span><span class="team-name">乙队</span>
          <span class="category-game-time">20:00</span><a href="/live/880201/play">延期</a>
        </li>
        <li class="group-game-item" data-id="880202">
          <span class="game-type">英超</span><span class="team-name">丙队</span><span class="team-name">丁队</span>
          <span class="category-game-time">20:00</span><a href="/live/880202/play">待定</a>
        </li>
        <li class="group-game-item" data-id="880203">
          <span class="game-type">英超</span><span class="team-name">戊队</span><span class="team-name">己队</span>
          <span class="category-game-time">20:00</span><a href="/live/880203/play">已取消</a>
        </li>
        <li class="group-game-item" data-id="880204">
          <span class="game-type">英超</span><span class="team-name">庚队</span><span class="team-name">辛队</span>
          <span class="category-game-time">20:00</span><a href="/live/880204/play">直播中</a>
        </li>
        '''

        with mock.patch.object(proxy, "_now_cn", return_value=datetime(2026, 10, 11, 22, 30)):
            matches = proxy.parse_matches(page)

        by_id = {item["match_id"]: item for item in matches}
        self.assertEqual("postponed", by_id["880201"]["status"])
        self.assertEqual("pending", by_id["880202"]["status"])
        self.assertEqual("cancelled", by_id["880203"]["status"])
        self.assertEqual("finished", by_id["880204"]["status"])

    def test_7m_void_state_is_authoritative_and_mapped_to_a_visible_hold_status(self):
        match = {
            "home": "甲队", "away": "乙队", "date": "10-11", "date_full": "2026-10-11",
            "status": "live", "minute": "85",
        }
        state_table = {
            ("甲队", "乙队"): {"state": 13, "hs": "", "as": "", "start": "2026,10,11,20,0,0"},
        }
        with mock.patch.object(proxy, "_fetch_7m_scores", return_value=state_table):
            proxy.merge_7m_scores({"matches": [match]})

        self.assertEqual("postponed", match["status"])
        self.assertTrue(match["_status_authoritative"])

    def test_source_wrapper_decodes_and_rejects_malformed_data(self):
        links = [{"name": "线路A2", "url": "https://media.example/live/a2.m3u8"}]
        self.assertEqual(links, proxy._decode_88zhibo_source_links(_source_response(links)))

        with self.assertRaises(ValueError):
            proxy._decode_88zhibo_source_links(json.dumps({"data": ""}))
        with self.assertRaises((ValueError, binascii.Error)):
            proxy._decode_88zhibo_source_links(json.dumps({"data": "MJZaFd%%%xy"}))

    def test_direct_resolver_selects_dynamic_lines_and_keeps_embed_referer(self):
        embeds = {
            "a2": "https://play.88player.top/m3u8.html?url=https%3A%2F%2Fmedia.example%2Flive%2Fa2.m3u8",
            "migu": "https://play.88player.top/m3u8.html?url=https%3A%2F%2Fmedia.example%2Flive%2Fmigu12.m3u8",
            "q3": "https://play.88player.top/m3u8.html?url=https%3A%2F%2Fmedia.example%2Flive%2Fq3.m3u8",
            "c4": "https://play.88player.top/m3u8.html?url=https%3A%2F%2Fmedia.example%2Flive%2Fc4.m3u8",
        }
        links = [
            {"name": "线路A2", "url": embeds["a2"]},
            {"name": "线路migu", "url": embeds["migu"]},
            {"name": "线路Q3", "url": embeds["q3"]},
            {"name": "线路C4", "url": embeds["c4"]},
        ]
        calls = []

        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            return source_body

        expected = {
            "88z_0": ("https://media.example/live/a2.m3u8", embeds["a2"], "线路A2"),
            "88z_1": ("https://media.example/live/migu12.m3u8", embeds["migu"], "线路migu"),
            "88z_2": ("https://media.example/live/q3.m3u8", embeds["q3"], "线路Q3"),
            "88z_3": ("https://media.example/live/c4.m3u8", embeds["c4"], "线路C4"),
        }
        with mock.patch.object(proxy, "fetch_url", side_effect=fake_fetch):
            actual = {
                src: proxy._resolve_88zhibo_direct("880103", src, source_links=links)
                for src in expected
            }

        self.assertEqual(expected, actual)
        self.assertEqual(0, len(calls))  # source_links avoid refetching one list per line

    def test_generic_embed_parser_extracts_static_hls_from_external_script(self):
        page_url = "https://player.example/watch/fixture"
        script_url = "https://player.example/assets/player.js"

        def fake_fetch(url, referer, **kwargs):
            if url == page_url:
                return '<script src="/assets/player.js"></script>', page_url
            if url == script_url:
                return 'const file = "https://media.example/live/english.m3u8?token=fixture";', script_url
            raise AssertionError("unexpected URL: %s" % url)

        with mock.patch.object(proxy, "_88z_fetch_public_text", side_effect=fake_fetch):
            stream_url, embed_referer = proxy._88z_generic_embed_hls_url(page_url)

        self.assertEqual("https://media.example/live/english.m3u8?token=fixture", stream_url)
        self.assertEqual(page_url, embed_referer)

    def test_src_all_returns_every_probed_hls_route_and_hides_dead_routes(self):
        match_id = "880107"
        links = [
            {"name": "线路Q3", "url": "https://media.example/live/q3.m3u8"},
            {"name": "English-USA (HD)", "url": "https://media.example/live/usa.m3u8"},
            {"name": "线路A2", "url": "https://media.example/live/a2.m3u8"},
            {"name": "线路C4", "url": "https://media.example/live/dead-c4.m3u8"},
            {"name": "线路A2", "url": "https://media.example/live/a2-alt.m3u8"},
        ]
        cache_keys = ["88z_%d-%s" % (index, match_id) for index in range(len(links))]
        previous = {key: proxy.STREAM_CACHE.pop(key, None) for key in cache_keys}
        try:
            with mock.patch.object(proxy, "_88z_source_links", return_value=links), \
                    mock.patch.object(proxy, "_probe_88z_hls_playable", side_effect=lambda url, _ref: "dead" not in url) as probe:
                payload, cached = proxy.resolve_88zhibo_streams(match_id, fresh=True)

            self.assertFalse(cached)
            self.assertEqual(
                ["线路A2", "线路A2 (2)", "线路Q3", "English-USA (HD)"],
                [source["name"] for source in payload["sources"]],
            )
            self.assertEqual("88z_2", payload["source"])
            self.assertEqual("https://media.example/live/a2.m3u8", payload["url"])
            self.assertNotIn("dead", " ".join(source["url"] for source in payload["sources"]))
            self.assertEqual(len(links), probe.call_count)
        finally:
            for key in cache_keys:
                proxy.STREAM_CACHE.pop(key, None)
                if previous[key] is not None:
                    proxy.STREAM_CACHE[key] = previous[key]

    def test_independent_embed_routes_match_exact_labels_then_fail_closed_without_hls(self):
        page_url = "https://embed.st/embed/admin/ppv-example-match/1"
        loader_url = "https://capitalhospitals.org/1/opaque-local-fixture"
        page = '<script data-cfasync="false" src="%s"></script>' % loader_url
        labels = [
            "English-ESPN+ (HD)", "English-Paramount+ (HD)", "English-USA (HD)",
            "French-Ligue 1+ (HD)", "English-Sky Sports Premier League (HD)",
        ]
        for index, label in enumerate(labels):
            with self.subTest(label=label):
                links = [
                    {"name": label + " alternate", "url": "https://unrelated.example/alternate"},
                    {"name": label, "url": page_url},
                ]

                def fake_fetch(url, **kwargs):
                    if url == page_url:
                        return page
                    raise AssertionError("unexpected URL: %s" % url)

                with mock.patch.object(proxy, "fetch_url", side_effect=fake_fetch), \
                        mock.patch.object(
                            proxy, "_88z_fetch_embed_script",
                            side_effect=ValueError("embed 播放脚本请求失败 HTTP 403"),
                        ) as fetch_script:
                    with self.assertRaisesRegex(ValueError, "HTTP 403，无法静态解析 HLS"):
                        proxy._resolve_88zhibo_direct("880106", "88z_1", source_links=links)

                fetch_script.assert_called_once_with(loader_url, page_url)

    def test_independent_embed_route_without_static_hls_never_returns_page_url_as_stream(self):
        label = "English-ESPN+ (HD)"
        page_url = "https://embed.st/embed/admin/ppv-example-match/1"
        source_body = _source_response([{"name": label, "url": page_url}])
        page = "<html><body>no static stream URL</body></html>"

        def fake_fetch(url, **kwargs):
            return source_body if url.endswith("/source") else page

        with mock.patch.object(proxy, "fetch_url", side_effect=fake_fetch):
            with self.assertRaisesRegex(ValueError, "没有可静态解析的 HLS"):
                proxy._resolve_88zhibo_direct("880106", "88z_0")

    def test_stream_allowlist_rejects_legacy_sources_without_resolving(self):
        with mock.patch.object(proxy, "_resolve_88zhibo_direct") as resolve:
            for src in ("bb", "plu", "qqlive100", "espn_plus"):
                with self.subTest(src=src):
                    payload, cached = proxy.resolve_88zhibo_stream("880104", src)
                    self.assertFalse(cached)
                    self.assertIsNone(payload["url"])
                    self.assertEqual("unsupported_source", payload["error"])
            resolve.assert_not_called()

    def test_success_is_cached_and_fresh_bypasses_cache_with_referer_probe(self):
        cache_key = "migu-880105"
        previous = proxy.STREAM_CACHE.pop(cache_key, None)
        stream_url = "https://media.example/live/cache-contract.m3u8"
        embed_referer = "https://play.88player.top/m3u8.html?url=https%3A%2F%2Fmedia.example%2Flive%2Fcache-contract.m3u8"
        try:
            with mock.patch.object(proxy, "_resolve_88zhibo_direct", return_value=(stream_url, embed_referer, "线路migu")) as resolve, \
                    mock.patch.object(proxy, "_probe_88z_hls_playable", return_value=True) as probe:
                first, first_cached = proxy.resolve_88zhibo_stream("880105", "migu", fresh=True)
                second, second_cached = proxy.resolve_88zhibo_stream("880105", "migu")
                third, third_cached = proxy.resolve_88zhibo_stream("880105", "migu", fresh=True)

            self.assertFalse(first_cached)
            self.assertTrue(second_cached)
            self.assertFalse(third_cached)
            self.assertEqual(stream_url, first["url"])
            self.assertEqual(embed_referer, first["referer"])
            self.assertEqual("线路migu", first["name"])
            self.assertEqual(first, second)
            self.assertEqual(first, third)
            self.assertEqual(2, resolve.call_count)
            self.assertEqual(2, probe.call_count)
            probe.assert_called_with(stream_url, embed_referer)
        finally:
            proxy.STREAM_CACHE.pop(cache_key, None)
            if previous is not None:
                proxy.STREAM_CACHE[cache_key] = previous


class _ProbeResponse:
    def __init__(self, status, body, content_type="application/octet-stream"):
        self.status = status
        self.body = body
        self.headers = {"Content-Type": content_type}

    def read(self, amount=-1, decode_content=False):
        return self.body if amount is None or amount < 0 else self.body[:amount]

    def release_conn(self):
        pass


class ZhiBo88PlayableProbeTest(unittest.TestCase):
    def _run_probe(self, url, responses, referer=None):
        calls = []

        def open_upstream(request_url, headers, **_kwargs):
            calls.append((request_url, headers))
            status, body, content_type = responses.get(
                request_url, (404, b"", "text/plain"),
            )
            return _ProbeResponse(status, body, content_type), request_url

        with mock.patch.object(proxy, "_blocked_internal_url", return_value=False), \
                mock.patch.object(proxy, "_relay_strategies", return_value=[{"Referer": "fixture"}]), \
                mock.patch.object(proxy, "_open_upstream", side_effect=open_upstream):
            return proxy._probe_88z_hls_playable(url, referer), calls

    def test_media_playlist_requires_recent_segment_and_preserves_referer(self):
        manifest = "https://media.example/live/index.m3u8"
        segment = "https://media.example/live/segment-2.ts"
        stream_referer = "https://play.88player.top/m3u8.html?fixture=1"
        result, calls = self._run_probe(manifest, {
            manifest: (200, b"#EXTM3U\n#EXTINF:6,\nsegment-1.ts\n#EXTINF:6,\nsegment-2.ts\n", "application/vnd.apple.mpegurl"),
            segment: (200, b"T" * 256, "video/mp2t"),
        }, stream_referer)

        self.assertTrue(result)
        self.assertEqual([manifest, segment], [call[0] for call in calls])
        self.assertTrue(all(call[1]["Referer"] == "fixture" for call in calls))

    def test_master_playlist_checks_child_manifest_and_media_segment(self):
        master = "https://media.example/live/master.m3u8"
        child = "https://media.example/live/720/index.m3u8"
        segment = "https://media.example/live/720/seg.ts"
        result, calls = self._run_probe(master, {
            master: (200, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n720/index.m3u8\n", "application/vnd.apple.mpegurl"),
            child: (200, b"#EXTM3U\n#EXTINF:6,\nseg.ts\n", "application/vnd.apple.mpegurl"),
            segment: (200, b"S" * 256, "video/mp2t"),
        })

        self.assertTrue(result)
        self.assertEqual([master, child, segment], [call[0] for call in calls])

    def test_html_error_page_and_missing_media_segment_are_not_playable(self):
        manifest = "https://media.example/live/index.m3u8"
        html_result, _ = self._run_probe(manifest, {
            manifest: (200, b"<!doctype html><html>blocked</html>", "text/html"),
        })
        dead_segment_result, _ = self._run_probe(manifest, {
            manifest: (200, b"#EXTM3U\n#EXTINF:6,\nmissing.ts\n", "application/vnd.apple.mpegurl"),
        })

        self.assertFalse(html_result)
        self.assertFalse(dead_segment_result)

    def test_encrypted_playlist_requires_a_fetchable_key(self):
        manifest = "https://media.example/live/index.m3u8"
        key = "https://media.example/live/key.bin"
        segment = "https://media.example/live/segment.ts"
        playlist = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:6,\nsegment.ts\n'
        good_result, good_calls = self._run_probe(manifest, {
            manifest: (200, playlist, "application/vnd.apple.mpegurl"),
            key: (200, b"K" * 16, "application/octet-stream"),
            segment: (200, b"S" * 256, "video/mp2t"),
        })
        missing_key_result, _ = self._run_probe(manifest, {
            manifest: (200, playlist, "application/vnd.apple.mpegurl"),
        })

        self.assertTrue(good_result)
        self.assertEqual([manifest, key, segment], [call[0] for call in good_calls])
        self.assertFalse(missing_key_result)

    def test_manifest_rewrite_preserves_88player_embed_referer(self):
        manifest_url = "https://media.example/live/master.m3u8"
        embed_referer = "https://play.88player.top/m3u8.html?url=https%3A%2F%2Fmedia.example%2Flive%2Fmaster.m3u8"
        response = mock.Mock(status=200, data=b"#EXTM3U\nchild.m3u8\n")
        response.headers = {"Content-Type": "application/vnd.apple.mpegurl"}

        with mock.patch.object(proxy, "_open_upstream", return_value=(response, manifest_url)):
            status, _content_type, body, error, is_m3u8 = proxy.relay_url(manifest_url, embed_referer)

        self.assertEqual(200, status)
        self.assertIsNone(error)
        self.assertTrue(is_m3u8)
        rewritten = body.decode("utf-8").splitlines()[-1]
        params = proxy.parse_qs(proxy.urlsplit(rewritten).query)
        self.assertEqual(embed_referer, params["ref"][0])
        self.assertEqual("https://media.example/live/child.m3u8", params["u"][0])


if __name__ == "__main__":
    unittest.main()
