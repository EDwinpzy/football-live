"""契约测试：后台「还原」（按页面清库）的安全边界。

这些断言锁的是「破坏了会出人命」的部分：
  * 双闸（后台密码 + 逐页确认短语）缺一不可——后台可能是免登录形态（OTV_ADMIN_NOAUTH=1）；
  * 删除前必须整表导出并随响应返回，前端据此落本地备份；
  * 电视源 / 影视源只清历史，保留当前生效那一版，且永不触碰线上正在用的配置表
    （vod_source_drafts / iptv_channel / iptv_line / iptv_config）。
"""
import importlib.util
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
ADMIN_SERVER = ROOT / "tools" / "admin_server.py"
ADMIN_HTML = ROOT / "tools" / "admin.html"

spec = importlib.util.spec_from_file_location("admin_server_reset_under_test", ADMIN_SERVER)
admin_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin_server)

LIVE_CONFIG_TABLES = {"vod_source_drafts", "iptv_channel", "iptv_line", "iptv_config"}


class FakeDb:
    """内存版 REST 替身：记录调用顺序，供「先备份后删除」这类顺序断言使用。"""

    def __init__(self, data=None):
        self.data = {k: [dict(r) for r in v] for k, v in (data or {}).items()}
        self.calls = []

    def count(self, table):
        self.calls.append(("count", table))
        return len(self.data.get(table, []))

    def newest(self, table, column):
        self.calls.append(("newest", table, column))
        vals = [int(r[column]) for r in self.data.get(table, []) if r.get(column) is not None]
        return max(vals) if vals else 0

    def dump(self, table):
        self.calls.append(("dump", table))
        return [dict(r) for r in self.data.get(table, [])]

    def delete(self, table, query=""):
        self.calls.append(("delete", table, query))
        if query.startswith("version=lt."):
            floor = int(query.split("lt.", 1)[1])
            self.data[table] = [r for r in self.data.get(table, []) if int(r["version"]) >= floor]
        else:
            self.data[table] = []
        return 204


def _install(db):
    patches = [
        mock.patch.object(admin_server, "_rest_count", db.count),
        mock.patch.object(admin_server, "_rest_newest", db.newest),
        mock.patch.object(admin_server, "_rest_dump", db.dump),
        mock.patch.object(admin_server, "_rest_delete", db.delete),
        mock.patch.object(admin_server, "_hu_obj_delete",
                          lambda key: db.calls.append(("obj_delete", key))),
        mock.patch.object(admin_server, "ADMIN_PASS", "test-pass"),
    ]
    for p in patches:
        p.start()
    return patches


class ResetScopeSafetyTest(unittest.TestCase):
    def test_scopes_never_target_live_config_tables(self):
        for scope, spec_ in admin_server.RESET_SCOPES.items():
            tables = {t for t, _ in spec_["tables"]} | {t for t, _, _ in spec_.get("keep_latest", ())}
            self.assertFalse(tables & LIVE_CONFIG_TABLES, f"{scope} 竟要删线上配置表: {tables}")
            for key in ("confirm", "label", "desc"):
                self.assertIn(key, spec_)

    def test_keep_latest_scopes_declare_version_column(self):
        kept = admin_server.RESET_SCOPES["iptv"].get("keep_latest") or ()
        self.assertTrue(kept, "电视源必须声明保留最新版")
        self.assertEqual(kept[0][2], "version")

    def test_vod_scope_is_declared_unsupported(self):
        """影视源两张表对服务账号 403：这页必须如实说明，而不是给个点了报错的按钮。"""
        spec_ = admin_server.RESET_SCOPES["vod"]
        self.assertTrue(spec_.get("unsupported"))
        self.assertEqual(spec_["tables"], ())
        out = admin_server.reset_preview("vod")
        self.assertTrue(out["unsupported"])
        self.assertEqual(out["total"], 0)
        self.assertIn("控制台", out["reason"])
        self.assertIn("error", admin_server.reset_apply("vod", "test-pass", "还原影视源"))

    def test_routes_registered(self):
        source = ADMIN_SERVER.read_text(encoding="utf-8")
        for route in ("reset_preview", "reset"):
            self.assertIn('"/api/' + route + '"', source)


class ResetGuardTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb({"licenses": [{"code": "a"}, {"code": "b"}], "activate_log": [{"id": 1}]})
        self._patches = _install(self.db)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_preview_is_read_only(self):
        out = admin_server.reset_preview("licenses")
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["confirm"], "还原卡密")
        self.assertFalse([c for c in self.db.calls if c[0] in ("dump", "delete", "obj_delete")])

    def test_wrong_password_does_nothing(self):
        out = admin_server.reset_apply("licenses", "nope", "还原卡密")
        self.assertIn("密码", out["error"])
        self.assertFalse([c for c in self.db.calls if c[0] in ("dump", "delete")])
        self.assertEqual(len(self.db.data["licenses"]), 2)

    def test_wrong_confirm_phrase_does_nothing(self):
        out = admin_server.reset_apply("licenses", "test-pass", "还原")
        self.assertIn("确认短语", out["error"])
        self.assertFalse([c for c in self.db.calls if c[0] in ("dump", "delete")])
        self.assertEqual(len(self.db.data["licenses"]), 2)

    def test_unknown_scope_rejected(self):
        self.assertIn("error", admin_server.reset_preview("everything"))
        self.assertIn("error", admin_server.reset_apply("everything", "test-pass", "x"))

    def test_oversize_backup_aborts_before_deleting(self):
        with mock.patch.object(admin_server, "RESET_BACKUP_MAX_BYTES", 10):
            out = admin_server.reset_apply("licenses", "test-pass", "还原卡密")
        self.assertIn("分批", out["error"])
        self.assertFalse([c for c in self.db.calls if c[0] == "delete"])
        self.assertEqual(len(self.db.data["licenses"]), 2)


class ResetApplyTest(unittest.TestCase):
    def setUp(self):
        self._patches = []

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _db(self, data):
        db = FakeDb(data)
        self._patches = _install(db)
        return db

    def test_licenses_scope_keeps_activate_log(self):
        db = self._db({"licenses": [{"code": "a"}], "activate_log": [{"id": 1}, {"id": 2}]})
        out = admin_server.reset_apply("licenses", "test-pass", "还原卡密")
        self.assertTrue(out["ok"])
        self.assertEqual(out["deleted"][0]["deleted"], 1)
        self.assertEqual(db.data["licenses"], [])
        self.assertEqual(len(db.data["activate_log"]), 2)
        self.assertEqual(out["backupData"]["rows"]["licenses"], [{"code": "a"}])

    def test_backup_precedes_delete_and_covers_every_row(self):
        db = self._db({"activate_log": [{"id": 1}, {"id": 2}], "licenses": [{"code": "a"}],
                       "hotupdate_log": [{"id": 9}], "hotupdate_pkg": [{"hot_version_code": 3}],
                       "announcements": [{"id": 1}]})
        out = admin_server.reset_apply("overview", "test-pass", "还原总览")
        self.assertTrue(out["ok"])
        kinds = [c[0] for c in db.calls]
        self.assertLess(kinds.index("dump"), kinds.index("delete"))
        self.assertEqual(sum(v["rows"] for v in out["backup"]["rows"]), 6)
        self.assertEqual(out["residual"], [])
        for table in ("activate_log", "licenses", "hotupdate_log", "hotupdate_pkg", "announcements"):
            self.assertEqual(db.data[table], [], table)

    def test_hu_scope_deletes_bucket_objects(self):
        db = self._db({"hotupdate_pkg": [{"hot_version_code": 3, "object_key": "backend-hu3.zip"},
                                        {"hot_version_code": 4, "object_key": ""}],
                       "hotupdate_log": [{"id": 1}]})
        out = admin_server.reset_apply("hu", "test-pass", "还原热更新")
        self.assertTrue(out["ok"])
        self.assertEqual([c[1] for c in db.calls if c[0] == "obj_delete"],
                         ["backend-hu3.zip", "backend-hu4.zip"])
        self.assertEqual(db.data["hotupdate_pkg"], [])

    def test_source_scopes_keep_newest_version(self):
        db = self._db({"iptv_version": [{"version": 1}, {"version": 2}, {"version": 3}],
                       "iptv_admin_log": [{"id": 1}]})
        out = admin_server.reset_apply("iptv", "test-pass", "还原电视源")
        self.assertTrue(out["ok"])
        self.assertEqual([r["version"] for r in db.data["iptv_version"]], [3])
        self.assertIn(("delete", "iptv_version", "version=lt.3"), db.calls)
        self.assertEqual(db.data["iptv_admin_log"], [])
        self.assertEqual(out["kept"][0]["keptVersion"], 3)
        self.assertFalse([c for c in db.calls if c[1] in LIVE_CONFIG_TABLES])

    def test_backup_is_returned_for_local_download(self):
        db = self._db({"announcements": [{"id": 7, "title": "维护通知"}]})
        out = admin_server.reset_apply("ann", "test-pass", "还原公告")
        self.assertTrue(out["ok"])
        self.assertEqual(out["backupData"]["rows"]["announcements"], [{"id": 7, "title": "维护通知"}])
        self.assertEqual(len(out["backup"]["sha256"]), 64)
        self.assertTrue(out["backup"]["name"].endswith("-ann.json"))
        self.assertEqual(db.data["announcements"], [])


class ResetUiRemovedContractTest(unittest.TestCase):
    """2026-09-22：用户要求去掉后台「还原」按钮 → 界面不再有任何清库入口。

    服务端 reset / reset_preview 与双闸逻辑保留（由上面的 Reset*Test 继续保护），
    只是不再从界面暴露；将来要恢复入口，把 UI 补回来即可，安全边界没动过。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = ADMIN_HTML.read_text(encoding="utf-8")
        cls.server = ADMIN_SERVER.read_text(encoding="utf-8")

    def test_ui_no_longer_exposes_reset_entry(self):
        for tab in ("overview", "licenses", "logs", "gen", "hu", "ann", "iptv", "vod-sources"):
            self.assertNotIn("resetOpen('" + tab + "')", self.html, tab)
        self.assertNotIn("resetOpen(", self.html)
        self.assertNotIn("还原</button>", self.html)

    def test_reset_dialog_markup_is_gone(self):
        for anchor in ('id="reset-veil"', 'id="reset-pass"', 'id="reset-confirm"',
                       'id="reset-go"', 'id="reset-ok"', 'id="reset-err"'):
            self.assertNotIn(anchor, self.html)
        for fn in ("resetOpen", "resetClose", "resetSubmit", "resetDownloadBackup", "RESET_SCOPE"):
            self.assertNotIn(fn, self.html)

    def test_server_side_reset_api_is_still_intact(self):
        self.assertIn('"/api/reset_preview": h_reset_preview', self.server)
        self.assertIn('"/api/reset": h_reset', self.server)
        self.assertIn("确认短语不匹配", self.server)
        self.assertIn("后台密码不正确", self.server)


if __name__ == "__main__":
    unittest.main()
