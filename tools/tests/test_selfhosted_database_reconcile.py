import json
import unittest

from tools.selfhosted.database_reconcile import build_reconciliation_sql
from tools.selfhosted.import_cloudbase import BUSINESS_TABLES, SEQUENCE_TABLES


def sample_export():
    return {
        "tables": {name: [] for name in BUSINESS_TABLES},
        "sequence_state": {name: None for name in SEQUENCE_TABLES},
    }


class DatabaseReconciliationTests(unittest.TestCase):
    def test_same_count_different_content_uses_multiset_comparison(self):
        export = sample_export()
        export["tables"]["licenses"] = [{"id": "one", "note": "original"}]
        sql = build_reconciliation_sql(export)
        self.assertIn("EXCEPT ALL SELECT row_data FROM target_0", sql)
        self.assertIn("EXCEPT ALL SELECT row_data FROM source_0", sql)
        self.assertIn("NOT EXISTS (SELECT row_data FROM source_0", sql)
        self.assertIn("NOT EXISTS (SELECT row_data FROM target_0", sql)
        self.assertIn("'match',", sql)
        self.assertNotIn("SELECT * FROM public", sql)

    def test_exact_match_query_normalizes_source_to_target_types(self):
        export = sample_export()
        export["tables"]["licenses"] = [{"id": "a", "note": "O'Brien\\test"}]
        sql = build_reconciliation_sql(export)
        self.assertIn('jsonb_populate_record(NULL::public."licenses", row_data)', sql)
        self.assertIn('to_jsonb(t) AS row_data FROM public."licenses" AS t', sql)
        self.assertIn("SET LOCAL standard_conforming_strings = on", sql)
        self.assertIn("O''Brien", sql)
        self.assertIn("BEGIN READ ONLY", sql)
        self.assertIn("'tables', jsonb_build_object", sql)
        self.assertIn("'source', source_count, 'target', target_count, 'match', is_match", sql)
        self.assertIn("pg_attribute", sql)
        self.assertNotIn("INSERT INTO", sql)

    def test_sequence_comparison_uses_exported_null_and_integer_states(self):
        export = sample_export()
        export["sequence_state"]["activate_log_id_seq"] = 42
        sql = build_reconciliation_sql(export)
        self.assertIn("sequencename = 'activate_log_id_seq' AND last_value IS NOT DISTINCT FROM 42::bigint", sql)
        self.assertIn("last_value IS NOT DISTINCT FROM NULL::bigint", sql)
        self.assertIn("'sequences', jsonb_build_object", sql)
        self.assertNotIn("'last_value',", sql)

    def test_rejects_malformed_or_out_of_scope_export(self):
        cases = []
        export = sample_export()
        export["tables"]["other_table"] = []
        cases.append(export)
        export = sample_export()
        del export["tables"][BUSINESS_TABLES[0]]
        cases.append(export)
        export = sample_export()
        export["tables"][BUSINESS_TABLES[0]] = ["not a row"]
        cases.append(export)
        export = sample_export()
        export["tables"][BUSINESS_TABLES[0]] = [{"x": float("nan")}]
        cases.append(export)
        export = sample_export()
        export["sequence_state"][next(iter(SEQUENCE_TABLES))] = True
        cases.append(export)
        export = sample_export()
        export["sequence_state"]["unexpected_seq"] = 1
        cases.append(export)
        for malformed in cases:
            with self.subTest(case=json.dumps(malformed, default=str)[:80]):
                with self.assertRaises(ValueError):
                    build_reconciliation_sql(malformed)


if __name__ == "__main__":
    unittest.main()
