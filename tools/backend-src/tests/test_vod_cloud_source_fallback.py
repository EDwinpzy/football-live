"""片源兜底链：本机出口连不上片源站时，按顺序用外部来源补线路。

2026-09-24 实测：自建 VPS 出口连不上 hongniuzy2 / bfzyapi / jyzyapi / haohankan
等站点（curl 000 超时），网页版详情页因此整天显示「暂无片源」；但同一条线路的
播放地址（hn.bfvvs.com 等）在 VPS 上可取。方案演变：

* 第一版：本地匹配为空时回 CloudBase `/vod/detail/{id}?refresh=1` 取一次线路
  （`OTV_VOD_CLOUD_FALLBACK`）。能用，但 CloudBase 2026-10-09 到期。
* 第二版（当前）：本机 `tools/build_vod_line_index.py` 解析好线路生成静态索引，
  推到 VPS 对象桶；`proxy.py` 读索引（`OTV_VOD_LINE_INDEX_URL`）当主兜底，
  云端回源降级为可选最后一环，自建栈默认关闭 → 不再依赖外部服务。
"""

import pathlib
import sys
import time
import types
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402
import vod_api  # noqa: E402
import vod_sources  # noqa: E402


CLOUD_LINE = {
    "id": "hongniu:192708:1",
    "source_id": "hongniu",
    "source_name": "红牛",
    "name": "红牛·hnm3u8",
    "episodes": [{"index": 0, "name": "第01集", "url": "https://hn.bfvvs.com/play/azpXyk5d/index.m3u8"}],
}
UNKNOWN_LINE = {
    "id": "hhkan:1:9",
    "source_id": "hhkan",
    "source_name": "好好看",
    "name": "好好看",
    "episodes": [{"index": 0, "name": "第01集", "url": "https://example.invalid/a.m3u8"}],
}

LOCAL_HONGNIU_ID = "10000000-0000-0000-0000-000000000007"


class _StubCatalog:
    def detail(self, douban_id, force_refresh=False):
        return {"id": "douban:" + str(douban_id), "douban_id": str(douban_id),
                "title": "早春晴朗", "year": "2026", "category": "tv"}

    def refresh_detail(self, douban_id):
        return self.detail(douban_id, force_refresh=True)


def _registry(*pairs):
    """pairs 是 (id, name)：自建后台的影视源 id 是数据库 UUID，名字才是对应关系。"""
    sources = [vod_sources.VodSource(id=item[0], name=item[1], site_url="https://example.invalid",
                                     api_url="https://example.invalid/api.php/provide/vod/",
                                     adapter_type="macms_json") for item in pairs]
    return vod_sources.SourceRegistry(sources, version=1)


class VodCloudSourceFallbackTest(unittest.TestCase):
    def test_local_sources_win_and_cloud_is_not_called(self):
        calls = []

        def cloud(subject, douban_id):
            calls.append(str(douban_id))
            return [CLOUD_LINE]

        registry = _registry((LOCAL_HONGNIU_ID, "红牛"))
        local_line = dict(CLOUD_LINE, id="local:1:1", source_id=LOCAL_HONGNIU_ID)
        service = vod_api.VodService(
            _StubCatalog(), registry,
            resolve_sources=lambda subject: [local_line], fallback_sources=[cloud])
        payload = service.detail("36660844")
        self.assertEqual([line["id"] for line in payload["sources"]], ["local:1:1"])
        self.assertEqual(payload["source_state"], "ready")
        self.assertEqual(calls, [])

    def test_cloud_sources_rescue_empty_local_matching(self):
        seen = []

        def cloud(subject, douban_id):
            seen.append((subject.get("title"), str(douban_id)))
            return [dict(CLOUD_LINE)]

        registry = _registry((LOCAL_HONGNIU_ID, "红牛"))
        service = vod_api.VodService(
            _StubCatalog(), registry,
            resolve_sources=lambda subject: [], fallback_sources=[cloud])
        payload = service.detail("36660844")
        self.assertEqual(payload["source_state"], "ready")
        self.assertEqual([line["id"] for line in payload["sources"]], ["hongniu:192708:1"])
        # 云端 slug 必须按源名落到本机 UUID，否则 /vod/play 会「影视源不存在」
        self.assertEqual(payload["sources"][0]["source_id"], LOCAL_HONGNIU_ID)
        self.assertEqual(payload["sources"][0]["source_name"], "红牛")
        self.assertEqual(seen, [("早春晴朗", "36660844")])
        # 兜底线路必须进入 _resolved_lines，/vod/play 才能解析
        self.assertIn("hongniu:192708:1", service._resolved_lines["36660844"])
        played = service.play("36660844", "hongniu:192708:1", 0)
        self.assertEqual(
            played["url"], "https://hn.bfvvs.com/play/azpXyk5d/index.m3u8")

    def test_cloud_line_with_unknown_source_is_filtered(self):
        registry = _registry((LOCAL_HONGNIU_ID, "红牛"))
        service = vod_api.VodService(
            _StubCatalog(), registry,
            resolve_sources=lambda subject: [],
            fallback_sources=[lambda subject, douban_id: [dict(UNKNOWN_LINE), dict(CLOUD_LINE)]])
        payload = service.detail("36660844")
        self.assertEqual([line["id"] for line in payload["sources"]], ["hongniu:192708:1"])

    def test_cloud_lines_kept_verbatim_when_registry_is_empty(self):
        service = vod_api.VodService(
            _StubCatalog(), vod_sources.SourceRegistry([], version=1),
            resolve_sources=lambda subject: [],
            fallback_sources=[lambda subject, douban_id: [dict(UNKNOWN_LINE)]])
        payload = service.detail("36660844")
        self.assertEqual([line["id"] for line in payload["sources"]], ["hhkan:1:9"])

    def test_cloud_failure_keeps_unavailable_instead_of_raising(self):
        def boom(subject, douban_id):
            raise RuntimeError("cloud relay disabled")

        service = vod_api.VodService(
            _StubCatalog(), _registry((LOCAL_HONGNIU_ID, "红牛")),
            resolve_sources=lambda subject: [], fallback_sources=[boom])
        payload = service.detail("36660844")
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["source_state"], "unavailable")

    def test_service_without_cloud_resolver_is_unchanged(self):
        service = vod_api.VodService(
            _StubCatalog(), _registry((LOCAL_HONGNIU_ID, "红牛")), resolve_sources=lambda subject: [])
        payload = service.detail("36660844")
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["source_state"], "unavailable")


class ProxyCloudVodSourcesTest(unittest.TestCase):
    """proxy 侧取云端线路的适配函数：只认 sources 数组，任何异常都回落空列表。"""

    def setUp(self):
        self._real_get = proxy._cloud_get_json
        self._real_enabled = proxy.CLOUD_VOD_FALLBACK_ENABLED
        self._had_vod = hasattr(proxy, "_cloud_vod_sources")
        self._real_vod = getattr(proxy, "_cloud_vod_sources", None)

    def tearDown(self):
        proxy._cloud_get_json = self._real_get
        proxy.CLOUD_VOD_FALLBACK_ENABLED = self._real_enabled
        if self._had_vod:
            proxy._cloud_vod_sources = self._real_vod
        elif hasattr(proxy, "_cloud_vod_sources"):
            delattr(proxy, "_cloud_vod_sources")

    def test_extracts_sources_from_cloud_payload(self):
        paths = []

        def fake(path, timeout=12, allow_when_relay_off=False):
            paths.append(path)
            self.assertTrue(allow_when_relay_off, "片源兜底必须显式放行，才能绕过通用中继开关")
            return {"title": "早春晴朗", "sources": [CLOUD_LINE]}

        proxy._cloud_get_json = fake
        proxy.CLOUD_VOD_FALLBACK_ENABLED = True
        result = proxy._cloud_vod_sources({"title": "早春晴朗"}, "douban:36660844")
        self.assertEqual([line["id"] for line in result], ["hongniu:192708:1"])
        self.assertEqual(paths, ["/vod/detail/36660844?refresh=1"])

    def test_fallback_disabled_skips_request(self):
        def fake(path, timeout=12, allow_when_relay_off=False):  # pragma: no cover
            raise AssertionError("兜底关闭时不得发起云端请求")

        proxy._cloud_get_json = fake
        proxy.CLOUD_VOD_FALLBACK_ENABLED = False
        self.assertEqual(proxy._cloud_vod_sources({"title": "早春晴朗"}, "36660844"), [])

    def test_cloud_error_returns_empty_list(self):
        def fake(path, timeout=12, allow_when_relay_off=False):
            raise RuntimeError("network down")

        proxy._cloud_get_json = fake
        proxy.CLOUD_VOD_FALLBACK_ENABLED = True
        self.assertEqual(proxy._cloud_vod_sources({"title": "早春晴朗"}, "36660844"), [])


INDEX_LINE = {
    "id": "hongniu:192708:1",
    "source_id": "hongniu",
    "source_name": "红牛",
    "name": "红牛·hnm3u8",
    "episodes": [{"index": 0, "name": "第01集", "url": "https://hn.bfvvs.com/play/azpXyk5d/index.m3u8"}],
}


class VodFallbackChainTest(unittest.TestCase):
    """兜底链按顺序试：第一个给出线路的 provider 生效，空/异常都继续往下。"""

    def setUp(self):
        self.registry = _registry((LOCAL_HONGNIU_ID, "红牛"))

    def _service(self, *providers):
        return vod_api.VodService(
            _StubCatalog(), self.registry,
            resolve_sources=lambda subject: [], fallback_sources=list(providers))

    def test_first_non_empty_provider_wins(self):
        calls = []

        def first(subject, douban_id):
            calls.append("first")
            return [dict(INDEX_LINE)]

        def second(subject, douban_id):
            calls.append("second")
            return [dict(INDEX_LINE, id="other:1:1")]

        payload = self._service(first, second).detail("36660844")
        self.assertEqual(calls, ["first"])
        self.assertEqual(payload["sources"][0]["source_id"], LOCAL_HONGNIU_ID)

    def test_empty_and_failing_provider_fall_through(self):
        calls = []

        def empty(subject, douban_id):
            calls.append("empty")
            return []

        def boom(subject, douban_id):
            calls.append("boom")
            raise RuntimeError("network down")

        def good(subject, douban_id):
            calls.append("good")
            return [dict(INDEX_LINE)]

        payload = self._service(empty, boom, good).detail("36660844")
        self.assertEqual(calls, ["empty", "boom", "good"])
        self.assertEqual(payload["source_state"], "ready")

    def test_no_provider_configured_keeps_unavailable(self):
        payload = self._service().detail("36660844")
        self.assertEqual(payload["source_state"], "unavailable")


class VodLineIndexTest(unittest.TestCase):
    """静态索引兜底：按豆瓣 id 命中、按标题+年份回落、过期与缺索引都回落空。"""

    def setUp(self):
        self._real_url = proxy.VOD_LINE_INDEX_URL
        self._real_payload = getattr(proxy, "_vod_index_payload", None)
        self._cache = dict(proxy._vod_index_cache)

    def tearDown(self):
        proxy.VOD_LINE_INDEX_URL = self._real_url
        if self._real_payload is not None:
            proxy._vod_index_payload = self._real_payload
        proxy._vod_index_cache.update(self._cache)

    def _payload(self, entries):
        proxy._vod_index_payload = lambda: {"schema": 1, "entries": entries}

    def test_entry_matched_by_douban_id(self):
        self._payload({"36660844": {"title": "早春晴朗", "year": "2026",
                                    "resolved_at": int(time.time()), "lines": [INDEX_LINE]}})
        proxy.VOD_LINE_INDEX_URL = "http://storage:8092/vod-lines.json"
        lines = proxy._index_vod_sources({"title": "早春晴朗", "year": "2026"}, "douban:36660844")
        self.assertEqual([line["id"] for line in lines], ["hongniu:192708:1"])

    def test_entry_matched_by_title_and_year_when_id_missing(self):
        self._payload({"99999999": {"title": "早春晴朗", "year": "2026",
                                    "resolved_at": int(time.time()), "lines": [INDEX_LINE]}})
        proxy.VOD_LINE_INDEX_URL = "http://storage:8092/vod-lines.json"
        lines = proxy._index_vod_sources({"title": "早春晴朗", "year": "2026"}, "36660844")
        self.assertEqual(len(lines), 1)

    def test_stale_entry_is_ignored(self):
        old = int(time.time()) - int((proxy.VOD_LINE_INDEX_MAX_AGE_DAYS + 1) * 86400)
        self._payload({"36660844": {"title": "早春晴朗", "year": "2026",
                                    "resolved_at": old, "lines": [INDEX_LINE]}})
        proxy.VOD_LINE_INDEX_URL = "http://storage:8092/vod-lines.json"
        self.assertEqual(proxy._index_vod_sources({"title": "早春晴朗"}, "36660844"), [])

    def test_missing_index_returns_empty(self):
        proxy.VOD_LINE_INDEX_URL = ""
        self.assertEqual(proxy._index_vod_sources({"title": "早春晴朗"}, "36660844"), [])
        proxy.VOD_LINE_INDEX_URL = "http://storage:8092/vod-lines.json"
        proxy._vod_index_payload = lambda: None
        self.assertEqual(proxy._index_vod_sources({"title": "早春晴朗"}, "36660844"), [])


class VodFallbackOrderTest(unittest.TestCase):
    """自建栈口径：索引优先、云端默认不再参与。"""

    def setUp(self):
        self._real_url = proxy.VOD_LINE_INDEX_URL
        self._real_cloud = proxy.CLOUD_VOD_FALLBACK_ENABLED

    def tearDown(self):
        proxy.VOD_LINE_INDEX_URL = self._real_url
        proxy.CLOUD_VOD_FALLBACK_ENABLED = self._real_cloud

    def test_index_only_when_cloud_disabled(self):
        proxy.VOD_LINE_INDEX_URL = "http://storage:8092/vod-lines.json"
        proxy.CLOUD_VOD_FALLBACK_ENABLED = False
        providers = proxy._vod_fallback_sources()
        self.assertEqual(providers, (proxy._index_vod_sources,))

    def test_cloud_is_last_resort_when_enabled(self):
        proxy.VOD_LINE_INDEX_URL = "http://storage:8092/vod-lines.json"
        proxy.CLOUD_VOD_FALLBACK_ENABLED = True
        self.assertEqual(
            proxy._vod_fallback_sources(),
            (proxy._index_vod_sources, proxy._cloud_vod_sources))

    def test_no_provider_when_both_disabled(self):
        proxy.VOD_LINE_INDEX_URL = ""
        proxy.CLOUD_VOD_FALLBACK_ENABLED = False
        self.assertEqual(proxy._vod_fallback_sources(), ())


class CloudGetJsonGateTest(unittest.TestCase):
    """通用中继关掉（自建栈的迁移契约）时，只有显式放行的片源兜底能发云端请求。"""

    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def setUp(self):
        self._real_urlreq = proxy.urlreq
        self._real_enabled = proxy.CLOUD_RELAY_ENABLED

    def tearDown(self):
        proxy.urlreq = self._real_urlreq
        proxy.CLOUD_RELAY_ENABLED = self._real_enabled

    def _stub_urlreq(self):
        proxy.urlreq = types.SimpleNamespace(
            Request=lambda url, headers=None: url,
            urlopen=lambda req, timeout=12: self._Resp(b'{"sources": [{"id": "x"}]}'))

    def test_generic_path_is_blocked_when_relay_off(self):
        proxy.CLOUD_RELAY_ENABLED = False
        self._stub_urlreq()
        with self.assertRaises(RuntimeError) as ctx:
            proxy._cloud_get_json("/vod/detail/1")
        self.assertIn("relay disabled", str(ctx.exception))

    def test_explicit_allow_bypasses_relay_gate(self):
        proxy.CLOUD_RELAY_ENABLED = False
        self._stub_urlreq()
        payload = proxy._cloud_get_json("/vod/detail/1", allow_when_relay_off=True)
        self.assertEqual([line["id"] for line in payload["sources"]], ["x"])


if __name__ == "__main__":
    unittest.main()
