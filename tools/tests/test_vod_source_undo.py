"""影视源删除后未推送，需要「撤销 / 回滚」能力（BUG-06）。

现状问题：`admin.html` 的影视源 tab 只有「删除」，而 `vod_source_admin_delete`
直接 `delete from vod_source_drafts where id=p_id`——**不可逆**。
误删一个源后，只能靠 SQL 手工补回，或者把整个配置回滚到旧版本（连带丢掉
其它源期间做的所有改动）。

可行的撤销基础已经有了：`vod_source_versions` 存着每个发布版本的**完整配置
快照**（config jsonb）。所以撤销 = 把 drafts 按某个历史版本重建。

本测试锁定行为契约：
  1. 列出可回滚的历史版本（版本号 + 时间 + 源数量 + sha256）
  2. 回滚到指定版本后，drafts 与该版本的 sources 逐条一致
  3. 回滚只影响 drafts，不删历史版本（历史是只追加的账本）
  4. 回滚后仍需显式 publish 才对线上生效——撤销不是隐式发布
  5. 版本号非法 / 不存在时返回明确错误，不静默成功
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "tools" / "vod_source_undo_migration.sql"
ADMIN_SERVER = ROOT / "tools" / "admin_server.py"
ADMIN_HTML = ROOT / "tools" / "admin.html"
LEDGER_PSQL = ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql"
LEDGER_SH = ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh"


class VodSourceUndoContractTest(unittest.TestCase):
    def test_migration_defines_rollback_rpc(self):
        self.assertTrue(MIGRATION.is_file(), "缺少 tools/vod_source_undo_migration.sql")
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("vod_source_admin_versions", sql)
        self.assertIn("vod_source_admin_rollback", sql)

    def test_rollback_rebuilds_drafts_from_a_version_snapshot(self):
        """回滚必须按 config.sources 重建 drafts，而不是删历史。"""
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("insert into vod_source_drafts", sql)
        self.assertIn("from vod_source_versions", sql)
        # 先清空 drafts 再按快照重建，保证幂等
        self.assertIn("delete from vod_source_drafts", sql)

    def test_rollback_is_guarded_by_auth_uid_like_siblings(self):
        """与既有 vod_source_admin_* 保持同一套守卫。"""
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("auth.uid()", sql)
        self.assertIn("2094843486709620737", sql)

    def test_rollback_never_deletes_history(self):
        """历史版本是只追加账本，回滚不得删它——否则撤销本身不可撤销。"""
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertNotIn("delete from vod_source_versions", sql)
        self.assertNotIn("truncate", sql)

    def test_rollback_rejects_unknown_version(self):
        """版本号不存在时必须明确报错，不静默成功。"""
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("not_found", sql)

    def test_rollback_preserves_revision_per_source(self):
        """回滚后每条源要带上该快照里的 revision，否则并发编辑保护失效。"""
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("revision", sql)

    def test_admin_server_exposes_rollback_endpoint(self):
        server = ADMIN_SERVER.read_text(encoding="utf-8")
        self.assertIn("def h_vod_source_rollback", server)
        self.assertIn("def h_vod_source_versions", server)
        self.assertIn('"/api/vod_source_rollback"', server)
        self.assertIn('"/api/vod_source_versions"', server)

    def test_rollback_endpoint_does_not_publish_implicitly(self):
        """撤销只改 drafts；生效仍需显式 publish——避免「以为撤回了其实线上没变」。"""
        server = ADMIN_SERVER.read_text(encoding="utf-8")
        body = server.split("def h_vod_source_rollback", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("vod_source_admin_publish", body)

    def test_frontend_offers_undo_ui(self):
        """后台要有撤销入口，否则功能做了也没人用得到。"""
        html = ADMIN_HTML.read_text(encoding="utf-8")
        self.assertIn("vod_source_rollback", html)
        self.assertIn("vod_source_versions", html)

    def test_rollback_is_registered_in_migration_ledger(self):
        self.assertIn("vod_source_undo_migration.sql", LEDGER_PSQL.read_text(encoding="utf-8"))
        self.assertIn("vod_source_undo_sha256", LEDGER_SH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
