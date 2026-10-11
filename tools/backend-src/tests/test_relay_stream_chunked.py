# -*- coding: utf-8 -*-
"""relay_stream 非 Range 媒体 chunked 下发行契约（2026-09-19）。

背景：足球直播「看一会就看不了、点重试/换源都没用」——个别链路（中间盒/代理/
CDN 边缘）会上游「声明 Content-Length=X、干净收尾却只交付 X-约10 字节」。
旧版 relay_stream 透传该 CL，客户端（OkHttp/fetch）body 结束时判缺字节硬报
unexpected end of stream → ERROR_CODE_IO_NETWORK_CONNECTION_FAILED，重试重抓
同一上游必复发（无限循环）。修复：非 Range 媒体一律 chunked 分帧，0-chunk 收尾
即完整；上游真异常仍掐断连接走既有重试链。
"""
import http.client
import pathlib
import urllib.parse
import socket
import sys
import threading
import time
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class _FakeUpstream(threading.Thread):
    """单发假上游：按脚本响应一次 HTTP 请求。

    mode="chunked-lie"：响应头同时带 Content-Length=N（虚高）与 chunked，
        实际 chunked 体只给 N-9 字节后干净收尾——复现中间盒短交付形态。
    mode="plain-short"：声明 CL=N，给 N-1000 字节后直接断连（上游真异常）。
    mode="plain-full"：声明 CL=N，足量交付（健康基线）。
    """

    def __init__(self, body_len, mode):
        super().__init__(daemon=True)
        self.body_len = body_len
        self.mode = mode
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def run(self):
        conn, _ = self.sock.accept()
        try:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:  # 读完请求头
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
            body = bytes((i % 251 for i in range(self.body_len)))
            if self.mode == "chunked-lie":
                head = (
                    "HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n"
                    "Content-Length: %d\r\nTransfer-Encoding: chunked\r\n\r\n"
                    % (self.body_len + 9)  # 虚高 9 字节
                ).encode()
                conn.sendall(head)
                short = body  # 实际只给 body_len（= 声明值 - 9）
                conn.sendall(("%x\r\n" % len(short)).encode() + short + b"\r\n0\r\n\r\n")
            elif self.mode == "plain-short":
                head = (
                    "HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n"
                    "Content-Length: %d\r\n\r\n" % self.body_len
                ).encode()
                conn.sendall(head + body[: max(0, self.body_len - 1000)])
            else:  # plain-full
                head = (
                    "HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n"
                    "Content-Length: %d\r\n\r\n" % self.body_len
                ).encode()
                conn.sendall(head + body)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            self.sock.close()


class _FakePlaylistUpstream(threading.Thread):
    """One-shot HLS playlist server for Range-on-playlist client behavior."""

    def __init__(self, body):
        super().__init__(daemon=True)
        self.body = body
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def run(self):
        conn, _ = self.sock.accept()
        try:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
            head = (
                "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\n"
                "Content-Length: %d\r\n\r\n" % len(self.body)
            ).encode()
            conn.sendall(head + self.body)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            self.sock.close()


class _RelayServer:
    """起真实 Handler 的中继服务（随机端口），测试结束关闭。"""

    def __init__(self):
        from http.server import ThreadingHTTPServer

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RelayStreamChunkedTest(unittest.TestCase):
    def setUp(self):
        proxy.RELAY_CACHE.clear()
        # 测试上游就是 127.0.0.1 —— 放开 SSRF 内网拦截（真实代码路径不变）
        self._old_blocked = proxy._blocked_internal_url
        proxy._blocked_internal_url = lambda _u: False
        self.relay = _RelayServer()

    def tearDown(self):
        self.relay.stop()
        proxy._blocked_internal_url = self._old_blocked
        proxy.RELAY_CACHE.clear()

    def _fetch(self, path, version="1.1", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.relay.port, timeout=15)
        conn._http_vsn = 11 if version == "1.1" else 10
        conn._http_vsn_str = "HTTP/1.1" if version == "1.1" else "HTTP/1.0"
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return conn, resp

    def test_non_range_is_chunked_and_short_delivery_is_clean(self):
        """chunked 虚报 CL 的上游：中继改以 chunked 下发行，短交付按完整收尾。"""
        up = _FakeUpstream(300_000, "chunked-lie")
        up.start()
        url = "http://127.0.0.1:%d/live/seg-1.ts?tx=1" % up.port
        conn, resp = self._fetch("/api/relay?u=" + urllib.parse.quote(url, safe=""))
        self.assertEqual(resp.status, 200)
        self.assertEqual((resp.getheader("Transfer-Encoding") or "").lower(), "chunked")
        self.assertIsNone(resp.getheader("Content-Length"))
        body = resp.read()  # chunked 解码；短交付不应抛 IncompleteRead
        self.assertEqual(len(body), 300_000)
        conn.close()
        # 虚报 CL 下交付体与 CL 不符 → 不得入缓存（防毒化）
        self.assertNotIn(url, proxy.RELAY_CACHE)

    def test_non_range_full_delivery_is_cached(self):
        """健康基线：足量交付照常转发且入短缓存。"""
        up = _FakeUpstream(200_000, "plain-full")
        up.start()
        url = "http://127.0.0.1:%d/live/seg-2.ts?tx=2" % up.port
        conn, resp = self._fetch("/api/relay?u=" + urllib.parse.quote(url, safe=""))
        self.assertEqual(resp.status, 200)
        body = resp.read()
        self.assertEqual(len(body), 200_000)
        conn.close()
        # The client can finish reading the advertised Content-Length just before the
        # handler resumes the generator and records its completed-body cache entry.
        deadline = time.monotonic() + 1.0
        while url not in proxy.RELAY_CACHE and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(url, proxy.RELAY_CACHE)

    def test_upstream_abort_still_fails_for_retry(self):
        """上游真异常（声明 CL 后断连）：不写终止块，客户端拿到网络错误去重试。"""
        up = _FakeUpstream(300_000, "plain-short")
        up.start()
        url = "http://127.0.0.1:%d/live/seg-3.ts?tx=3" % up.port
        conn, resp = self._fetch("/api/relay?u=" + urllib.parse.quote(url, safe=""))
        self.assertEqual(resp.status, 200)
        with self.assertRaises((http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionResetError)):
            resp.read()
        conn.close()
        self.assertNotIn(url, proxy.RELAY_CACHE)

    def test_http10_client_falls_back_to_close_delimited(self):
        """HTTP/1.0 客户端：退化为无 CL + 关闭分帧，依旧能干净收完短交付体。"""
        up = _FakeUpstream(120_000, "chunked-lie")
        up.start()
        url = "http://127.0.0.1:%d/live/seg-4.ts?tx=4" % up.port
        conn, resp = self._fetch(
            "/api/relay?u=" + urllib.parse.quote(url, safe=""), version="1.0"
        )
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Transfer-Encoding"))
        self.assertIsNone(resp.getheader("Content-Length"))
        body = resp.read()
        self.assertEqual(len(body), 120_000)
        conn.close()

    def test_range_request_keeps_content_length(self):
        """Range/206 语义不变：透传段长 Content-Length，不走 chunked。"""
        up = _FakeUpstream(100_000, "plain-full")
        up.start()
        url = "http://127.0.0.1:%d/vod/film.mp4?tx=5" % up.port
        conn, resp = self._fetch(
            "/api/relay?u=" + urllib.parse.quote(url, safe=""),
            headers={"Range": "bytes=0-999"},
        )
        # 假上游不实现 206（回 200 全量），断言点在中继对 Range 请求的头部策略：
        # relay_stream 收到 rng 时必须保留 Content-Length、不得标记 chunked
        self.assertIsNone(resp.getheader("Transfer-Encoding"))
        self.assertIsNotNone(resp.getheader("Content-Length"))
        resp.read()
        conn.close()

    def test_range_request_to_hls_playlist_still_returns_rewritten_manifest(self):
        """部分 HLS clients Range 请求子清单；它仍须完整重写后按 manifest 返回。"""
        playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg-1.ts?token=1\n"
        up = _FakePlaylistUpstream(playlist)
        up.start()
        url = "http://127.0.0.1:%d/live/child.m3u8" % up.port
        conn, resp = self._fetch(
            "/api/relay?u=" + urllib.parse.quote(url, safe=""),
            headers={"Range": "bytes=0-"},
        )
        self.assertEqual(resp.status, 200)
        body = resp.read()
        self.assertIn("mpegurl", (resp.getheader("Content-Type") or "").lower())
        self.assertEqual(resp.getheader("Content-Length"), str(len(body)))
        self.assertIn(b"#EXTM3U", body)
        self.assertIn(b"relay?u=", body)
        conn.close()

    def test_relay_stream_marks_chunked_only_for_chunked_upstream(self):
        """函数级契约：chunked 上游 → 下行 chunked 无 CL；普通 CL 上游 → 透传 CL；
        Range → 保留 CL 不走 chunked。"""
        # chunked 上游（虚报 CL 形态）→ 下行 chunked
        up = _FakeUpstream(64_000, "chunked-lie")
        up.start()
        url = "http://127.0.0.1:%d/live/seg-6.ts?tx=6" % up.port
        status, _ctype, extra, it, _m = proxy.relay_stream(url)
        self.assertEqual(status, 200)
        self.assertEqual((extra.get("Transfer-Encoding") or "").lower(), "chunked")
        self.assertNotIn("Content-Length", extra)
        total = sum(len(b) for b in it)
        self.assertEqual(total, 64_000)  # 实际交付体（声明值 - 9）
        self.assertNotIn(url, proxy.RELAY_CACHE)  # 与虚报 CL 不符 → 不入缓存

        # 普通 CL 上游 → 透传 CL（点播链路行为与旧版一致）
        up1 = _FakeUpstream(64_000, "plain-full")
        up1.start()
        url1 = "http://127.0.0.1:%d/vod/film.mp4?tx=8" % up1.port
        status1, _c1, extra1, it1, _m1 = proxy.relay_stream(url1)
        self.assertEqual(status1, 200)
        self.assertNotIn("Transfer-Encoding", extra1)
        self.assertEqual(extra1.get("Content-Length"), str(64_000))
        for _ in it1:
            pass
        self.assertIn(url1, proxy.RELAY_CACHE)

        # Range → 保留 CL、不走 chunked
        up2 = _FakeUpstream(64_000, "plain-full")
        up2.start()
        url2 = "http://127.0.0.1:%d/live/seg-7.ts?tx=7" % up2.port
        status2, _c2, extra2, it2, _m2 = proxy.relay_stream(url2, rng="bytes=0-99")
        self.assertNotIn("Transfer-Encoding", extra2)
        # 假上游忽略 Range 回 200+全量 CL：中继透传上游 CL（200 全量语义）
        self.assertIn("Content-Length", extra2)
        for _ in it2:
            pass


if __name__ == "__main__":
    unittest.main()
