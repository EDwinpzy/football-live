import importlib.util
import pathlib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[3]
ADMIN_SERVER = ROOT / "tools" / "admin_server.py"
ADMIN_HTML = ROOT / "tools" / "admin.html"
VOD_MIGRATION = ROOT / "tools" / "vod_sources_migration.sql"

spec = importlib.util.spec_from_file_location("admin_server", ADMIN_SERVER)
admin_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin_server)


class VodSourceAdminContractTest(unittest.TestCase):
    def test_source_mutations_are_registered_behind_session_gate(self):
        source = ADMIN_SERVER.read_text(encoding="utf-8")
        for route in ("vod_source_list", "vod_source_save", "vod_source_delete",
                      "vod_source_discover", "vod_source_test", "vod_source_publish"):
            self.assertIn('"/api/' + route + '"', source)

    def test_source_toggle_and_priority_mutations_are_registered(self):
        source = ADMIN_SERVER.read_text(encoding="utf-8")
        for route in ("vod_source_set_enabled", "vod_source_set_priority"):
            self.assertIn('"/api/' + route + '"', source)

    def test_source_toggle_forwards_id_and_enabled_without_edit_payload(self):
        with mock.patch.object(admin_server, "rpc", return_value={"ok": True}) as call:
            result = admin_server.h_vod_source_set_enabled({"id": "source-1", "enabled": False})
        self.assertEqual(result, {"ok": True})
        call.assert_called_once_with("vod_source_admin_set_enabled", {
            "p_id": "source-1", "p_enabled": False,
        })

    def test_source_priority_forwards_source_id(self):
        with mock.patch.object(admin_server, "rpc", return_value={"ok": True}) as call:
            result = admin_server.h_vod_source_set_priority({"id": "source-1"})
        self.assertEqual(result, {"ok": True})
        call.assert_called_once_with("vod_source_admin_set_priority", {"p_id": "source-1"})

    def test_source_sql_mutations_keep_one_enabled_and_reorder_priority(self):
        sql = VOD_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("create or replace function vod_source_admin_set_enabled", sql)
        self.assertIn("至少需要一个启用中的影视源", sql)
        self.assertIn("create or replace function vod_source_admin_set_priority", sql)
        self.assertIn("停用中的影视源不能设为默认优先", sql)
        self.assertIn("row_number() over(order by (id=p_id) desc, sort_order, id)", sql)

    def test_test_grade_boundaries(self):
        self.assertEqual(admin_server.vod_test_grade(80), "优秀")
        self.assertEqual(admin_server.vod_test_grade(60), "良好")
        self.assertEqual(admin_server.vod_test_grade(40), "一般")
        self.assertEqual(admin_server.vod_test_grade(39), "不可用")

    def test_admin_has_draft_publish_and_per_source_test_controls(self):
        html = ADMIN_HTML.read_text(encoding="utf-8")
        self.assertIn('data-tab="vod-sources"', html)
        self.assertIn('id="btn-vod-source-publish"', html)
        self.assertIn('id="vod-source-test-veil"', html)
        self.assertIn('id="vod-source-preview"', html)
        self.assertIn("未发布变更", html)
        self.assertIn("立即推送", html)
        self.assertIn("vodSourceToggle", html)
        self.assertIn("vodSourcePrioritize", html)
        self.assertIn("设为默认优先", html)

    def test_admin_reads_drafts_payload_returned_by_rpc(self):
        html = ADMIN_HTML.read_text(encoding="utf-8")
        self.assertIn("payload.drafts", html)
        self.assertIn("publishedVersion", html)

    def test_admin_only_shows_preview_for_automatically_matched_candidate(self):
        html = ADMIN_HTML.read_text(encoding="utf-8")
        self.assertIn("d.matched && d.preview_url", html)

    def test_source_test_does_not_call_zero_match_preview_playable(self):
        class FakeAdapter:
            def __init__(self, _source):
                pass

            def search(self, _subject):
                return [{
                    "id": "99260", "title": "凡人修仙传 慕兰之战", "year": "2026",
                    "category": "anime", "season": 0,
                    "raw": {"vod_play_url": "第1集$https://v.example/1.m3u8"},
                }]

        payload = {
            "source": {
                "id": "360zy", "name": "360", "api_url": "https://example.test/api",
                "adapter_type": "macms_json",
            },
            "title": "凡人修仙传",
            "year": "2026",
            "episode": 1,
        }
        with mock.patch.object(admin_server.vod_sources, "MacCmsAdapter", FakeAdapter):
            result = admin_server.h_vod_source_test(payload)
        self.assertFalse(result["matched"])
        self.assertEqual(result["grade"], "不可用")
        self.assertIn("匹配度 0", result["summary"])

    def test_selfhosted_admin_image_includes_vod_source_runtime_and_ui(self):
        dockerfile = (ROOT / "deploy" / "selfhosted" / "Dockerfile.admin").read_text(encoding="utf-8")
        self.assertIn("COPY tools/backend-src /app/tools/backend-src", dockerfile)
        self.assertIn("COPY tools/admin.html /app/tools/admin.html", dockerfile)
        html = (ROOT / "tools" / "admin.html").read_text(encoding="utf-8")
        self.assertIn('data-tab="vod-sources"', html)


if __name__ == "__main__":
    unittest.main()
