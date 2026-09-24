#!/usr/bin/env python3
"""Validate a license_gen batch and insert it into the self-hosted PostgreSQL database."""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec


TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))
import license_gen  # noqa: E402

CODE_RE = re.compile(r"^OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$")
CARD_FIELDS = ("code", "plan", "days", "price", "note")
INSERT_SQL = """
INSERT INTO public.licenses (code, plan, days, ticket, note)
VALUES (%s, %s, %s, %s::jsonb, %s)
ON CONFLICT (code) DO NOTHING
""".strip()


def load_public_key():
    """Load the public key paired with the signing key used by license_gen.py."""
    return license_gen.load_pub()


def _read_batch(batch_dir: str | Path, public_key) -> list[tuple[str, str, int, str, str]]:
    directory = Path(batch_dir)
    cards_path = directory / "cards.csv"
    tickets_path = directory / "tickets.jsonl"
    if not cards_path.is_file() or not tickets_path.is_file():
        raise ValueError("batch must contain cards.csv and tickets.jsonl")

    try:
        with cards_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != CARD_FIELDS:
                raise ValueError("cards.csv has an invalid license_gen header")
            cards = list(reader)
        ticket_lines = [line for line in tickets_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        tickets = [json.loads(line) for line in ticket_lines]
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        raise ValueError("batch files are invalid") from exc
    if not cards or len(cards) != len(tickets):
        raise ValueError("cards.csv and tickets.jsonl must contain the same non-empty batch")

    card_by_code: dict[str, Mapping[str, str]] = {}
    for row in cards:
        code = str(row.get("code") or "").strip()
        if not CODE_RE.fullmatch(code) or code in card_by_code:
            raise ValueError("cards.csv contains an invalid or duplicate code")
        card_by_code[code] = row

    entries = []
    seen_tickets: set[str] = set()
    required = {"v", "code", "plan", "days", "issued", "nonce", "sig"}
    for ticket in tickets:
        if not isinstance(ticket, dict) or not required.issubset(ticket):
            raise ValueError("tickets.jsonl contains an invalid ticket")
        code = ticket["code"]
        plan = ticket["plan"]
        if (
            ticket["v"] != license_gen.TICKET_V
            or not isinstance(code, str)
            or not CODE_RE.fullmatch(code)
            or code in seen_tickets
            or not isinstance(plan, str)
            or plan not in license_gen.PLANS
        ):
            raise ValueError("tickets.jsonl contains an invalid or duplicate ticket")
        try:
            days = int(ticket["days"])
            if isinstance(ticket["days"], bool) or days != ticket["days"]:
                raise ValueError
            date.fromisoformat(str(ticket["issued"]))
            nonce = str(ticket["nonce"])
            signature = base64.b64decode(str(ticket["sig"]), validate=True)
            public_key.verify(
                signature,
                license_gen.canonical(code, plan, days, str(ticket["issued"]), nonce).encode("utf-8"),
                ec.ECDSA(__import__("cryptography.hazmat.primitives.hashes", fromlist=["SHA256"]).SHA256()),
            )
        except (InvalidSignature, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"ticket signature or fields are invalid for {code}") from exc

        row = card_by_code.get(code)
        expected_plan = license_gen.PLANS[plan]
        if (
            row is None
            or row.get("plan") != plan
            or int(row.get("days") or -1) != days
            or days != expected_plan["days"]
            or str(row.get("price") or "") != expected_plan["price"]
        ):
            raise ValueError(f"batch card does not match its signed ticket: {code}")
        seen_tickets.add(code)
        entries.append((code, plan, days, json.dumps(ticket, ensure_ascii=False, separators=(",", ":")),
                        str(row.get("note") or "")))

    if seen_tickets != set(card_by_code):
        raise ValueError("cards.csv and tickets.jsonl contain different codes")
    return entries


def connect_selfhosted_database():
    """Connect only to the private Compose PostgreSQL service, never an external host."""
    database = os.environ.get("OTV_LICENSE_IMPORT_DB", "")
    username = os.environ.get("OTV_LICENSE_IMPORT_USER", "")
    password_file = os.environ.get("OTV_LICENSE_IMPORT_PASSWORD_FILE", "")
    if not database or not username or not password_file:
        raise ValueError("self-hosted PostgreSQL credentials are not configured")
    password = Path(password_file).read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError("self-hosted PostgreSQL credentials are not configured")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("the PostgreSQL driver is not installed") from exc
    return psycopg.connect(
        host="postgres", port=5432, dbname=database, user=username, password=password, connect_timeout=15
    )


def import_batch(
    batch_dir: str | Path,
    *,
    connect: Callable[[], object] | None = None,
    public_key=None,
) -> tuple[int, int]:
    key = public_key if public_key is not None else load_public_key()
    entries = _read_batch(batch_dir, key)
    if connect is None:
        connect = connect_selfhosted_database
    connection = connect()
    inserted_count = 0
    with connection:
        with connection.cursor() as cursor:
            for entry in entries:
                cursor.execute(INSERT_SQL, entry)
                inserted_count += cursor.rowcount
    return inserted_count, len(entries)


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Import a signed license_gen batch into self-hosted PostgreSQL")
    parser.add_argument("batch_dir", help="license_gen batch directory containing cards.csv and tickets.jsonl")
    args = parser.parse_args(argv)
    try:
        inserted, validated = import_batch(args.batch_dir)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"license batch import failed: {exc}", file=sys.stderr)
        return 1
    print(f"Inserted {inserted} of {validated} validated license tickets into self-hosted PostgreSQL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
