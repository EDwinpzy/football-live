#!/usr/bin/env python3
"""Render a private, read-only PostgreSQL reconciliation query for a CloudBase export.

Run the resulting file with ``psql -X -qAt -v ON_ERROR_STOP=1 -f FILE``. Its
only result row is a JSON report; source records never appear in that result.
The SQL file itself contains source records and must be kept private.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping

try:
    from .import_cloudbase import BUSINESS_TABLES, SEQUENCE_TABLES
except ImportError:  # Direct CLI execution.
    from import_cloudbase import BUSINESS_TABLES, SEQUENCE_TABLES


def _sql_literal(value: str) -> str:
    """Quote a literal after the script has enabled standard_conforming_strings."""
    return "'" + value.replace("'", "''") + "'"


def _validate_export(export: Mapping[str, object]) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if not isinstance(export, Mapping):
        raise ValueError("export must be an object")
    tables = export.get("tables")
    sequences = export.get("sequence_state")
    if not isinstance(tables, Mapping) or set(tables) != set(BUSINESS_TABLES):
        raise ValueError("export table inventory does not match the allowlist")
    if not isinstance(sequences, Mapping) or set(sequences) != set(SEQUENCE_TABLES):
        raise ValueError("export sequence inventory does not match the allowlist")
    for name, rows in tables.items():
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not row or
            any(not isinstance(key, str) or not key for key in row)
            for row in rows
        ):
            raise ValueError(f"invalid rows for table {name}")
    for name, value in sequences.items():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"invalid sequence state for {name}")
    try:
        json.dumps(tables, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("table rows contain non-JSON values") from exc
    return tables, sequences


def build_reconciliation_sql(export: Mapping[str, object]) -> str:
    """Compare typed row multisets and sequence values; emit one redacted JSON row.

    ``EXCEPT ALL`` catches duplicate and same-count content mismatches. A source
    row must contain exactly the target table's column names, so PostgreSQL
    cannot silently ignore an unexpected export field during normalization.
    """
    tables, sequences = _validate_export(export)
    ctes: list[str] = []
    table_pairs: list[str] = []
    sequence_pairs: list[str] = []
    table_matches: list[str] = []
    sequence_matches: list[str] = []

    for index, name in enumerate(BUSINESS_TABLES):
        target = f'public."{name}"'
        payload = _sql_literal(json.dumps(tables[name], ensure_ascii=True, separators=(",", ":")))
        src = f"source_{index}"
        dst = f"target_{index}"
        check = f"table_{index}"
        # Cast through the target composite type before comparing JSONB values.
        # This normalizes timestamps, numerics, UUIDs, and other PG column types.
        ctes.extend((
            f"{src}_raw AS (SELECT value AS row_data FROM jsonb_array_elements({payload}::jsonb))",
            f"{src} AS (SELECT to_jsonb(jsonb_populate_record(NULL::{target}, row_data)) AS row_data FROM {src}_raw)",
            f"{dst} AS (SELECT to_jsonb(t) AS row_data FROM {target} AS t)",
            f"{check} AS (SELECT "
            f"(SELECT count(*) FROM {src}) AS source_count, "
            f"(SELECT count(*) FROM {dst}) AS target_count, "
            f"NOT EXISTS (SELECT row_data FROM {src} EXCEPT ALL SELECT row_data FROM {dst}) "
            f"AND NOT EXISTS (SELECT row_data FROM {dst} EXCEPT ALL SELECT row_data FROM {src}) "
            f"AND NOT EXISTS (SELECT 1 FROM {src}_raw AS r WHERE "
            f"(SELECT count(*) FROM jsonb_object_keys(r.row_data)) <> "
            f"(SELECT count(*) FROM pg_attribute WHERE attrelid = '{target}'::regclass "
            f"AND attnum > 0 AND NOT attisdropped) OR EXISTS "
            f"(SELECT 1 FROM jsonb_object_keys(r.row_data) AS field(name) "
            f"WHERE NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = '{target}'::regclass "
            f"AND attnum > 0 AND NOT attisdropped AND attname = field.name))) AS is_match)",
        ))
        table_pairs.extend((_sql_literal(name),
            f"(SELECT jsonb_build_object('source', source_count, 'target', target_count, 'match', is_match) FROM {check})"))
        table_matches.append(f"(SELECT is_match FROM {check})")

    for index, name in enumerate(SEQUENCE_TABLES):
        source = sequences[name]
        expected = "NULL::bigint" if source is None else f"{source}::bigint"
        check = f"sequence_{index}"
        ctes.append(
            f"{check} AS (SELECT EXISTS (SELECT 1 FROM pg_sequences "
            f"WHERE schemaname = 'public' AND sequencename = {_sql_literal(name)} "
            f"AND last_value IS NOT DISTINCT FROM {expected}) AS is_match)"
        )
        sequence_pairs.extend((_sql_literal(name), f"(SELECT is_match FROM {check})"))
        sequence_matches.append(f"(SELECT is_match FROM {check})")

    all_matches = " AND ".join(table_matches + sequence_matches)
    return (
        "\\set ON_ERROR_STOP on\n"
        "BEGIN READ ONLY;\n"
        "SET LOCAL standard_conforming_strings = on;\n"
        "WITH " + ",\n".join(ctes) + "\n"
        "SELECT jsonb_build_object("
        "'tables', jsonb_build_object(" + ", ".join(table_pairs) + "), "
        "'sequences', jsonb_build_object(" + ", ".join(sequence_pairs) + "), "
        "'match', " + all_matches + ")::text;\n"
        "COMMIT;\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render private, read-only database reconciliation SQL")
    parser.add_argument("--input", required=True, type=Path, help="CloudBase JSON export")
    parser.add_argument("--output", required=True, type=Path, help="private SQL output path")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output.is_relative_to(root):
        parser.error("reconciliation SQL must be stored outside the repository")
    with args.input.open(encoding="utf-8") as stream:
        export = json.load(stream)
    sql = build_reconciliation_sql(export)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(sql)
    try:
        os.chmod(output, 0o600)
    except OSError:
        pass
    print(json.dumps({"output": str(output), "tables": len(BUSINESS_TABLES), "sequences": len(SEQUENCE_TABLES)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
