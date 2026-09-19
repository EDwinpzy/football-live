import pathlib
import unittest
import importlib.util
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[3]
SERVER = ROOT / "tools" / "admin_server.py"
ADMIN = ROOT / "tools" / "admin.html"
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
