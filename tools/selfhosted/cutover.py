#!/usr/bin/env python3
"""Redacted cutover ledger and promotion gates for the My TV migration."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.selfhosted.import_cloudbase import BUSINESS_TABLES, SEQUENCE_TABLES, build_data_import_sql


STATES = frozenset({"cloudbase_primary", "maintenance", "selfhosted_primary", "rollback"})
TRANSITIONS = {
    "cloudbase_primary": frozenset({"maintenance"}),
    "maintenance": frozenset({"selfhosted_primary", "rollback"}),
    "selfhosted_primary": frozenset({"rollback"}),
    "rollback": frozenset(),
}
REQUIRED_HEALTH_CHECKS = (
    "external_https",
    "health",
    "admin_login",
    "activation",
    "hotupdate",
    "iptv",
    "web",
    "apk_update",
    "cloudbase_bridge",
)
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initial_state() -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "cloudbase_primary",
        "backup_id": None,
        "snapshot": None,
        "sync": None,
        "verification": None,
        "maintenance_marker": None,
        "pre_maintenance_backup_id": None,
        "promotion_checkpoint": None,
        "events": [],
    }


def _validate_state(state: Mapping[str, object]) -> None:
    if state.get("schema_version") != 1 or state.get("state") not in STATES:
        raise ValueError("invalid cutover state")


def transition_state(state: Mapping[str, object], target: str) -> dict[str, object]:
    _validate_state(state)
    current = str(state["state"])
    if target not in TRANSITIONS[current]:
        raise ValueError(f"invalid cutover transition: {current} -> {target}")
    updated = copy.deepcopy(dict(state))
    updated["state"] = target
    events = list(updated.get("events", []))
    events.append({"from": current, "to": target, "at": _now()})
    updated["events"] = events
    return updated


def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _manifest_summary(manifest: Mapping[str, object]) -> dict[str, object]:
    if manifest.get("bucket") != "hotupdate":
        raise ValueError("object manifest is outside the My TV bucket")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("object manifest is invalid")
    normalized = []
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("object manifest is invalid")
        key, size, digest = item.get("key"), item.get("size"), item.get("sha256")
        if (
            not isinstance(key, str)
            or not key
            or "\\" in key
            or key.startswith("/")
            or any(part in ("", ".", "..") for part in key.split("/"))
            or not (key.startswith(("apk-update/", "iptv/")) or re.fullmatch(r"backend-hu[0-9]+\.zip", key))
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
        ):
            raise ValueError("object manifest is invalid")
        normalized.append({"key": key, "size": size, "sha256": digest})
    normalized.sort(key=lambda item: item["key"])
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "object_count": len(normalized),
        "object_bytes": sum(item["size"] for item in normalized),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def record_snapshot(
    state: dict[str, object], backup_id: str, data_export: Mapping[str, object], object_manifest: Mapping[str, object]
) -> dict[str, object]:
    _validate_state(state)
    if state["state"] not in ("cloudbase_primary", "maintenance"):
        raise ValueError("cannot snapshot after promotion or rollback")
    backup_id = _validate_identifier(backup_id, "backup id")
    tables, sequences = data_export.get("tables"), data_export.get("sequence_state")
    if not isinstance(tables, Mapping) or set(tables) != set(BUSINESS_TABLES):
        raise ValueError("snapshot does not contain the My TV business table set")
    if not isinstance(sequences, Mapping) or set(sequences) != set(SEQUENCE_TABLES):
        raise ValueError("snapshot sequence inventory is invalid")
    counts = {}
    for table in BUSINESS_TABLES:
        rows = tables[table]
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise ValueError("snapshot contains invalid table rows")
        counts[table] = len(rows)
    sequence_state = {}
    for name, value in sequences.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("snapshot sequence inventory is invalid")
        sequence_state[name] = value
    snapshot = {
        "backup_id": backup_id,
        "captured_at": _now(),
        "maintenance_marker": state.get("maintenance_marker") if state["state"] == "maintenance" else None,
        "tables": counts,
        "sequence_count": len(sequence_state),
        "sequence_state": sequence_state,
        **_manifest_summary(object_manifest),
    }
    state["backup_id"] = backup_id
    state["snapshot"] = snapshot
    state["sync"] = None
    state["verification"] = None
    return state


def record_sync(
    state: dict[str, object],
    database_report: Mapping[str, object],
    object_report: Mapping[str, object],
) -> dict[str, object]:
    _validate_state(state)
    if state["state"] not in ("cloudbase_primary", "maintenance") or not isinstance(state.get("snapshot"), Mapping):
        raise ValueError("sync requires a CloudBase snapshot")
    snapshot = state["snapshot"]
    tables = database_report.get("tables")
    sequences = database_report.get("sequences")
    if not isinstance(tables, Mapping) or set(tables) != set(BUSINESS_TABLES):
        raise ValueError("database reconciliation report is invalid")
    if not isinstance(sequences, Mapping) or set(sequences) != set(SEQUENCE_TABLES):
        raise ValueError("database sequence reconciliation report is invalid")
    if not isinstance(database_report.get("match"), bool):
        raise ValueError("database reconciliation result is invalid")

    table_counts = {}
    mismatched_tables = []
    exact_rows = True
    for table in BUSINESS_TABLES:
        row = tables[table]
        if not isinstance(row, Mapping):
            raise ValueError("database reconciliation report is invalid")
        source, target, match = row.get("source"), row.get("target"), row.get("match")
        valid_counts = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (source, target))
        if not valid_counts or not isinstance(match, bool):
            raise ValueError("database reconciliation counts are invalid")
        table_counts[table] = {"source": source, "target": target, "match": match}
        if match is not True or source != snapshot["tables"][table] or target != source:
            mismatched_tables.append(table)
            exact_rows = False

    sequence_match = all(sequences[name] is True for name in SEQUENCE_TABLES)
    database_ok = database_report["match"] is True and exact_rows and sequence_match

    object_counts = (
        object_report.get("source_objects"), object_report.get("target_objects"),
        object_report.get("source_bytes"), object_report.get("target_bytes"),
        object_report.get("matched_objects"),
    )
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in object_counts):
        raise ValueError("object reconciliation counts are invalid")
    object_mismatches = object_report.get("mismatched_keys")
    if not isinstance(object_mismatches, list) or any(not isinstance(key, str) for key in object_mismatches):
        raise ValueError("object reconciliation report is invalid")
    object_ok = (
        object_report.get("ok") is True
        and object_counts[0] == snapshot["object_count"]
        and object_counts[1] == object_counts[0]
        and object_counts[2] == snapshot["object_bytes"]
        and object_counts[3] == object_counts[2]
        and object_counts[4] == object_counts[0]
        and object_report.get("manifest_sha256") == snapshot["manifest_sha256"]
        and not object_mismatches
    )
    maintenance_marker = state.get("maintenance_marker") if state["state"] == "maintenance" else None
    snapshot_is_current = snapshot.get("maintenance_marker") == maintenance_marker
    ok = database_ok and object_ok and snapshot_is_current
    state["sync"] = {
        "ok": ok,
        "maintenance_marker": maintenance_marker,
        "tables": table_counts,
        "mismatched_tables": mismatched_tables,
        "sequence_match": sequence_match,
        "objects_checked": object_counts[0],
        "object_mismatches": len(object_mismatches),
        "verified_at": _now(),
    }
    state["verification"] = None
    return state


def record_verification(state: dict[str, object], checks: Mapping[str, object]) -> dict[str, object]:
    _validate_state(state)
    if state["state"] not in ("cloudbase_primary", "maintenance"):
        raise ValueError("health verification must precede promotion")
    if state["state"] == "maintenance":
        sync = state.get("sync")
        if not (isinstance(sync, Mapping) and sync.get("ok") and sync.get("maintenance_marker") == state.get("maintenance_marker")):
            raise ValueError("maintenance verification requires a verified final sync")
    normalized = {name: checks.get(name) is True for name in REQUIRED_HEALTH_CHECKS}
    state["verification"] = {
        "ok": all(normalized.values()),
        "maintenance_marker": state.get("maintenance_marker") if state["state"] == "maintenance" else None,
        "checks": normalized,
        "verified_at": _now(),
    }
    return state


def enter_maintenance(state: dict[str, object], marker_id: str, *, source_writes_paused: bool) -> dict[str, object]:
    _validate_state(state)
    if state["state"] != "cloudbase_primary":
        raise ValueError("maintenance can only begin from CloudBase primary")
    if not state.get("backup_id") or not (isinstance(state.get("sync"), Mapping) and state["sync"].get("ok")):
        raise ValueError("maintenance requires a verified snapshot and sync")
    if not (isinstance(state.get("verification"), Mapping) and state["verification"].get("ok")):
        raise ValueError("maintenance requires passed health checks")
    if source_writes_paused is not True:
        raise ValueError("CloudBase writes must be paused before entering maintenance")
    state = transition_state(state, "maintenance")
    state["maintenance_marker"] = _validate_identifier(marker_id, "maintenance marker")
    state["pre_maintenance_backup_id"] = state.get("backup_id")
    state["snapshot"] = None
    state["sync"] = None
    state["verification"] = None
    return state


def promote(state: dict[str, object], marker_id: str) -> dict[str, object]:
    _validate_state(state)
    if state["state"] != "maintenance":
        raise ValueError("promotion requires maintenance state")
    if state.get("maintenance_marker") != _validate_identifier(marker_id, "maintenance marker"):
        raise ValueError("maintenance marker does not match")
    snapshot = state.get("snapshot")
    sync = state.get("sync")
    verification = state.get("verification")
    if not (
        isinstance(snapshot, Mapping)
        and snapshot.get("backup_id") == state.get("backup_id")
        and snapshot.get("maintenance_marker") == marker_id
        and isinstance(sync, Mapping)
        and sync.get("maintenance_marker") == marker_id
    ):
        raise ValueError("promotion requires a final snapshot and sync after CloudBase writes were paused")
    if not (
        state.get("backup_id")
        and isinstance(sync, Mapping)
        and sync.get("ok")
        and isinstance(verification, Mapping)
        and verification.get("ok")
    ):
        raise ValueError("promotion requires verified snapshot, sync, and health evidence")
    state = transition_state(state, "selfhosted_primary")
    state["promotion_checkpoint"] = f"{state['backup_id']}-{len(state['events'])}"
    return state


def rollback_check(state: Mapping[str, object], new_side_writes: int) -> dict[str, object]:
    _validate_state(state)
    if isinstance(new_side_writes, bool) or not isinstance(new_side_writes, int) or new_side_writes < 0:
        raise ValueError("new-side write count must be a non-negative integer")
    allowed_state = state["state"] in ("maintenance", "selfhosted_primary")
    allowed = allowed_state and new_side_writes == 0
    reason = "no self-hosted writes recorded" if allowed else (
        "rollback is blocked after new-side writes" if new_side_writes else "rollback is unavailable in the current state"
    )
    return {"allowed": allowed, "reason": reason, "new_side_writes": new_side_writes}


def build_status_report(state: Mapping[str, object]) -> dict[str, object]:
    _validate_state(state)
    snapshot, sync, verification = state.get("snapshot"), state.get("sync"), state.get("verification")
    snapshot_summary = None
    if isinstance(snapshot, Mapping):
        snapshot_summary = {
            "backup_id": snapshot.get("backup_id"),
            "captured_at": snapshot.get("captured_at"),
            "tables": snapshot.get("tables"),
            "sequence_count": snapshot.get("sequence_count"),
            "object_count": snapshot.get("object_count"),
            "object_bytes": snapshot.get("object_bytes"),
            "manifest_sha256": snapshot.get("manifest_sha256"),
        }
    sync_summary = None
    if isinstance(sync, Mapping):
        sync_summary = {
            "ok": sync.get("ok"),
            "tables": sync.get("tables"),
            "mismatched_tables": sync.get("mismatched_tables"),
            "sequence_match": sync.get("sequence_match"),
            "objects_checked": sync.get("objects_checked"),
            "object_mismatches": sync.get("object_mismatches"),
            "verified_at": sync.get("verified_at"),
        }
    verification_summary = None
    if isinstance(verification, Mapping):
        verification_summary = {
            "ok": verification.get("ok"),
            "checks": verification.get("checks"),
            "verified_at": verification.get("verified_at"),
        }
    return {
        "schema_version": 1,
        "state": state["state"],
        "backup_id": state.get("backup_id"),
        "snapshot": snapshot_summary,
        "sync": sync_summary,
        "verification": verification_summary,
        "maintenance_marker": state.get("maintenance_marker"),
        "promotion_checkpoint": state.get("promotion_checkpoint"),
        "rollback_checkpoint": state.get("pre_maintenance_backup_id") or state.get("backup_id"),
    }


def _state_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(REPOSITORY_ROOT):
        raise ValueError("cutover state must be stored outside the repository")
    return path


def _load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _save_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def _print_report(state: Mapping[str, object]) -> None:
    print(json.dumps(build_status_report(state), ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="My TV CloudBase cutover gates and redacted reports")
    parser.add_argument("--state-file", required=True, type=Path, help="protected state file outside the repository")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create a CloudBase-primary state file")
    snapshot = sub.add_parser("snapshot", help="record a count-only backup and My TV object manifest")
    snapshot.add_argument("--backup-id", required=True)
    snapshot.add_argument("--data-export", required=True, type=Path)
    snapshot.add_argument("--object-manifest", required=True, type=Path)
    sync = sub.add_parser("sync", help="prepare safe SQL and record database/object reconciliation")
    sync.add_argument("--database-report", required=True, type=Path)
    sync.add_argument("--object-report", required=True, type=Path)
    sync.add_argument("--data-export", type=Path)
    sync.add_argument("--sql-output", type=Path)
    verify = sub.add_parser("verify", help="record required public service acceptance checks")
    verify.add_argument("--health-report", required=True, type=Path)
    maintenance = sub.add_parser("maintenance", help="record the verified CloudBase write-pause marker")
    maintenance.add_argument("--marker-id", required=True)
    maintenance.add_argument("--source-writes-paused", action="store_true")
    promotion = sub.add_parser("promote", help="promote the verified VPS as sole writer")
    promotion.add_argument("--marker-id", required=True)
    rollback = sub.add_parser("rollback-check", help="check rollback eligibility without changing state")
    rollback.add_argument("--new-side-writes", required=True, type=int)
    sub.add_parser("report", help="print the redacted migration report")
    args = parser.parse_args(argv)

    try:
        state_path = _state_path(args.state_file)
        if args.command == "init":
            if state_path.exists():
                raise ValueError("cutover state file already exists")
            state = initial_state()
        else:
            state = _load_json(state_path)
            if not isinstance(state, dict):
                raise ValueError("cutover state file is invalid")
            _validate_state(state)

        if args.command == "snapshot":
            data_export = _load_json(args.data_export)
            object_manifest = _load_json(args.object_manifest)
            if not isinstance(data_export, Mapping) or not isinstance(object_manifest, Mapping):
                raise ValueError("snapshot input is invalid")
            state = record_snapshot(state, args.backup_id, data_export, object_manifest)
        elif args.command == "sync":
            if bool(args.data_export) != bool(args.sql_output):
                raise ValueError("--data-export and --sql-output must be supplied together")
            if args.data_export:
                sql_path = _state_path(args.sql_output)
                exported = _load_json(args.data_export)
                if not isinstance(exported, Mapping):
                    raise ValueError("data export is invalid")
                sql_path.parent.mkdir(parents=True, exist_ok=True)
                with sql_path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(build_data_import_sql(exported))
                try:
                    os.chmod(sql_path, 0o600)
                except OSError:
                    pass
            database_report = _load_json(args.database_report)
            object_report = _load_json(args.object_report)
            if not isinstance(database_report, Mapping) or not isinstance(object_report, Mapping):
                raise ValueError("sync report is invalid")
            state = record_sync(state, database_report, object_report)
        elif args.command == "verify":
            health_report = _load_json(args.health_report)
            if not isinstance(health_report, Mapping):
                raise ValueError("health report is invalid")
            state = record_verification(state, health_report)
        elif args.command == "maintenance":
            state = enter_maintenance(state, args.marker_id, source_writes_paused=args.source_writes_paused)
        elif args.command == "promote":
            state = promote(state, args.marker_id)
        elif args.command == "rollback-check":
            result = rollback_check(state, args.new_side_writes)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0 if result["allowed"] else 1
        elif args.command == "report":
            _print_report(state)
            return 0

        _save_state(state_path, state)
        _print_report(state)
        if args.command in ("sync", "verify"):
            gate = state.get("sync") if args.command == "sync" else state.get("verification")
            return 0 if isinstance(gate, Mapping) and gate.get("ok") else 1
        return 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
        print(f"cutover: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
