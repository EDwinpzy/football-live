import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _granted_functions(sql: str) -> set:
    """Collect function names that the migration grants to a PostgREST role."""
    granted = set()
    pattern = re.compile(
        r"grant\s+execute\s+on\s+function\s+(.*?)\s+to\s+"
        r"(authenticated|authenticator|anon)\b",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(sql):
        for item in _split_identities(match.group(1)):
            token = item.split("(")[0].strip()
            if "." in token:
                token = token.rsplit(".", 1)[-1]
            if token:
                granted.add(token.lower())
    return granted


def _split_identities(identity_list: str) -> list:
    """Split a GRANT function list on commas that are not inside parentheses."""
    items, depth, current = [], 0, []
    for char in identity_list:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return [i.strip() for i in items if i.strip()]


def _defined_functions(sql: str) -> set:
    """Collect public function names the migration creates."""
    names = set()
    pattern = re.compile(
        r"create\s+or\s+replace\s+function\s+public\.([a-z0-9_]+)\s*\(",
        re.IGNORECASE,
    )
    for match in pattern.finditer(sql):
        names.add(match.group(1).lower())
    return names


# Trigger functions run from inside the database, never over HTTP, so they
# need no PostgREST grant. Anything else the migration creates is an RPC the
# admin console calls and must therefore be granted.
NON_RPC_FUNCTIONS = {"licenses_touch_updated_at"}


class AdminRpcGrantTests(unittest.TestCase):
    """PostgREST 在进入函数体之前先检查数据库层的 EXECUTE 权限。

    这些管理 RPC 的函数体内部用 auth.uid() 白名单做授权，但那只在函数
    被调用之后才生效。缺了 GRANT 的话，PostgREST 会先拒绝，报
    "permission denied for function xxx"，后台的卡密列表、电视源、
    版本列表页全部不可用。admin_sold_migration.sql 和 iptv_migration.sql
    都漏了这一段，属于切栈时未补齐的授权。
    """

    def _assert_all_defined_functions_are_granted(self, filename: str) -> None:
        sql = (ROOT / filename).read_text(encoding="utf-8")
        defined = _defined_functions(sql) - NON_RPC_FUNCTIONS
        granted = _granted_functions(sql)
        missing = sorted(defined - granted)
        self.assertEqual(
            missing, [],
            f"{filename} 创建了函数但没授予 PostgREST 角色 EXECUTE：{missing}")

    def test_admin_sold_migration_grants_execute_on_all_its_functions(self):
        self._assert_all_defined_functions_are_granted("admin_sold_migration.sql")

    def test_iptv_migration_grants_execute_on_all_its_functions(self):
        self._assert_all_defined_functions_are_granted("iptv_migration.sql")

    def test_vod_sources_migration_grants_execute_on_all_its_functions(self):
        self._assert_all_defined_functions_are_granted("vod_sources_migration.sql")

    def test_announcement_migration_grants_execute_on_all_its_functions(self):
        self._assert_all_defined_functions_are_granted("announcement_migration.sql")

    def test_hotupdate_migration_grants_execute_on_all_its_functions(self):
        self._assert_all_defined_functions_are_granted("hotupdate_migration.sql")

    def test_repair_migration_is_wired_into_the_deploy_flow(self):
        """授权补丁必须在每次部署执行：老库的账本已记满，改原迁移不会重跑。"""
        psql = (ROOT / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8")
        self.assertIn(r"\ir rpc_grants_repair.sql", psql)
        sh = (ROOT / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")
        self.assertIn("selfhosted/rpc_grants_repair.sql", sh)

    def test_repair_grants_admin_and_iptv_rpcs_to_authenticated(self):
        repair = (ROOT / "selfhosted" / "rpc_grants_repair.sql").read_text(encoding="utf-8")
        granted = _granted_functions(repair)
        for name in ("admin_licenses", "admin_mark_sold", "admin_stats",
                     "iptv_admin_list", "iptv_admin_versions", "iptv_admin_logs",
                     "iptv_admin_publish_lock", "iptv_admin_publish_confirm"):
            self.assertIn(name, granted, f"{name} 未在补丁中授权")

    def test_repair_revokes_admin_rpcs_from_anon(self):
        """管理接口必须对 anon 收紧，授权只给 authenticated。"""
        repair = (ROOT / "selfhosted" / "rpc_grants_repair.sql").read_text(encoding="utf-8")
        revoked_blocks = re.findall(
            r"revoke\s+execute\s+on\s+function\s+(.*?)\s+from\s+anon",
            repair, re.IGNORECASE | re.DOTALL)
        self.assertTrue(revoked_blocks, "补丁没有对 anon 做反向收紧")
        revoked_names: set = set()
        for block in revoked_blocks:
            names = {t.split("(")[0].strip().rsplit(".", 1)[-1].lower()
                      for t in _split_identities(block)}
            revoked_names |= names
        self.assertIn("admin_licenses", revoked_names)
        self.assertIn("iptv_admin_list", revoked_names)
        self.assertIn("iptv_admin_publish_lock", revoked_names)


if __name__ == "__main__":
    unittest.main()
