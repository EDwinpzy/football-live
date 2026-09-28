import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IptvPublishLockMigrationTests(unittest.TestCase):
    """IPTV 发布锁是切栈时漏掉的一次迁移。

    tools/selfhosted/iptv_publish_lock.sql 定义了 iptv_publish_lock 表和
    iptv_admin_publish_lock RPC，admin_server 的 _guard_iptv_publisher 与
    scheduler.DatabasePublishLock 都依赖它。生产库漏跑这次迁移时，
    apply_schema_migrations.psql 的"已知旧库"校验会因为缺这两个对象而
    拒绝执行（refusing to replay historical migrations），进而所有依赖
    schema-migrations 成功才启动的服务（admin 等）都起不来。
    """

    def test_bootstrap_defines_the_publish_lock_table_and_rpc(self):
        sql = (ROOT / "selfhosted" / "iptv_publish_lock.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS public.iptv_publish_lock", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION public.iptv_admin_publish_lock", sql)

    def test_bootstrap_bundle_includes_the_publish_lock_sql(self):
        """发布锁随 db_bootstrap 一起应用，所以账本条目是 db_bootstrap.sql。"""
        bootstrap = (ROOT / "selfhosted" / "db_bootstrap.sql").read_text(encoding="utf-8")
        self.assertIn(r"\ir iptv_publish_lock.sql", bootstrap)

    def test_legacy_schema_probe_excludes_the_publish_lock_from_hard_fail(self):
        """缺发布锁的旧库应被识别为已知旧库，让缺失的那次迁移补跑，
        而不是整批拒绝。"""
        psql = (ROOT / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8")
        hard_fail = re.search(r"(?s)refusing to replay historical migrations.*?\);", psql)
        self.assertIsNotNone(hard_fail)
        self.assertNotIn("iptv_admin_publish_lock", hard_fail.group(0))

    def test_psql_boolean_variable_is_compared_as_a_string(self):
        """:var 会展开成裸的 t/f，直接放进 SQL 会变成 "WHERE f" 语法错。"""
        psql = (ROOT / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8")
        # \gset on a boolean yields bare t/f, so the probe result must be read
        # as text and quoted at the point of use.
        self.assertIn("AS legacy_supports_restore_text \\gset", psql)
        self.assertIn("WHERE :'legacy_supports_restore_text' = 'true'", psql)
        self.assertNotRegex(psql, r"(?m)^\s*WHERE :legacy_supports_restore\s")

    def test_migration_script_surfaces_psql_errors(self):
        """迁移失败时必须把 psql 输出打出来，否则线上只能看到一句通用错误。"""
        sh = (ROOT / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")
        self.assertIn("psql output follows", sh)
        self.assertIn('grep -v', sh)

    def test_repair_step_installs_and_asserts_the_publish_lease(self):
        """账本已记的 db_bootstrap 不会重跑，发布锁必须由每次执行的修复步骤补建，
        并显式断言建成了 —— \\ir 里的报错不会可靠地让迁移失败。"""
        repair = (ROOT / "selfhosted" / "rpc_grants_repair.sql").read_text(encoding="utf-8")
        self.assertIn(r"\ir iptv_publish_lock.sql", repair)
        self.assertIn("assert_publish_lock_ready", repair)
        self.assertIn("to_regclass('public.iptv_publish_lock')", repair)
        self.assertIn("to_regprocedure('public.iptv_admin_publish_lock(text,text)')", repair)


if __name__ == "__main__":
    unittest.main()
