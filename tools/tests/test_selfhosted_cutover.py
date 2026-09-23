import subprocess
import sys
import unittest
from pathlib import Path

from tools.selfhosted.import_cloudbase import BUSINESS_TABLES, SEQUENCE_TABLES
from tools.selfhosted.cutover import (
    build_status_report,
    enter_maintenance,
    initial_state,
    promote,
    record_snapshot,
    record_sync,
    record_verification,
    rollback_check,
    transition_state,
)

ROOT = Path(__file__).resolve().parents[2]


class SelfHostedCutoverTests(unittest.TestCase):
    def source_export(self):
        return {
            "tables": {
                name: ([{"code": "license-code-fixture"}] if name == "licenses" else [])
                for name in BUSINESS_TABLES
            },
            "sequence_state": {name: None for name in SEQUENCE_TABLES},
        }

    def object_manifest(self):
        return {"bucket": "hotupdate", "files": [{"key": "backend-hu21.zip", "size": 100, "sha256": "a" * 64}]}

    def ready_state(self):
        state = initial_state()
        record_snapshot(state, "backup-20260923", self.source_export(), self.object_manifest())
        self.record_successful_sync(state)
        self.record_successful_health(state)
        return state

    def record_successful_sync(self, state):
        database_report = {
            "tables": {
                name: {"source": int(name == "licenses"), "target": int(name == "licenses"), "match": True}
                for name in BUSINESS_TABLES
            },
            "sequences": {name: True for name in SEQUENCE_TABLES},
            "match": True,
        }
        object_report = {
            "ok": True,
            "manifest_sha256": state["snapshot"]["manifest_sha256"],
            "source_objects": 1,
            "target_objects": 1,
            "source_bytes": 100,
            "target_bytes": 100,
            "matched_objects": 1,
            "mismatched_keys": [],
        }
        record_sync(state, database_report, object_report)

    def test_sync_rejects_same_count_database_rows_with_changed_content(self):
        state = initial_state()
        record_snapshot(state, "backup-20260923", self.source_export(), self.object_manifest())
        database_report = {
            "tables": {
                name: {"source": int(name == "licenses"), "target": int(name == "licenses"),
                       "match": name != "licenses"}
                for name in BUSINESS_TABLES
            },
            "sequences": {name: True for name in SEQUENCE_TABLES},
            "match": False,
        }
        object_report = {
            "ok": True, "manifest_sha256": state["snapshot"]["manifest_sha256"],
            "source_objects": 1, "target_objects": 1, "source_bytes": 100, "target_bytes": 100,
            "matched_objects": 1, "mismatched_keys": [],
        }
        record_sync(state, database_report, object_report)
        self.assertFalse(state["sync"]["ok"])
        self.assertEqual(state["sync"]["mismatched_tables"], ["licenses"])

    def test_sync_rejects_object_hash_mismatch_even_when_counts_match(self):
        state = initial_state()
        record_snapshot(state, "backup-20260923", self.source_export(), self.object_manifest())
        database_report = {
            "tables": {
                name: {"source": int(name == "licenses"), "target": int(name == "licenses"), "match": True}
                for name in BUSINESS_TABLES
            },
            "sequences": {name: True for name in SEQUENCE_TABLES},
            "match": True,
        }
        object_report = {
            "ok": False, "manifest_sha256": state["snapshot"]["manifest_sha256"],
            "source_objects": 1, "target_objects": 1, "source_bytes": 100, "target_bytes": 100,
            "matched_objects": 0, "mismatched_keys": ["backend-hu21.zip"],
        }
        record_sync(state, database_report, object_report)
        self.assertFalse(state["sync"]["ok"])
        self.assertEqual(state["sync"]["object_mismatches"], 1)

    def record_successful_health(self, state):
        record_verification(state, {
            "external_https": True,
            "health": True,
            "admin_login": True,
            "activation": True,
            "hotupdate": True,
            "iptv": True,
            "web": True,
            "apk_update": True,
            "cloudbase_bridge": True,
        })

    def test_state_machine_requires_maintenance_and_passed_gates_before_promotion(self):
        state = initial_state()
        with self.assertRaisesRegex(ValueError, "invalid cutover transition"):
            transition_state(state, "selfhosted_primary")

        state = self.ready_state()
        state = enter_maintenance(state, "maintenance-20260923", source_writes_paused=True)
        self.assertIsNone(state["snapshot"])
        with self.assertRaisesRegex(ValueError, "final snapshot"):
            promote(state, "maintenance-20260923")

        record_snapshot(state, "backup-final-20260923", self.source_export(), self.object_manifest())
        self.record_successful_sync(state)
        self.record_successful_health(state)
        state = promote(state, "maintenance-20260923")
        self.assertEqual(state["state"], "selfhosted_primary")
        self.assertTrue(state["promotion_checkpoint"])

    def test_promotion_rejects_missing_snapshot_sync_or_health_evidence(self):
        state = initial_state()
        state["state"] = "maintenance"
        state["maintenance_marker"] = "maintenance-20260923"
        with self.assertRaisesRegex(ValueError, "final snapshot"):
            promote(state, "maintenance-20260923")

    def test_rollback_is_not_allowed_after_new_server_writes(self):
        state = self.ready_state()
        state = enter_maintenance(state, "maintenance-20260923", source_writes_paused=True)
        record_snapshot(state, "backup-final-20260923", self.source_export(), self.object_manifest())
        self.record_successful_sync(state)
        self.record_successful_health(state)
        state = promote(state, "maintenance-20260923")
        result = rollback_check(state, new_side_writes=1)
        self.assertFalse(result["allowed"])
        self.assertIn("new-side writes", result["reason"])

    def test_snapshot_and_status_report_exclude_business_rows(self):
        state = initial_state()
        record_snapshot(state, "backup-20260923", self.source_export(), self.object_manifest())
        report = build_status_report(state)
        serialized = str(report)
        self.assertEqual(report["snapshot"]["tables"]["licenses"], 1)
        self.assertEqual(report["snapshot"]["object_count"], 1)
        self.assertNotIn("license-code-fixture", serialized)
        self.assertNotIn("rows", report)

    def test_snapshot_rejects_objects_outside_mytv_storage_scope(self):
        manifest = {"bucket": "other", "files": self.object_manifest()["files"]}
        with self.assertRaisesRegex(ValueError, "outside the My TV bucket"):
            record_snapshot(initial_state(), "backup-20260923", self.source_export(), manifest)

    def test_cutover_cli_runs_directly_from_the_repository_root(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "selfhosted" / "cutover.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("snapshot", result.stdout)
        self.assertIn("rollback-check", result.stdout)


if __name__ == "__main__":
    unittest.main()
