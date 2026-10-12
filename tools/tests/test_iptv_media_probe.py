import ast
import pathlib
import re
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch


class IptvMediaProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = pathlib.Path(__file__).resolve().parents[1] / 'admin_server.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        names = {'_iptv_media_payload', '_open_iptv_probe_response', '_probe_iptv_line'}
        cls.ns = {'time': time, 'urllib': urllib, '_PROBE_UA': {},
                  '_assert_public_media_url': lambda u: None,
                  'runtime_client': types.SimpleNamespace(NoRedirect=urllib.request.HTTPRedirectHandler)}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        nodes.extend(n for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'IPTV_PLAYLIST_MAX_BYTES'
            for target in n.targets
        ))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), cls.ns)

    def probe(self, responses, redirects=None, public_url_check=None, requested=None):
        redirects = redirects or {}
        class Response:
            status = 200
            def __init__(self, body): self.body, self.headers = body, {}
            def read(self, limit): return self.body[:limit]
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Opener:
            def open(self, req, timeout):
                if requested is not None:
                    requested.append(req.full_url)
                if req.full_url in redirects:
                    raise urllib.error.HTTPError(
                        req.full_url, 302, 'Found', {'Location': redirects[req.full_url]}, None,
                    )
                return Response(responses[req.full_url])
        with patch.object(urllib.request, 'build_opener', return_value=Opener()), \
                patch.dict(self.ns, {'_assert_public_media_url': public_url_check or (lambda u: None)}):
            return self.ns['_probe_iptv_line']('https://example.test/live.m3u8')

    def test_http200_html_is_not_live(self):
        self.assertIsNone(self.probe({'https://example.test/live.m3u8': b'<html>unavailable</html>' * 30}))

    def test_master_media_and_actual_ts(self):
        ts = bytes([0x47]) + bytes(187)
        self.assertIsNotNone(self.probe({
            'https://example.test/live.m3u8': b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=10\nmedia.m3u8\n',
            'https://example.test/media.m3u8': b'#EXTM3U\n#EXTINF:5\nfirst.ts\n',
            'https://example.test/first.ts': ts * 4,
        }))

    def test_html_segment_and_empty_manifest_are_rejected(self):
        self.assertIsNone(self.probe({'https://example.test/live.m3u8': b'#EXTM3U\n'}))
        self.assertIsNone(self.probe({'https://example.test/live.m3u8': b'#EXTM3U\nfirst.ts\n',
                                     'https://example.test/first.ts': b'<html>error</html>' * 100}))

    def test_unrecognizable_payload_and_unbounded_chain_are_rejected(self):
        self.assertFalse(self.ns['_iptv_media_payload'](bytes(8192), 'video/mp2t'))
        self.assertIsNone(self.ns['_probe_iptv_line']('https://example.test/x', _depth=3))

    def test_long_vod_playlist_endlist_after_old_buffer_is_rejected(self):
        manifest = ('#EXTM3U\n' + ''.join(
            f'#EXTINF:5,\nsegment{i:04}.ts\n' for i in range(500)
        ) + '#EXT-X-ENDLIST\n').encode()
        self.assertGreater(manifest.find(b'#EXT-X-ENDLIST'), 4096)
        self.assertLessEqual(len(manifest), self.ns['IPTV_PLAYLIST_MAX_BYTES'])
        ts = bytes([0x47]) + bytes(187)
        self.assertIsNone(self.probe({
            'https://example.test/live.m3u8': manifest,
            'https://example.test/segment0000.ts': ts * 4,
        }))

    def test_playlist_over_safe_limit_is_rejected_before_fetching_a_segment(self):
        cap = self.ns['IPTV_PLAYLIST_MAX_BYTES']
        manifest = ('#EXTM3U\n#EXTINF:5,\nfirst.ts\n' + '#COMMENT\n' * cap + '#EXT-X-ENDLIST\n').encode()
        requested = []
        self.assertIsNone(self.probe(
            {'https://example.test/live.m3u8': manifest}, requested=requested,
        ))
        self.assertEqual(requested, ['https://example.test/live.m3u8'])

    def test_segment_redirect_is_followed_after_public_url_validation(self):
        segment = 'https://example.test/first.ts'
        target = 'https://cdn.example.test/first.ts'
        ts = (bytes([0x47]) + bytes(187)) * 4
        checked = []
        result = self.probe(
            {'https://example.test/live.m3u8': b'#EXTM3U\n#EXTINF:5,\nfirst.ts\n', target: ts},
            redirects={segment: target}, public_url_check=checked.append,
        )
        self.assertIsNotNone(result)
        self.assertIn(target, checked)

    def test_segment_redirect_to_private_ip_is_rejected(self):
        segment = 'https://example.test/first.ts'
        target = 'https://127.0.0.1/first.ts'
        checked = []

        def require_public(url):
            checked.append(url)
            if urllib.parse.urlsplit(url).hostname == '127.0.0.1':
                raise ValueError('not public')

        requested = []
        self.assertIsNone(self.probe(
            {'https://example.test/live.m3u8': b'#EXTM3U\n#EXTINF:5,\nfirst.ts\n'},
            redirects={segment: target}, public_url_check=require_public, requested=requested,
        ))
        self.assertIn(target, checked)
        self.assertNotIn(target, requested)

    def test_segment_redirect_rejects_https_downgrade_and_more_than_two_hops(self):
        segment = 'https://example.test/first.ts'
        manifest = {'https://example.test/live.m3u8': b'#EXTM3U\n#EXTINF:5,\nfirst.ts\n'}
        self.assertIsNone(self.probe(manifest, redirects={segment: 'http://cdn.example.test/first.ts'}))

        first = 'https://cdn1.example.test/first.ts'
        second = 'https://cdn2.example.test/first.ts'
        third = 'https://cdn3.example.test/first.ts'
        requested = []
        self.assertIsNone(self.probe(
            manifest,
            redirects={segment: first, first: second, second: third},
            requested=requested,
        ))
        self.assertNotIn(third, requested)


if __name__ == '__main__': unittest.main()
