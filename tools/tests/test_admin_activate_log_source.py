import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_admin_server():
    """Load tools/admin_server.py with its sibling modules stubbed out.

    admin_server.py reaches into the deployed container layout at import time
    (sibling backend-src modules, private key file, storage root). Unit tests
    only exercise the request handlers, so those imports are stubbed.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    backend_src = ROOT / "backend-src"
    for extra in (str(ROOT), str(backend_src)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    for name in ("hhkan", "vod_sources", "apk_update", "license_gen", "selfhosted"):
        if name not in sys.modules:
            try:
                __import__(name)
            except ImportError:
                sys.modules[name] = types.ModuleType(name)
    import admin_server
    return admin_server


class AdminActivateLogSourceTests(unittest.TestCase):
    def setUp(self):
        self.admin = _load_admin_server()

    def test_logs_reads_from_selfhosted_license_v2_export(self):
        """自建栈没有 CloudBase 的 admin_logs，必须改用 activate_log 导出函数。"""
        calls = []

        def fake_rpc(name, payload):
            calls.append((name, payload))
            return {"total": 1, "rows": [{"id": 1, "at": "2026-09-29T00:00:00Z",
                                          "code": "OTV-AAAAA-BBBBB", "result": "ok",
                                          "device_id": "dev", "ip": "1.2.3.4"}]}

        with mock.patch.object(self.admin, "rpc", side_effect=fake_rpc):
            out = self.admin.h_logs({"code": "", "limit": 100})

        self.assertEqual([c[0] for c in calls], ["admin_license_v2_log_export"])
        self.assertNotIn("error", out)
        self.assertEqual(out["rows"][0]["code"], "OTV-AAAAA-BBBBB")

    def test_logs_keeps_code_filter_by_narrowing_rows(self):
        """RPC 不接受按码过滤，过滤在服务端对导出的行做精确/前缀匹配。"""
        rows = [
            {"id": 1, "at": "t1", "code": "OTV-AAAAA-BBBBB", "result": "ok"},
            {"id": 2, "at": "t2", "code": "OTV-CCCCC-DDDDD", "result": "ok"},
            {"id": 3, "at": "t3", "code": "OTV-AAAAA-EEEEE", "result": "ok"},
        ]

        with mock.patch.object(self.admin, "rpc",
                               return_value={"total": 3, "rows": rows}):
            out = self.admin.h_logs({"code": "OTV-AAAAA", "limit": 100})

        self.assertEqual([r["code"] for r in out["rows"]],
                         ["OTV-AAAAA-BBBBB", "OTV-AAAAA-EEEEE"])
        self.assertEqual(out["total"], 2)

    def test_logs_rejects_invalid_limit(self):
        with mock.patch.object(self.admin, "rpc", return_value={"total": 0, "rows": []}):
            self.assertEqual(self.admin.h_logs({"limit": "abc"}), {"error": "bad_limit"})

    def test_logs_always_returns_a_dict_to_the_console(self):
        """前端读 d.error / d.rows。返回非 dict 会让页面显示空白而不是错误。"""
        with mock.patch.object(self.admin, "rpc", return_value=["unexpected", "list"]):
            out = self.admin.h_logs({"code": "", "limit": 100})
        self.assertIsInstance(out, dict)
        self.assertEqual(out, {"error": "bad_rpc_response"})

    def test_vod_source_test_reports_latency(self):
        """后台按 latency_ms 排"最快源"。缺这个字段会让所有源都显示 0ms
        并被排到最前面。"""
        source = inspect.getsource(self.admin.h_vod_source_test)
        # Both the success and the failure branch must expose the key so the
        # console can read d.latency_ms unconditionally.
        self.assertGreaterEqual(source.count('"latency_ms"'), 2)

    def test_vod_source_test_failure_still_reports_latency_key(self):
        """异常分支也要带 latency_ms，前端才能统一读取。"""
        with mock.patch.object(self.admin, "hhkan", None):
            out = self.admin.h_vod_source_test({"title": "x", "episode": 1})
        self.assertIn("latency_ms", out)


if __name__ == "__main__":
    unittest.main()
