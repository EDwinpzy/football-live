import base64
import csv
import importlib
import json
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

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def executemany(self, statement, rows):
        self.statement = statement
        self.rows = list(rows)


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

    assert count == 1
    assert connection.committed
    assert "insert into public.licenses" in connection.cursor_value.statement.lower()
    assert "on conflict (code) do nothing" in connection.cursor_value.statement.lower()
    assert connection.cursor_value.rows[0][0:3] == (payload["code"], payload["plan"], payload["days"])
    assert json.loads(connection.cursor_value.rows[0][3]) == payload


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
