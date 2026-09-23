#!/usr/bin/env python3
"""Schema and integrity helpers for the CloudBase-to-PostgreSQL migration."""

from __future__ import annotations

import subprocess
import json
import os
import argparse
import shutil
from pathlib import Path
from typing import Mapping


MIGRATION_ORDER = (
    "license_v2_migration.sql",
    "license_note_migration.sql",
    "admin_sold_migration.sql",
    "hotupdate_migration.sql",
    "announcement_migration.sql",
    "iptv_migration.sql",
    "vod_sources_migration.sql",
    "pg_cu_optimization_migration.sql",
)

OMIT_BEGIN = "-- SELFHOSTED-OMIT-BEGIN:"
OMIT_END = "-- SELFHOSTED-OMIT-END"

BUSINESS_TABLES = (
    "licenses",
    "activate_log",
    "announcements",
    "hotupdate_log",
    "hotupdate_pkg",
    "iptv_admin_log",
    "iptv_channel",
    "iptv_config",
    "iptv_group",
    "iptv_line",
    "iptv_version",
    "vod_source_drafts",
    "vod_source_versions",
)

DATA_IMPORT_ORDER = (
    "licenses",
    "activate_log",
    "announcements",
    "hotupdate_pkg",
    "hotupdate_log",
    "iptv_admin_log",
    "iptv_group",
    "iptv_channel",
    "iptv_line",
    "iptv_config",
    "iptv_version",
    "vod_source_drafts",
    "vod_source_versions",
)

VOD_MIGRATION_SEED_IDS = tuple(
    f"10000000-0000-0000-0000-{index:012d}" for index in range(1, 10)
)

SEQUENCE_TABLES = {
    "activate_log_id_seq": "activate_log",
    "announcements_id_seq": "announcements",
    "hotupdate_log_id_seq": "hotupdate_log",
    "hotupdate_pkg_id_seq": "hotupdate_pkg",
    "iptv_admin_log_id_seq": "iptv_admin_log",
    "iptv_channel_id_seq": "iptv_channel",
    "iptv_group_id_seq": "iptv_group",
    "iptv_line_id_seq": "iptv_line",
    "iptv_version_id_seq": "iptv_version",
}


def _tcb_command_prefix() -> list[str]:
    if os.name == "nt":
        app_data = Path(os.environ.get("APPDATA", ""))
        cli_entry = app_data / "npm" / "node_modules" / "@cloudbase" / "cli" / "bin" / "tcb"
        node = shutil.which("node")
        if cli_entry.is_file() and node:
            return [node, str(cli_entry)]
        return ["tcb.cmd"]
    return ["tcb"]


def migration_paths(repository_root: str | Path) -> tuple[Path, ...]:
    root = Path(repository_root)
    paths = (
        root / "tools" / "selfhosted" / "db_bootstrap.sql",
        root / "tools" / "selfhosted" / "base_schema.sql",
    ) + tuple(
        root / "tools" / name for name in MIGRATION_ORDER
    )
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing migration file(s): " + ", ".join(missing))
    return paths


def render_migration_sql(path: str | Path) -> str:
    """Remove explicitly marked CloudBase-only storage statements."""
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    if path.name == "db_bootstrap.sql":
        lock_path = path.parent / "iptv_publish_lock.sql"
        if not lock_path.is_file():
            raise FileNotFoundError(f"missing migration file(s): {lock_path.name}")
        source = source.replace("\\ir iptv_publish_lock.sql", lock_path.read_text(encoding="utf-8"))
    output: list[str] = []
    skipping = False
    begin_count = end_count = 0
    for line in source.splitlines():
        if line.startswith(OMIT_BEGIN):
            if skipping:
                raise ValueError(f"nested self-hosted omission marker in {path.name}")
            skipping = True
            begin_count += 1
            continue
        if line.strip() == OMIT_END:
            if not skipping:
                raise ValueError(f"unmatched self-hosted omission marker in {path.name}")
            skipping = False
            end_count += 1
            continue
        if not skipping:
            output.append(line)
    if skipping or begin_count != end_count:
        raise ValueError(f"unclosed self-hosted omission marker in {path.name}")
    return "\n".join(output) + "\n"


def _execute_psql(database_url: str, path: Path, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", database_url, "-X", "-v", "ON_ERROR_STOP=1", "--single-transaction"],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )


def apply_schema_migrations(
    database_url: str,
    repository_root: str | Path,
    *,
    executor=None,
) -> tuple[str, ...]:
    """Apply the base schema and existing migrations, one transaction each."""
    if not database_url:
        raise ValueError("database URL is required")
    execute = executor or (lambda path, sql: _execute_psql(database_url, path, sql))
    applied: list[str] = []
    for path in migration_paths(repository_root):
        result = execute(path, render_migration_sql(path))
        if result.returncode:
            raise RuntimeError(f"schema migration failed: {path.name}")
        applied.append(path.name)
    return tuple(applied)

def build_reconciliation_report(
    source_counts: Mapping[str, int],
    target_counts: Mapping[str, int],
    sequence_state: Mapping[str, int],
) -> dict[str, object]:
    names = sorted(set(source_counts) | set(target_counts))
    tables = {
        name: {
            "source": source_counts.get(name),
            "target": target_counts.get(name),
            "match": name in source_counts and name in target_counts and source_counts[name] == target_counts[name],
        }
        for name in names
    }
    return {
        "tables": tables,
        "mismatched_tables": [name for name in names if not tables[name]["match"]],
        "sequences": {name: sequence_state[name] for name in sorted(sequence_state)},
    }


def _cli_rows(result: Mapping[str, object]) -> list[list[object]]:
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("CloudBase CLI result is missing data")
    rows = data.get("Rows", [])
    if not isinstance(rows, list):
        raise ValueError("CloudBase CLI result rows are invalid")
    decoded: list[list[object]] = []
    for row in rows:
        cells = json.loads(row) if isinstance(row, str) else row
        if not isinstance(cells, list):
            raise ValueError("CloudBase CLI returned an invalid row")
        decoded.append(cells)
    return decoded


def parse_tcb_export_result(result: Mapping[str, object]) -> list[dict[str, object]]:
    """Decode a tcb --json aggregate query into JSON objects."""
    rows = _cli_rows(result)
    if len(rows) != 1 or len(rows[0]) != 1:
        raise ValueError("CloudBase export query returned an unexpected shape")
    payload = rows[0][0]
    records = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError("CloudBase export payload is not a list of records")
    return records


def export_cloudbase_data(environment_id: str, *, runner=None) -> dict[str, object]:
    """Read all business rows and public sequence states using the authenticated tcb CLI."""
    if not environment_id or any(ch.isspace() for ch in environment_id):
        raise ValueError("CloudBase environment id is required")
    run = runner or subprocess.run
    tcb = _tcb_command_prefix()
    tables: dict[str, list[dict[str, object]]] = {}
    for table in BUSINESS_TABLES:
        sql = (
            "SELECT COALESCE(json_agg(to_jsonb(t)), '[]'::json)::text AS payload "
            f'FROM public."{table}" AS t'
        )
        result = run(
            [*tcb, "db", "execute", "-e", environment_id, "--sql", sql, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"CloudBase export query failed for table {table}")
        output = result.stdout
        json_start = output.find("{")
        if json_start < 0:
            raise RuntimeError(f"CloudBase export returned an invalid response for table {table}")
        try:
            envelope = json.loads(output[json_start:])
            tables[table] = parse_tcb_export_result(envelope)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"CloudBase export returned invalid rows for table {table}") from exc

    sequence_sql = (
        "SELECT sequencename, last_value FROM pg_sequences "
        "WHERE schemaname = 'public' ORDER BY sequencename"
    )
    result = run(
        [*tcb, "db", "execute", "-e", environment_id, "--sql", sequence_sql, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise RuntimeError("CloudBase sequence-state query failed")
    json_start = result.stdout.find("{")
    if json_start < 0:
        raise RuntimeError("CloudBase sequence-state query returned an invalid response")
    try:
        sequence_rows = _cli_rows(json.loads(result.stdout[json_start:]))
        sequence_state = {
            str(row[0]): (None if row[1] in (None, "null", "") else int(row[1]))
            for row in sequence_rows
            if len(row) == 2
        }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError("CloudBase sequence-state query returned invalid rows") from exc
    if set(sequence_state) != set(SEQUENCE_TABLES):
        raise RuntimeError("CloudBase sequence-state inventory does not match the expected schema")
    return {
        "schema_version": 1,
        "source_env": environment_id,
        "tables": tables,
        "sequence_state": sequence_state,
    }


def build_data_import_sql(export: Mapping[str, object]) -> str:
    """Build an atomic exact reconciliation that only inserts missing source rows."""
    tables = export.get("tables")
    sequences = export.get("sequence_state")
    if not isinstance(tables, Mapping) or not isinstance(sequences, Mapping):
        raise ValueError("export must contain tables and sequence_state")
    missing = set(BUSINESS_TABLES) - set(tables)
    extra = set(tables) - set(BUSINESS_TABLES)
    if missing:
        raise ValueError("missing table(s) in export: " + ", ".join(sorted(missing)))
    if extra:
        raise ValueError("unexpected table(s) in export: " + ", ".join(sorted(extra)))
    if set(sequences) != set(SEQUENCE_TABLES):
        raise ValueError("sequence-state inventory does not match the expected schema")

    statements = [r"\set ON_ERROR_STOP on", "BEGIN;"]
    for table in DATA_IMPORT_ORDER:
        rows = tables[table]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"invalid row list for table {table}")
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("'", "''")
        target = f'public."{table}"'
        identical = (
            f"(SELECT count(*) FROM {target}) = jsonb_array_length(source_rows) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM jsonb_array_elements(source_rows) AS src(row_data) "
            f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS existing WHERE to_jsonb(existing) = to_jsonb(jsonb_populate_record(NULL::{target}, src.row_data)))"
            ")"
        )
        seeded_condition = ""
        seeded_action = ""
        if table == "iptv_config":
            seeded_condition = (
                f"(SELECT count(*) FROM {target}) = 1 "
                f"AND EXISTS (SELECT 1 FROM {target} AS target WHERE target.key = 'mode' AND target.value = 'auto') "
                "AND jsonb_array_length(source_rows) = 1 "
                "AND EXISTS (SELECT 1 FROM jsonb_array_elements(source_rows) AS src(row_data) "
                "WHERE src.row_data ->> 'key' = 'mode' AND src.row_data ->> 'value' IN ('auto', 'manual'))"
            )
            seeded_action = (
                f"INSERT INTO {target} AS target (key, value, updated_at) "
                f"SELECT key, value, updated_at FROM jsonb_populate_recordset(NULL::{target}, source_rows) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;"
            )
        elif table == "vod_source_drafts":
            seed_ids_sql = ", ".join(f"'{seed_id}'::uuid" for seed_id in VOD_MIGRATION_SEED_IDS)
            seeded_condition = (
                f"(SELECT count(*) FROM {target}) = {len(VOD_MIGRATION_SEED_IDS)} "
                f"AND NOT EXISTS (SELECT 1 FROM {target} WHERE id NOT IN ({seed_ids_sql}))"
            )
            seeded_action = (
                f"DELETE FROM {target} WHERE id IN ({seed_ids_sql});\n"
                f"        INSERT INTO {target} OVERRIDING SYSTEM VALUE "
                f"SELECT * FROM jsonb_populate_recordset(NULL::{target}, source_rows);"
            )

        reconciliation = f"    IF {identical} THEN\n        NULL;\n"
        if seeded_condition:
            reconciliation += (
                f"    ELSIF {seeded_condition} THEN\n"
                f"        {seeded_action}\n"
            )
        source_row_missing = (
            "EXISTS (SELECT 1 FROM jsonb_array_elements(source_rows) AS src(row_data) "
            f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS existing "
            "WHERE to_jsonb(existing) = "
            f"to_jsonb(jsonb_populate_record(NULL::{target}, src.row_data))))"
        )
        reconciliation += (
            "    ELSE\n"
            f"        INSERT INTO {target} OVERRIDING SYSTEM VALUE "
            f"SELECT * FROM jsonb_populate_recordset(NULL::{target}, source_rows) "
            "ON CONFLICT DO NOTHING;\n"
            f"        IF (SELECT count(*) FROM {target}) <> jsonb_array_length(source_rows) "
            f"OR {source_row_missing} THEN\n"
            f"            RAISE EXCEPTION 'target table differs from CloudBase export: {table}';\n"
            "        END IF;\n"
            "    END IF;"
        )
        statements.append(
            "DO $mytv_data_import$\n"
            f"DECLARE source_rows jsonb := '{payload}'::jsonb;\n"
            "BEGIN\n"
            f"    IF EXISTS (SELECT 1 FROM {target} LIMIT 1) THEN\n"
            f"{reconciliation}\n"
            "    ELSE\n"
            f"        INSERT INTO {target} OVERRIDING SYSTEM VALUE "
            f"SELECT * FROM jsonb_populate_recordset(NULL::{target}, source_rows);\n"
            "    END IF;\n"
            "END $mytv_data_import$;"
        )
    for sequence, table in sorted(SEQUENCE_TABLES.items()):
        raw_value = sequences[sequence]
        if raw_value is not None and (not isinstance(raw_value, int) or raw_value < 0):
            raise ValueError(f"invalid sequence value for {sequence}")
        source_value = raw_value or 0
        max_expr = f'COALESCE((SELECT MAX(id) FROM public."{table}"), 0)'
        current_expr = f"GREATEST({source_value}, {max_expr})"
        statements.append(
            f"SELECT setval(pg_get_serial_sequence('public.{table}', 'id'), "
            f"GREATEST(1, {current_expr}), {current_expr} > 0);"
        )
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private helpers for CloudBase PostgreSQL migration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export-data")
    export_parser.add_argument("--env-id", required=True)
    export_parser.add_argument("--output", required=True, type=Path)
    sql_parser = subparsers.add_parser("render-data-sql")
    sql_parser.add_argument("--input", required=True, type=Path)
    sql_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    output_path = args.output.resolve()
    if output_path.is_relative_to(root):
        parser.error("migration exports must be stored outside the repository")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "export-data":
        exported = export_cloudbase_data(args.env_id)
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(exported, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        try:
            os.chmod(output_path, 0o600)
        except OSError:
            pass
        print(json.dumps({"output": str(output_path), "tables": {name: len(rows) for name, rows in exported["tables"].items()}, "sequences": len(exported["sequence_state"])}))
        return 0

    with args.input.open("r", encoding="utf-8") as stream:
        exported = json.load(stream)
    sql = build_data_import_sql(exported)
    with output_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(sql)
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass
    print(json.dumps({"output": str(output_path), "tables": {name: len(rows) for name, rows in exported["tables"].items()}, "sql_bytes": len(sql.encode("utf-8"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
