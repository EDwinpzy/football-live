import base64
import csv
import importlib
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[2]


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
