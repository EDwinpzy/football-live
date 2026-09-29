import unittest
from pathlib import Path
import re
import yaml

from tools.selfhosted.import_cloudbase import (
    BUSINESS_TABLES,
    MIGRATION_ORDER,
    apply_schema_migrations,
    build_reconciliation_report,
    build_data_import_sql,
    migration_paths,
    parse_tcb_export_result,
    SEQUENCE_TABLES,
    render_migration_sql,
)


ROOT = Path(__file__).resolve().parents[2]


class SelfHostedDatabaseContractTests(unittest.TestCase):
    def test_bootstrap_preserves_auth_uid_text_subject_from_postgrest_claims(self):
        sql = (ROOT / "tools" / "selfhosted" / "db_bootstrap.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE ROLE anon", sql)
        self.assertIn("CREATE ROLE authenticated", sql)
        self.assertIn("CREATE ROLE authenticator", sql)
        self.assertIn("request.jwt.claims", sql)
        self.assertIn("->> 'sub'", sql)
        self.assertIn("RETURNS text", sql)
        self.assertIn("GRANT anon, authenticated TO authenticator", sql)

    def test_postgrest_connects_as_the_bootstrapped_authenticator_role(self):
        compose = (ROOT / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8")
        env_example = (ROOT / "deploy" / "selfhosted" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("POSTGREST_DB_USER:-authenticator", compose)
        self.assertIn("POSTGREST_DB_USER=authenticator", env_example)

    def test_existing_postgres_volumes_migrate_before_postgrest_starts(self):
        compose = yaml.safe_load((ROOT / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8"))
        migrations = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8")
        runner = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")

        services = compose["services"]
        migration_service = services["schema-migrations"]
        self.assertEqual(migration_service["depends_on"]["postgres"]["condition"], "service_healthy")
        self.assertEqual(
            services["postgrest"]["depends_on"]["schema-migrations"]["condition"],
            "service_completed_successfully",
        )
        migration_environment = migration_service["environment"]
        self.assertIn("POSTGRES_PASSWORD", migration_environment)
        self.assertIn("LICENSE_IMPORTER_PASSWORD_FILE", migration_environment)
        self.assertNotIn("LICENSE_IMPORTER_PASSWORD", migration_environment)
        self.assertIn("license-importer-db-password", " ".join(migration_service["volumes"]))
        migration_mounts = " ".join(migration_service["volumes"])
        for migration_file in (
            "apply_schema_migrations.psql",
            "db_bootstrap.sql",
            "iptv_publish_lock.sql",
            "base_schema.sql",
            "license_importer_role.sql",
            "license_importer_smoke.psql",
            *MIGRATION_ORDER,
        ):
            self.assertIn(migration_file, migration_mounts)
        self.assertIn("schema_migrations", migrations)
        self.assertIn("license_v2_migration.sql", migrations)
        self.assertIn("license_importer_smoke.psql", migrations)
        self.assertIn("ON_ERROR_STOP=1", runner)
        self.assertIn("output_file", runner)
        self.assertIn("schema migration failed", runner)
        self.assertNotIn("printf '%s\\n' \"$LICENSE_IMPORTER_PASSWORD\"", runner)

    def test_license_import_role_is_reconciled_with_least_privilege_grants(self):
        policy = (ROOT / "tools" / "selfhosted" / "license_importer_role.sql").read_text(encoding="utf-8").lower()
        smoke = (ROOT / "tools" / "selfhosted" / "license_importer_smoke.psql").read_text(encoding="utf-8").lower()

        self.assertIn("license_importer_password", policy)
        self.assertIn("revoke all privileges on all tables in schema public", policy)
        self.assertIn("grant insert (code, plan, days, ticket, note), select (code)", policy)
        self.assertIn("has_column_privilege", policy)
        self.assertIn("has_table_privilege", policy)
        self.assertIn("on conflict (code) do nothing", smoke)
        self.assertIn("perform ticket", smoke)
        self.assertIn("update public.licenses", smoke)
        self.assertIn("insufficient_privilege", smoke)
        self.assertIn("rollback", smoke)

    def test_runtime_runner_strips_cloudbase_only_migration_sections(self):
        runner = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")
        hotupdate = (ROOT / "tools" / "hotupdate_migration.sql").read_text(encoding="utf-8")

        self.assertIn("SELFHOSTED-OMIT-BEGIN", runner)
        self.assertIn("SELFHOSTED-OMIT-END", runner)
        self.assertIn("hotupdate_migration.sql", runner)
        self.assertIn("SELFHOSTED-OMIT-BEGIN", hotupdate)
        self.assertIn("storage.buckets", hotupdate)

    def test_migrations_are_explicitly_ordered_and_exclude_destructive_reset(self):
        self.assertEqual(
            MIGRATION_ORDER,
            (
                "license_v2_migration.sql",
                "license_note_migration.sql",
                "admin_sold_migration.sql",
                "hotupdate_migration.sql",
                "announcement_migration.sql",
                "iptv_migration.sql",
                "vod_sources_migration.sql",
                "pg_cu_optimization_migration.sql",
            ),
        )
        self.assertTrue(all((ROOT / "tools" / name).is_file() for name in MIGRATION_ORDER))
        self.assertNotIn("license_v2_reset.sql", MIGRATION_ORDER)
        runtime_versions = re.findall(
            r"WHERE version = '([^']+\.sql)'",
            (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8"),
        )
        # The self-hosted ledger runs entries the CloudBase cutover bundle
        # deliberately omits:
        #   - db_bootstrap / base_schema: CloudBase already had a schema.
        #   - license_restore_v1: lives in its own migration so a legacy
        #     database cannot claim it as already applied.
        #   - admin_license_action_migration: self-hosted-only fix. The
        #     admin_unbind / admin_set_status RPCs were called by
        #     admin_server.py but never defined anywhere, so every「解绑」/
        #     「改状态」failed with "Could not find the function … in the
        #     schema cache". CloudBase is long retired, so this belongs to the
        #     self-hosted ledger only.
        #   - vod_source_undo_migration: self-hosted-only too. Adds the
        #     vod_source_admin_versions / vod_source_admin_rollback RPCs the
        #     admin UI needs to undo a source deletion (BUG-06); they read
        #     vod_source_versions, which only exists on the self-hosted stack.
        self.assertEqual(
            tuple(runtime_versions),
            (
                "db_bootstrap.sql",
                "base_schema.sql",
                "license_v2_migration.sql",
                "license_note_migration.sql",
                "license_restore_v1.sql",
                "admin_sold_migration.sql",
                "admin_license_action_migration.sql",
                "vod_source_undo_migration.sql",
                "hotupdate_migration.sql",
                "announcement_migration.sql",
                "iptv_migration.sql",
                "vod_sources_migration.sql",
                "pg_cu_optimization_migration.sql",
            ),
        )
        self.assertEqual(
            tuple(version for version in runtime_versions if version not in MIGRATION_ORDER),
            (
                "db_bootstrap.sql",
                "base_schema.sql",
                "license_restore_v1.sql",
                "admin_license_action_migration.sql",
                "vod_source_undo_migration.sql",
            ),
        )
        # Restore is a self-hosted ledger entry only; it must not be pulled into
        # the CloudBase import bundle, which replays MIGRATION_ORDER only.
        self.assertNotIn("license_restore_v1.sql", MIGRATION_ORDER)

    def test_reconciliation_report_contains_only_counts_and_sequence_state(self):
        report = build_reconciliation_report(
            {"licenses": 102, "activate_log": 162},
            {"licenses": 102, "activate_log": 160},
            {"licenses_id_seq": 102},
        )
        self.assertEqual(report["tables"]["licenses"], {"source": 102, "target": 102, "match": True})
        self.assertEqual(report["tables"]["activate_log"]["match"], False)
        self.assertEqual(report["sequences"]["licenses_id_seq"], 102)
        self.assertEqual(report["mismatched_tables"], ["activate_log"])
        self.assertNotIn("rows", report)

    def test_cloudbase_cli_rows_decode_to_table_objects(self):
        result = {
            "data": {
                "Rows": [
                    '["[{\\"id\\":1,\\"title\\":\\"hello\\"},{\\"id\\":2,\\"title\\":\\"world\\"}]"]'
                ]
            }
        }
        self.assertEqual(
            parse_tcb_export_result(result),
            [{"id": 1, "title": "hello"}, {"id": 2, "title": "world"}],
        )

    def test_data_import_is_atomic_non_destructive_and_preserves_identity_values(self):
        exported = {
            "tables": {name: [] for name in BUSINESS_TABLES},
            "sequence_state": {name: None for name in SEQUENCE_TABLES},
        }
        exported["sequence_state"].update({"iptv_channel_id_seq": 80, "iptv_line_id_seq": 130})
        exported["tables"]["iptv_channel"] = [{"id": 80, "name": "A"}]
        exported["tables"]["iptv_line"] = [{"id": 130, "channel_id": 80}]
        sql = build_data_import_sql(exported)
        self.assertIn("ON_ERROR_STOP", sql)
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("OVERRIDING SYSTEM VALUE", sql)
        self.assertIn("jsonb_array_length(source_rows)", sql)
        self.assertIn('to_jsonb(existing) = to_jsonb(jsonb_populate_record(NULL::public."iptv_channel", src.row_data))', sql)
        self.assertIn("target table differs from CloudBase export", sql)
        self.assertLess(sql.index('INSERT INTO public."iptv_channel"'), sql.index('INSERT INTO public."iptv_line"'))
        self.assertIn("setval(pg_get_serial_sequence", sql)
        self.assertNotIn("TRUNCATE", sql.upper())
        self.assertIn('"name":"A"', sql)

    def test_data_import_rejects_partial_exports(self):
        with self.assertRaisesRegex(ValueError, "missing table"):
            build_data_import_sql({"tables": {"licenses": []}, "sequence_state": {}})

    def test_data_import_adds_missing_source_rows_without_overwriting_target_conflicts(self):
        exported = {
            "tables": {name: [] for name in BUSINESS_TABLES},
            "sequence_state": {name: None for name in SEQUENCE_TABLES},
        }
        exported["tables"]["activate_log"] = [{"id": 163, "device_id": "staged-device"}]
        sql = build_data_import_sql(exported)
        blocks = sql.split("DO $mytv_data_import$\n")
        block = next(part.split("END $mytv_data_import$;", 1)[0] for part in blocks if 'public."activate_log"' in part)

        self.assertIn('INSERT INTO public."activate_log" OVERRIDING SYSTEM VALUE', block)
        self.assertIn("ON CONFLICT DO NOTHING", block)
        self.assertIn('SELECT count(*) FROM public."activate_log"', block)
        self.assertIn("target table differs from CloudBase export: activate_log", block)
        self.assertNotIn('UPDATE public."activate_log"', block)
        self.assertNotIn('DELETE FROM public."activate_log"', block)

    def test_import_reconciles_only_the_known_iptv_migration_default(self):
        exported = {
            "tables": {name: [] for name in BUSINESS_TABLES},
            "sequence_state": {name: None for name in SEQUENCE_TABLES},
        }
        exported["tables"]["iptv_config"] = [
            {"key": "mode", "value": "manual", "updated_at": "2026-09-04T04:56:28+08:00"}
        ]
        sql = build_data_import_sql(exported)
        self.assertIn("target.value = 'auto'", sql)
        self.assertIn("ON CONFLICT (key) DO UPDATE", sql)
        self.assertIn("target table differs from CloudBase export: iptv_config", sql)

    def test_migration_paths_start_with_catalog_base_schema(self):
        paths = migration_paths(ROOT)
        self.assertEqual(paths[0].name, "db_bootstrap.sql")
        self.assertEqual(paths[1].name, "base_schema.sql")
        self.assertEqual(tuple(path.name for path in paths[2:]), MIGRATION_ORDER)

    def test_bootstrap_renderer_inlines_the_lock_rpc_and_reads_the_postgrest_password_from_environment(self):
        path = ROOT / "tools" / "selfhosted" / "db_bootstrap.sql"
        rendered = render_migration_sql(path)
        self.assertIn("\\getenv postgrest_db_password POSTGREST_DB_PASSWORD", rendered)
        self.assertIn("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO authenticated", rendered)
        self.assertIn("CREATE OR REPLACE FUNCTION public.iptv_admin_publish_lock", rendered)
        self.assertNotIn("\\ir iptv_publish_lock.sql", rendered)

    def test_renderer_omits_only_marked_cloudbase_storage_block(self):
        path = ROOT / "tools" / "hotupdate_migration.sql"
        rendered = render_migration_sql(path)
        self.assertNotIn("insert into storage.buckets", rendered.lower())
        self.assertNotIn("create policy", rendered.lower())
        self.assertIn("create table if not exists public.hotupdate_pkg", rendered.lower())
        self.assertIn("public.hotupdate_check", rendered.lower())

    def test_schema_migrations_execute_in_order_and_stop_on_first_failure(self):
        calls = []

        class Result:
            returncode = 0

        def executor(path, sql):
            calls.append((path.name, sql))
            return Result()

        applied = apply_schema_migrations("postgresql://staging/otv", ROOT, executor=executor)
        self.assertEqual(applied, tuple(path.name for path in migration_paths(ROOT)))
        self.assertEqual([name for name, _ in calls], list(applied))

        calls.clear()

        def failing_executor(path, sql):
            calls.append(path.name)
            result = Result()
            result.returncode = 1 if path.name == "license_v2_migration.sql" else 0
            return result

        with self.assertRaisesRegex(RuntimeError, "license_v2_migration.sql"):
            apply_schema_migrations("postgresql://staging/otv", ROOT, executor=failing_executor)
        self.assertEqual(calls, ["db_bootstrap.sql", "base_schema.sql", "license_v2_migration.sql"])

    def test_data_import_replaces_only_the_exact_nine_vod_migration_seeds(self):
        exported = {
            "tables": {name: [] for name in BUSINESS_TABLES},
            "sequence_state": {name: None for name in SEQUENCE_TABLES},
        }
        exported["tables"]["vod_source_drafts"] = [
            {"id": f"20000000-0000-0000-0000-{index:012d}", "name": f"source-{index}"}
            for index in range(1, 9)
        ]
        sql = build_data_import_sql(exported)
        blocks = sql.split("DO $mytv_data_import$\n")
        block = next(part.split("END $mytv_data_import$;", 1)[0] for part in blocks if "target table differs from CloudBase export: vod_source_drafts" in part)
        seed_ids = [f"10000000-0000-0000-0000-{index:012d}" for index in range(1, 10)]

        for seed_id in seed_ids:
            self.assertIn(seed_id, block)
        self.assertIn('count(*) FROM public."vod_source_drafts") = 9', block)
        self.assertIn('id NOT IN (', block)
        self.assertIn('DELETE FROM public."vod_source_drafts" WHERE id IN (', block)
        self.assertIn('INSERT INTO public."vod_source_drafts" OVERRIDING SYSTEM VALUE', block)
        self.assertIn("target table differs from CloudBase export: vod_source_drafts", block)
        self.assertNotIn('DELETE FROM public."vod_source_drafts";\n', block)
        self.assertNotIn("TRUNCATE", block.upper())

    def test_iptv_default_mode_reconciliation_does_not_raise_after_upsert(self):
        exported = {
            "tables": {name: [] for name in BUSINESS_TABLES},
            "sequence_state": {name: None for name in SEQUENCE_TABLES},
        }
        exported["tables"]["iptv_config"] = [
            {"key": "mode", "value": "manual", "updated_at": "2026-09-04T04:56:28+08:00"}
        ]
        sql = build_data_import_sql(exported)
        blocks = sql.split("DO $mytv_data_import$\n")
        block = next(part.split("END $mytv_data_import$;", 1)[0] for part in blocks if 'public."iptv_config"' in part)
        self.assertIn("target.value = 'auto'", block)
        self.assertIn(
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;\n"
            "    ELSE\n        INSERT INTO public.\"iptv_config\" OVERRIDING SYSTEM VALUE",
            block,
        )
        self.assertIn("ON CONFLICT DO NOTHING", block)
        self.assertIn("target table differs from CloudBase export: iptv_config", block)

    def test_row_reconciliation_normalizes_export_values_through_target_column_types(self):
        exported = {
            "tables": {name: [] for name in BUSINESS_TABLES},
            "sequence_state": {name: None for name in SEQUENCE_TABLES},
        }
        sql = build_data_import_sql(exported)
        self.assertIn(
            'to_jsonb(existing) = to_jsonb(jsonb_populate_record(NULL::public."licenses", src.row_data))',
            sql,
        )


if __name__ == "__main__":
    unittest.main()
