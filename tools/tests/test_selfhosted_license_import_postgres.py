import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_RUNNER = ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh"
IMPORT_SMOKE = ROOT / "tools" / "selfhosted" / "license_importer_smoke.psql"
MIGRATION_SQL = ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql"
IMPORTER_POLICY = ROOT / "tools" / "selfhosted" / "license_importer_role.sql"
RESTORE_MIGRATION = ROOT / "tools" / "selfhosted" / "migrations" / "license_restore_v1.sql"
LEGACY_NO_RESTORE_FIXTURE = ROOT / "tools" / "tests" / "fixtures" / "legacy_activate_code_no_restore.sql"
PG_TEST_URL = os.environ.get("PG_TEST_URL", "")
PSQL = shutil.which("psql")
SHELL = shutil.which("sh") or shutil.which("bash")


def test_migration_runner_tracks_source_hashes_and_refuses_unsafe_legacy_replay():
    runner = MIGRATION_RUNNER.read_text(encoding="utf-8")
    migration_sql = MIGRATION_SQL.read_text(encoding="utf-8")

    assert "sha256sum" in runner
    assert '"db_bootstrap_sha256=$migration_hash_db_bootstrap"' in runner
    assert "sql_sha256" in migration_sql
    assert "legacy_schema" in migration_sql
    assert "refusing to replay" in migration_sql.lower()


def test_restore_support_is_a_separate_ledger_migration_not_smuggled_into_license_v2():
    restore_sql = RESTORE_MIGRATION.read_text(encoding="utf-8")
    license_v2 = (ROOT / "tools" / "license_v2_migration.sql").read_text(encoding="utf-8")
    migration_sql = MIGRATION_SQL.read_text(encoding="utf-8")
    runner = MIGRATION_RUNNER.read_text(encoding="utf-8")

    # The restore body must live in its own file so a legacy database whose
    # ledger already covers license_v2_migration.sql still runs it once.
    assert "OTV-RESTORE-MARKER-BEGIN" in restore_sql
    assert "OTV-RESTORE-MARKER-END" in restore_sql
    assert "restore" not in license_v2.lower(), (
        "license_v2_migration.sql must not carry the only copy of restore support"
    )

    # It is registered, executed and hashed like any other migration.
    assert "'license_restore_v1.sql'" in migration_sql
    assert "\\ir migrations/license_restore_v1.sql" in migration_sql
    assert "selfhosted/migrations/license_restore_v1.sql" in runner
    assert "license_restore_sha256=" in runner

    # The SQL and its ledger row stay inside one transaction: BEGIN, then the
    # \ir, then the INSERT, then COMMIT with nothing interleaved.
    restore_block = migration_sql.split(
        "\\ir migrations/license_restore_v1.sql", 1
    )[1].split("COMMIT;", 1)[0]
    assert "INSERT INTO public.schema_migrations" in restore_block
    assert "'license_restore_v1.sql'" in restore_block
    restore_pending_block = migration_sql.split(
        "SELECT NOT EXISTS (\n    SELECT 1 FROM public.schema_migrations "
        "WHERE version = 'license_restore_v1.sql'", 1
    )[1].split("\\ir migrations/license_restore_v1.sql", 1)[0]
    assert "BEGIN;" in restore_pending_block


def test_legacy_claim_inspects_the_function_body_instead_of_the_signature():
    migration_sql = MIGRATION_SQL.read_text(encoding="utf-8")

    # A signature probe cannot distinguish a legacy body from a current one,
    # so the claim must read the installed source via pg_get_functiondef.
    assert "pg_get_functiondef" in migration_sql
    assert "OTV-RESTORE-MARKER-BEGIN" in migration_sql
    assert "activate_code_supports_restore" in migration_sql
    # The signature may remain as a schema-completeness check, but the restore
    # decision must come from the probe result, not from that signature.
    # The probe result is read as text on purpose: \gset on a boolean expands
    # to the bare literal t/f, which is not a valid SQL expression.
    assert "AS legacy_supports_restore_text \\gset" in migration_sql

    # A legacy database that fails the probe must not be recorded as migrated;
    # its ledger row is withheld so the pending path installs the function.
    assert "WHERE :'legacy_supports_restore_text' = 'true'" in migration_sql
    assert "version <> 'license_restore_v1.sql'" in migration_sql


def test_restore_marker_lives_inside_the_installed_function_body():
    restore_sql = RESTORE_MIGRATION.read_text(encoding="utf-8")
    fixture_sql = LEGACY_NO_RESTORE_FIXTURE.read_text(encoding="utf-8")

    # The probe reads the installed body, so the marker must be inside the
    # function text and the fixture must genuinely lack it.
    body = restore_sql.split("$function$")[1]
    assert "OTV-RESTORE-MARKER-BEGIN" in body
    assert "OTV-RESTORE-MARKER-END" in body
    assert "OTV-RESTORE-MARKER" not in fixture_sql.split("$function$")[1]
    assert "activate_code" in fixture_sql


def test_importer_policy_revokes_public_execute_but_preserves_explicit_client_rpcs():
    policy = IMPORTER_POLICY.read_text(encoding="utf-8").lower()

    assert "revoke all privileges on all functions in schema public from public" in policy
    for rpc in (
        "activate_code(text,text,text,integer,text,text)",
        "hotupdate_check(integer,integer)",
        "hotupdate_report(text,integer,integer,integer,text,text)",
        "announcement_latest()",
        "vod_source_public_check(bigint)",
    ):
        assert rpc in policy
    assert "license_importer has execute privileges on public functions" in policy


def _local_test_database_environment(database_url: str) -> tuple[dict[str, str], str]:
    parsed = urlparse(database_url)
    database = unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise AssertionError("PG_TEST_URL must use postgres:// or postgresql://")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise AssertionError("PG_TEST_URL is restricted to a local PostgreSQL server")
    if not database.startswith("mytv_test_"):
        raise AssertionError("PG_TEST_URL must target a dedicated mytv_test_* database")
    if not parsed.username:
        raise AssertionError("PG_TEST_URL must include a PostgreSQL administrator username")

    environment = os.environ.copy()
    environment.update(
        {
            "PGHOST": parsed.hostname,
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": unquote(parsed.username),
            "PGPASSWORD": unquote(parsed.password or ""),
            "PGDATABASE": database,
            "POSTGRES_USER": unquote(parsed.username),
            "POSTGRES_PASSWORD": unquote(parsed.password or ""),
            "POSTGRES_DB": database,
            "POSTGREST_DB_PASSWORD": secrets.token_urlsafe(32),
            "LICENSE_IMPORTER_PASSWORD": secrets.token_urlsafe(32),
        }
    )
    for key, values in parse_qs(parsed.query).items():
        if key == "sslmode" and values:
            environment["PGSSLMODE"] = values[-1]
    return environment, database


@pytest.mark.skipif(
    not PG_TEST_URL or not PSQL or not SHELL,
    reason="set PG_TEST_URL to a local mytv_test_* database and install psql plus a POSIX shell for PostgreSQL integration coverage",
)
def test_license_import_role_insert_duplicate_read_denials_and_updates():
    environment, database = _local_test_database_environment(PG_TEST_URL)
    environment["MIGRATION_TOOLS_ROOT"] = str(ROOT / "tools")
    importer_environment = environment.copy()
    importer_environment["PGUSER"] = "license_importer"
    importer_environment["PGPASSWORD"] = environment["LICENSE_IMPORTER_PASSWORD"]

    def run_psql(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert PSQL is not None
        return subprocess.run(
            [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert SHELL is not None
    with tempfile.TemporaryDirectory(prefix="mytv-license-import-test-") as tmp:
        password_file = Path(tmp) / "license-importer-db-password"
        password_file.write_text(environment["LICENSE_IMPORTER_PASSWORD"], encoding="utf-8")
        if os.name == "posix":
            password_file.chmod(0o600)
        environment["LICENSE_IMPORTER_PASSWORD_FILE"] = str(password_file)

        first_migration = subprocess.run(
            [SHELL, str(MIGRATION_RUNNER)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert first_migration.returncode == 0, "local PostgreSQL schema migration failed; output suppressed to protect test credentials"

        second_migration = subprocess.run(
            [SHELL, str(MIGRATION_RUNNER)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert second_migration.returncode == 0, "reapplying local PostgreSQL migrations failed; output suppressed to protect test credentials"

        recorded_version = run_psql(
            "-Atc",
            "SELECT count(*) FROM public.schema_migrations WHERE version = 'license_v2_migration.sql'",
            env=environment,
        )
        assert recorded_version.returncode == 0
        assert recorded_version.stdout.strip() == "1"

        restore_version = run_psql(
            "-Atc",
            "SELECT count(*) FROM public.schema_migrations WHERE version = 'license_restore_v1.sql'",
            env=environment,
        )
        assert restore_version.returncode == 0
        assert restore_version.stdout.strip() == "1", "restore must be tracked as its own ledger entry"

        restore_body = run_psql(
            "-Atc",
            "SELECT count(*) FROM pg_catalog.pg_proc proc "
            "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = proc.pronamespace "
            "WHERE nsp.nspname = 'public' AND proc.proname = 'activate_code' AND proc.pronargs = 6 "
            "AND pg_catalog.pg_get_functiondef(proc.oid) LIKE '%OTV-RESTORE-MARKER-BEGIN%'",
            env=environment,
        )
        assert restore_body.returncode == 0
        assert restore_body.stdout.strip() == "1", "the installed activate_code body must carry restore support"

        role_version = run_psql(
            "-Atc",
            "SELECT count(*) FROM public.schema_migrations WHERE version = 'license_importer_role_v1'",
            env=environment,
        )
        assert role_version.returncode == 0
        assert role_version.stdout.strip() == "1"

        login_smoke = run_psql("-f", str(IMPORT_SMOKE), env=importer_environment)
        assert login_smoke.returncode == 0, f"license_importer PostgreSQL privilege smoke test failed for {database}; output suppressed"

        public_api_check = run_psql(
            "-Atc",
            "SELECT "
            "has_function_privilege('anon', 'public.activate_code(text,text,text,integer,text,text)', 'EXECUTE') "
            "AND has_function_privilege('anon', 'public.hotupdate_check(integer,integer)', 'EXECUTE') "
            "AND has_function_privilege('anon', 'public.hotupdate_report(text,integer,integer,integer,text,text)', 'EXECUTE') "
            "AND has_function_privilege('anon', 'public.announcement_latest()', 'EXECUTE') "
            "AND has_function_privilege('anon', 'public.vod_source_public_check(bigint)', 'EXECUTE')",
            env=environment,
        )
        assert public_api_check.returncode == 0
        assert public_api_check.stdout.strip() == "t", "revoking PUBLIC must preserve the explicit anonymous client RPC grants"

        public_grant = run_psql(
            "-c",
            "GRANT EXECUTE ON FUNCTION public.admin_mark_sold(text, boolean) TO PUBLIC",
            env=environment,
        )
        assert public_grant.returncode == 0
        acl_reconcile = subprocess.run(
            [SHELL, str(MIGRATION_RUNNER)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert acl_reconcile.returncode == 0, "reconciling legacy PUBLIC function grants failed"

        restricted_admin = run_psql(
            "-Atc",
            "SELECT NOT has_function_privilege(" 
            "'license_importer', 'public.admin_mark_sold(text,boolean)', 'EXECUTE')",
            env=environment,
        )
        assert restricted_admin.returncode == 0
        assert restricted_admin.stdout.strip() == "t", "PUBLIC EXECUTE must not make the importer an admin RPC caller"

        importer_admin_call = run_psql(
            "-c",
            "SELECT set_config('request.jwt.claims', '{\"sub\":\"2094843486709620737\"}', false); "
            "SELECT public.admin_mark_sold('nonexistent-importer-probe', true)",
            env=importer_environment,
        )
        assert importer_admin_call.returncode != 0
        assert "permission denied" in importer_admin_call.stderr.lower()

        authenticated_admin = run_psql(
            "-Atc",
            "SELECT has_function_privilege(" 
            "'authenticated', 'public.admin_mark_sold(text,boolean)', 'EXECUTE') "
            "AND has_function_privilege('authenticated', 'public.admin_stats()', 'EXECUTE')",
            env=environment,
        )
        assert authenticated_admin.returncode == 0
        assert authenticated_admin.stdout.strip() == "t", "authenticated admin RPC grants must survive PUBLIC revocation"

        migration_inputs = (
            "selfhosted/apply_schema_migrations.psql",
            "selfhosted/db_bootstrap.sql",
            "selfhosted/iptv_publish_lock.sql",
            "selfhosted/base_schema.sql",
            "selfhosted/license_importer_role.sql",
            "selfhosted/license_importer_smoke.psql",
            "selfhosted/migrations/license_restore_v1.sql",
            "selfhosted/migrations/vod_sources_replace_v1.sql",
            "license_v2_migration.sql",
            "license_note_migration.sql",
            "admin_sold_migration.sql",
            "hotupdate_migration.sql",
            "announcement_migration.sql",
            "iptv_migration.sql",
            "vod_sources_migration.sql",
            "pg_cu_optimization_migration.sql",
        )
        changed_tools_root = Path(tmp) / "changed-tools"
        for relative_path in migration_inputs:
            source = ROOT / "tools" / relative_path
            destination = changed_tools_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        changed_migration = changed_tools_root / "license_note_migration.sql"
        changed_migration.write_text(
            changed_migration.read_text(encoding="utf-8") + "\n-- changed after it was recorded\n",
            encoding="utf-8",
        )
        changed_environment = environment.copy()
        changed_environment["MIGRATION_TOOLS_ROOT"] = str(changed_tools_root)
        changed_source_run = subprocess.run(
            [SHELL, str(MIGRATION_RUNNER)],
            env=changed_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert changed_source_run.returncode != 0, "a recorded migration must reject changed SQL bytes"

        legacy_code = f"legacy-migration-{secrets.token_hex(6)}"
        legacy_function = run_psql(
            "-f",
            str(LEGACY_NO_RESTORE_FIXTURE),
            env=environment,
        )
        assert legacy_function.returncode == 0, "could not install the pre-restore legacy fixture"

        legacy_marker = run_psql(
            "-c",
            "INSERT INTO public.licenses "
            "(code, plan, days, ticket, note, created_at, updated_at) "
            f"VALUES ('{legacy_code}', 'monthly', 30, '{{}}'::jsonb, 'preserve-me', "
            "'2000-01-01 00:00:00+00', '2001-01-01 00:00:00+00') "
            "ON CONFLICT (code) DO UPDATE SET updated_at = EXCLUDED.updated_at; "
            "DELETE FROM public.vod_source_drafts "
            "WHERE id = '10000000-0000-0000-0000-000000000001'; "
            "DROP TABLE public.schema_migrations",
            env=environment,
        )
        assert legacy_marker.returncode == 0

        legacy_recovery = subprocess.run(
            [SHELL, str(MIGRATION_RUNNER)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert legacy_recovery.returncode == 0, "a complete legacy schema without a ledger should be baselined safely"

        # The regression this guards: a legacy database already has the v2
        # activate_code signature, so a signature-only check would record every
        # migration as applied and permanently skip restore. After the upgrade
        # the body must carry the restore branch and be in the ledger.
        legacy_restored = run_psql(
            "-Atc",
            "SELECT count(*) FROM pg_catalog.pg_proc proc "
            "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = proc.pronamespace "
            "WHERE nsp.nspname = 'public' AND proc.proname = 'activate_code' AND proc.pronargs = 6 "
            "AND pg_catalog.pg_get_functiondef(proc.oid) LIKE '%OTV-RESTORE-MARKER-BEGIN%'",
            env=environment,
        )
        assert legacy_restored.returncode == 0
        assert legacy_restored.stdout.strip() == "1", (
            "a legacy database without restore support must be upgraded, not claimed as migrated"
        )

        legacy_restore_ledger = run_psql(
            "-Atc",
            "SELECT count(*) FROM public.schema_migrations WHERE version = 'license_restore_v1.sql'",
            env=environment,
        )
        assert legacy_restore_ledger.returncode == 0
        assert legacy_restore_ledger.stdout.strip() == "1", (
            "restore must be recorded exactly once after the legacy upgrade"
        )

        legacy_restore_call = run_psql(
            "-Atc",
            "SELECT public.activate_code(null, repeat('a', 64), '127.0.0.1', 2, 'restore', null) ->> 'ret'",
            env=environment,
        )
        assert legacy_restore_call.returncode == 0, "action=restore must not fail after the legacy upgrade"
        assert legacy_restore_call.stdout.strip() == "404", (
            "an unbound device must be reported as restore_not_found rather than an error"
        )

        preserved_state = run_psql(
            "-Atc",
            "SELECT extract(epoch FROM updated_at)::bigint "
            f"FROM public.licenses WHERE code = '{legacy_code}'; "
            "SELECT count(*) FROM public.vod_source_drafts "
            "WHERE id = '10000000-0000-0000-0000-000000000001'; "
            "SELECT count(*) FROM public.schema_migrations "
            "WHERE version IN ('db_bootstrap.sql', 'base_schema.sql', 'license_v2_migration.sql', "
            "'license_note_migration.sql', 'license_restore_v1.sql', 'admin_sold_migration.sql', "
            "'hotupdate_migration.sql', "
            "'announcement_migration.sql', 'iptv_migration.sql', 'vod_sources_migration.sql', "
            "'pg_cu_optimization_migration.sql')",
            env=environment,
        )
        assert preserved_state.returncode == 0
        assert preserved_state.stdout.splitlines() == ["978307200", "0", "11"], (
            "legacy baseline must keep updated_at and the deleted source unchanged while recording known migrations"
        )
