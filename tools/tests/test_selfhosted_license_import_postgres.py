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
PG_TEST_URL = os.environ.get("PG_TEST_URL", "")
PSQL = shutil.which("psql")
SHELL = shutil.which("sh") or shutil.which("bash")


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

        role_version = run_psql(
            "-Atc",
            "SELECT count(*) FROM public.schema_migrations WHERE version = 'license_importer_role_v1'",
            env=environment,
        )
        assert role_version.returncode == 0
        assert role_version.stdout.strip() == "1"

        login_smoke = run_psql("-f", str(IMPORT_SMOKE), env=importer_environment)
        assert login_smoke.returncode == 0, f"license_importer PostgreSQL privilege smoke test failed for {database}; output suppressed"
