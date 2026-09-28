import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LicenseImporterSmokeSqlTests(unittest.TestCase):
    """license_importer_smoke.psql 用 \gset 断言"不该有行"的结果。

    psql 的 \gset 在零行时直接报 "no rows returned for \gset" 并中止，
    而 ON CONFLICT DO NOTHING ... RETURNING 在冲突时恰恰返回零行。
    也就是说这些断言永远无法表达它想表达的意思，迁移会假失败。
    统一改成 count(*)：恒定返回一行，可安全 \gset。
    """

    def setUp(self):
        self.sql = (ROOT / "selfhosted" / "license_importer_smoke.psql").read_text(encoding="utf-8")

    def test_no_gset_is_attached_to_a_returning_clause(self):
        offenders = re.findall(r"RETURNING[^\n]*\n\s*\\gset", self.sql)
        self.assertEqual(offenders, [], "RETURNING + \\gset 会在零行时中止")

    def test_assertions_use_count_star(self):
        self.assertIn("AS inserted_rows", self.sql)
        self.assertIn("AS duplicate_rows", self.sql)
        self.assertIn("AS found_rows", self.sql)


if __name__ == "__main__":
    unittest.main()
