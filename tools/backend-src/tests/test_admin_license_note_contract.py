import pathlib
import unittest
import importlib.util
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[3]
SERVER = ROOT / "tools" / "admin_server.py"
ADMIN = ROOT / "tools" / "admin.html"
CLOUD_ADMIN = ROOT / "cloudfunctions" / "otv-admin" / "admin.html"
MIGRATION = ROOT / "tools" / "license_note_migration.sql"

spec = importlib.util.spec_from_file_location("admin_server_note_under_test", SERVER)
admin_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin_server)


class AdminLicenseNoteContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SERVER.read_text(encoding="utf-8")
        cls.admin = ADMIN.read_text(encoding="utf-8")
        cls.migration = MIGRATION.read_text(encoding="utf-8") if MIGRATION.exists() else ""

    def test_server_exposes_validated_note_update_route(self):
        self.assertIn("def h_update_note", self.server)
        self.assertIn('"/api/update_note": h_update_note', self.server)
        self.assertIn("admin_update_note", self.server)
        self.assertIn("200", self.server)

    def test_migration_adds_admin_note_rpc_and_audit(self):
        sql = self.migration.lower()
        self.assertIn("create or replace function public.admin_update_note", sql)
        self.assertIn("p_note text", sql)
        self.assertIn("update public.licenses", sql)
        self.assertIn("admin_note", sql)

    def test_admin_can_edit_and_clear_card_note(self):
        self.assertIn("备注", self.admin)
        self.assertIn("saveNote", self.admin)
        self.assertIn('api("update_note"', self.admin)
        self.assertIn("取消", self.admin)

    def test_note_lookup_is_scoped_to_license_table(self):
        """总览台账与卡密表共用 ledgerTable，行上 data-key 同名。

        查找必须限定在 #lic-table，否则同一卡密出现在总览「最近 8 条」时，
        编辑框会被写进隐藏的总览表——线上表现为「编辑按钮点了没反应」。
        """
        body = self.admin.split("function noteCell(code)")[1].split("function editNote")[0]
        self.assertIn('getElementById("lic-table")', body)
        self.assertNotIn("document.querySelector(`tr[data-key=", body)

    def test_note_editor_is_a_non_clipped_overlay(self):
        """备注列只有 12% 宽：编辑态必须脱离单元格 overflow，否则保存按钮被裁掉点不到。"""
        css = self.admin.split("table.grid tr:hover td.ops{background:#151f2b}")[1].split("/* 备注列")[1]
        self.assertIn("td.note-cell.editing{overflow:visible", css.split("*/")[1].replace(" ", ""))
        self.assertIn("note-edit", self.admin)

    def test_cloud_function_copy_matches_tools_admin(self):
        self.assertTrue(CLOUD_ADMIN.exists(), f"缺少云端副本 {CLOUD_ADMIN}")
        self.assertEqual(ADMIN.read_text(encoding="utf-8"), CLOUD_ADMIN.read_text(encoding="utf-8"))

    def test_update_note_rejects_bad_code_and_overlong_note(self):
        self.assertEqual(admin_server.h_update_note({"code": "bad", "note": "x"}), {"error": "bad_code"})
        code = "OTV-ABCDE-23456"
        self.assertEqual(
            admin_server.h_update_note({"code": code, "note": "x" * 201}),
            {"error": "note_too_long"},
        )

    def test_update_note_passes_normalized_values_to_rpc(self):
        with mock.patch.object(admin_server, "rpc", return_value={"ok": True}) as rpc:
            out = admin_server.h_update_note({"code": " otv-abcde-23456 ", "note": "  VIP 用户  "})
        self.assertEqual(out, {"ok": True})
        rpc.assert_called_once_with(
            "admin_update_note", {"p_code": "OTV-ABCDE-23456", "p_note": "VIP 用户"}
        )


if __name__ == "__main__":
    unittest.main()
