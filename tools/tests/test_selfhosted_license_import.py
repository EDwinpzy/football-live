import base64
import csv
import importlib
import json
import re
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_RUNNER = ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh"
MIGRATION_SQL = ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql"
RESTORE_MIGRATION = ROOT / "tools" / "selfhosted" / "migrations" / "license_restore_v1.sql"
LICENSE_V2_MIGRATION = ROOT / "tools" / "license_v2_migration.sql"
COMPOSE_FILE = ROOT / "deploy" / "selfhosted" / "compose.yaml"


def _import_tool():
    try:
        return importlib.import_module("tools.selfhosted.import_licenses")
    except ModuleNotFoundError as exc:
        pytest.fail(f"self-hosted license importer is required: {exc}")


def _batch(tmp_path, *, code="OTV-ABCDE-23456", plan="monthly", days=30, note="first, 'batch'"):
    issued = "2026-09-24"
    nonce = "0123456789abcdef"
    payload = {"v": 1, "code": code, "plan": plan, "days": days,
               "issued": issued, "nonce": nonce, "sig": "placeholder"}
    ticket_key = ec.generate_private_key(ec.SECP256R1())
    from tools import license_gen
    payload["sig"] = base64.b64encode(ticket_key.sign(
        license_gen.canonical(code, plan, days, issued, nonce).encode(),
        ec.ECDSA(hashes.SHA256()),
    )).decode("ascii")
    with (tmp_path / "cards.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("code", "plan", "days", "price", "note"))
        writer.writerow((code, plan, days, "9.9", note))
    (tmp_path / "tickets.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return ticket_key.public_key(), payload


class FakeRunner:
    pass


class FakeCursor:
    def __init__(self):
        self.statement = ""
        self.rows = []
        self.rowcounts = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, row):
        self.statement = statement
        self.rows.append(tuple(row))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1


class FakeConnection:
    def __init__(self):
        self.cursor_value = FakeCursor()
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.committed = exc_type is None
        return False

    def cursor(self):
        return self.cursor_value


def test_importer_validates_license_gen_batch_and_writes_selfhosted_postgres(tmp_path, monkeypatch):
    public_key, payload = _batch(tmp_path)
    importer = _import_tool()
    monkeypatch.setattr(importer, "load_public_key", lambda: public_key)
    connection = FakeConnection()

    count = importer.import_batch(tmp_path, connect=lambda: connection)

    assert count == (1, 1)
    assert connection.committed
    assert "insert into public.licenses" in connection.cursor_value.statement.lower()
    assert "on conflict (code) do nothing" in connection.cursor_value.statement.lower()
    assert connection.cursor_value.rows[0][0:3] == (payload["code"], payload["plan"], payload["days"])
    assert json.loads(connection.cursor_value.rows[0][3]) == payload


def test_importer_returns_actual_insert_count_when_codes_already_exist(tmp_path, monkeypatch):
    public_key, _payload = _batch(tmp_path)
    importer = _import_tool()
    monkeypatch.setattr(importer, "load_public_key", lambda: public_key)
    connection = FakeConnection()
    connection.cursor_value.rowcounts = [0]

    inserted, validated = importer.import_batch(tmp_path, connect=lambda: connection)

    assert (inserted, validated) == (0, 1)


def test_connection_uses_dedicated_importer_credentials_instead_of_postgres_bootstrap_env(tmp_path, monkeypatch):
    importer = _import_tool()
    password_file = tmp_path / "license-importer-password"
    password_file.write_text("dedicated-secret\n", encoding="utf-8")
    calls = []
    expected_connection = object()
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(
        connect=lambda **kwargs: calls.append(kwargs) or expected_connection
    ))
    monkeypatch.setenv("PGHOST", "external.example")
    monkeypatch.setenv("PGPORT", "6543")
    monkeypatch.setenv("PGDATABASE", "postgres")
    monkeypatch.setenv("PGUSER", "bootstrap_admin")
    monkeypatch.setenv("PGPASSWORD", "bootstrap-secret")
    monkeypatch.setenv("OTV_LICENSE_IMPORT_DB", "optimal_tv")
    monkeypatch.setenv("OTV_LICENSE_IMPORT_USER", "license_importer")
    monkeypatch.setenv("OTV_LICENSE_IMPORT_PASSWORD_FILE", str(password_file))

    connection = importer.connect_selfhosted_database()

    assert connection is expected_connection
    assert calls == [{
        "host": "postgres", "port": 5432, "dbname": "optimal_tv", "user": "license_importer",
        "password": "dedicated-secret", "connect_timeout": 15,
    }]


def test_admin_generation_reports_inserted_and_validated_license_counts(tmp_path, monkeypatch):
    admin_server = importlib.import_module("tools.admin_server")
    batch_dir = tmp_path / "batch-1"
    batch_dir.mkdir()
    (batch_dir / "cards.csv").write_text("code,plan,days,price,note\n", encoding="utf-8")
    outputs = [
        SimpleNamespace(returncode=0, stdout="批次 batch-1: generated\n", stderr=""),
        SimpleNamespace(
            returncode=0,
            stdout="Inserted 2 of 5 validated license tickets into self-hosted PostgreSQL.\n",
            stderr="",
        ),
    ]
    monkeypatch.setattr(admin_server.subprocess, "run", lambda *_args, **_kwargs: outputs.pop(0))
    monkeypatch.setattr(admin_server, "OUT_ROOT", tmp_path)

    result = admin_server.h_gen({"plan": "monthly", "count": 5})

    assert result["imported"] == 2
    assert result["validated"] == 5


def test_importer_rejects_bad_ticket_signature_before_database_write(tmp_path, monkeypatch):
    public_key, payload = _batch(tmp_path)
    payload["sig"] = base64.b64encode(b"not a signature").decode("ascii")
    (tmp_path / "tickets.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    importer = _import_tool()
    monkeypatch.setattr(importer, "load_public_key", lambda: public_key)
    connection = FakeConnection()

    with pytest.raises(ValueError, match="signature"):
        importer.import_batch(tmp_path, connect=lambda: connection)

    assert not connection.committed
    assert connection.cursor_value.rows == []


def test_importer_rejects_csv_ticket_mismatch_before_database_write(tmp_path, monkeypatch):
    public_key, _payload = _batch(tmp_path)
    with (tmp_path / "cards.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("code", "plan", "days", "price", "note"))
        writer.writerow(("OTV-ABCDE-23456", "monthly", 31, "9.9", "mismatch"))
    importer = _import_tool()
    monkeypatch.setattr(importer, "load_public_key", lambda: public_key)
    connection = FakeConnection()

    with pytest.raises(ValueError, match="batch|ticket|match"):
        importer.import_batch(tmp_path, connect=lambda: connection)

    assert not connection.committed
    assert connection.cursor_value.rows == []


def test_migration_runner_copies_and_hashes_the_restore_migration():
    runner = MIGRATION_RUNNER.read_text(encoding="utf-8")

    assert "selfhosted/migrations/license_restore_v1.sql" in runner, (
        "the runner must stage the restore migration into its sandbox"
    )
    assert 'migration_hash_license_restore=$(sha256_file "$migration_root/tools/selfhosted/migrations/license_restore_v1.sql")' in runner
    assert '-v "license_restore_sha256=$migration_hash_license_restore"' in runner

    # psql variables are interpolated verbatim, so every value must stay hex.
    hashes = re.findall(r'migration_hash_\w+=?=\$\(sha256(?:sum|_file)', runner)
    assert hashes
    assert "printf '%s' \"$hash_value\"" in runner, (
        "hashes are passed to psql as text and must not carry a filename suffix"
    )
    assert 'case "$hash_value" in' in runner
    assert "*[!0123456789abcdef]*|''" in runner


def test_migration_driver_registers_and_runs_the_restore_migration_once():
    migration_sql = MIGRATION_SQL.read_text(encoding="utf-8")

    assert "('license_restore_v1.sql', :'license_restore_sha256')" in migration_sql, (
        "restore must be hashed into the expected-migration table"
    )
    assert migration_sql.count("\\ir migrations/license_restore_v1.sql") == 1, (
        "the restore migration must be executed exactly once"
    )
    assert "VALUES ('license_restore_v1.sql', :'license_restore_sha256')" in migration_sql

    # Its ledger row is written in the same transaction that runs the SQL.
    block = migration_sql.split("\\ir migrations/license_restore_v1.sql", 1)[1]
    head, _, tail = block.partition("COMMIT;")
    assert "INSERT INTO public.schema_migrations" in head
    assert "license_restore_v1.sql" in head
    assert tail.lstrip().startswith("\\endif"), (
        "the restore transaction must close before the next migration begins"
    )
    assert "admin_sold_migration.sql" not in head, (
        "no other migration may share the restore transaction"
    )


def test_legacy_claim_uses_the_function_body_not_the_signature():
    migration_sql = MIGRATION_SQL.read_text(encoding="utf-8")

    assert "pg_catalog.pg_get_functiondef(proc.oid)" in migration_sql
    assert "OTV-RESTORE-MARKER-BEGIN" in migration_sql
    assert "pg_temp.activate_code_supports_restore()" in migration_sql

    # The signature survives only as a schema-completeness check. It is
    # identical before and after restore, so it must not appear in the
    # restore-support decision, which reads the installed body instead.
    signature_entry = (
        "('function public.activate_code(text,text,text,integer,text,text)', "
        "to_regprocedure('public.activate_code(text,text,text,integer,text,text)') IS NOT NULL)"
    )
    assert signature_entry in migration_sql
    restore_decision = migration_sql.split("AS legacy_supports_restore_text", 1)[0]
    decision_block = restore_decision.rsplit("DO $validate_legacy_schema$", 1)
    if len(decision_block) == 2:
        # The signature entry lives in the completeness list, never as the
        # restore gate; the gate is the separate probe result.
        assert "activate_code_supports_restore" not in decision_block[1].split("WHERE", 1)[0]

    # Unsupported restore must leave the ledger row empty so the pending path
    # installs it, rather than hard-failing or silently claiming it.
    assert "WHERE :'legacy_supports_restore_text' = 'true'" in migration_sql
    assert "OR version <> 'license_restore_v1.sql'" in migration_sql


def test_restore_lives_outside_license_v2_so_legacy_claims_cannot_skip_it():
    restore_sql = RESTORE_MIGRATION.read_text(encoding="utf-8")
    license_v2 = LICENSE_V2_MIGRATION.read_text(encoding="utf-8")

    assert "OTV-RESTORE-MARKER-BEGIN" in restore_sql
    assert "'restore'" in restore_sql, "the installed body must accept the restore action"
    assert "OTV-RESTORE-MARKER-BEGIN" not in license_v2
    assert "v_action = 'restore'" not in license_v2, (
        "license_v2_migration.sql must not be the sole carrier of restore support"
    )

    # The marker must be inside the function body, because the migrator probes
    # the installed source rather than the file on disk.
    assert "OTV-RESTORE-MARKER-BEGIN" in restore_sql.split("$function$")[1]


def test_restore_migration_replaces_the_function_and_keeps_its_privileges():
    restore_sql = RESTORE_MIGRATION.read_text(encoding="utf-8")

    assert "create or replace function public.activate_code(" in restore_sql.lower()
    assert "security definer" in restore_sql
    assert "set search_path to 'public'" in restore_sql
    assert (
        "revoke all on function public.activate_code(text,text,text,integer,text,text) from public"
        in restore_sql
    )
    assert (
        "grant execute on function public.activate_code(text,text,text,integer,text,text) "
        "to anon, authenticated" in restore_sql
    )


def test_compose_schema_migrations_service_is_safe_for_explicit_run():
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    service = compose.split("  schema-migrations:", 1)[1].split("\n  auth:", 1)[0]

    # `docker compose run --rm schema-migrations` must not depend on the
    # one-shot container being recreated by a later `up`.
    assert 'restart: "no"' in service
    assert "OTV_LICENSE_IMPORT_PASSWORD_FILE" not in service, (
        "the importer password must arrive through a file, never the environment"
    )
    assert "LICENSE_IMPORTER_PASSWORD_FILE: /run/secrets/license-importer-db-password" in service
    assert "license-importer-db-password" in service

    # The new migration must be visible inside the container, otherwise the
    # runner fails to stage it.
    assert (
        "../../tools/selfhosted/migrations/license_restore_v1.sql:"
        "/app/tools/selfhosted/migrations/license_restore_v1.sql:ro" in service
    )

    # It waits for the database rather than for any API service.
    assert "postgres:" in service
    assert "condition: service_healthy" in service
    for other in ("postgrest:", "business-api:", "admin:", "web-api:"):
        assert other not in service, f"schema-migrations must not depend on {other}"
