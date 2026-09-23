import unittest
import json
import hashlib
import tempfile
from pathlib import Path

from tools.selfhosted.storage_migration import reconcile_storage_manifest, select_mytv_objects


class SelfHostedStorageMigrationTests(unittest.TestCase):
    @staticmethod
    def _manifest(files):
        return {"schema_version": 1, "source_env": "private-env", "bucket": "hotupdate", "files": [
            {"key": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for key, data in files.items()
        ]}

    @staticmethod
    def _write(root, key, data):
        path = root.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_reconciliation_exact_match_and_redacted_report(self):
        files = {"apk-update/tv.json": b"tv", "iptv/version.json": b"iptv", "backend-hu21.zip": b"zip"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key, data in files.items():
                self._write(root, key, data)
            self._write(root, "other-app/private.db", b"unrelated")
            report = reconcile_storage_manifest(self._manifest(files), root)
        self.assertTrue(report["ok"])
        self.assertEqual(report["source_objects"], 3)
        self.assertEqual(report["target_objects"], 3)
        self.assertEqual(report["source_bytes"], 9)
        self.assertEqual(report["target_bytes"], 9)
        self.assertEqual(report["matched_objects"], 3)
        self.assertEqual(report["mismatched_keys"], [])
        self.assertRegex(report["source_manifest_sha256"], r"^[a-f0-9]{64}$")
        canonical = json.dumps(
            [{"key": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
             for key, data in sorted(files.items())],
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(report["manifest_sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertNotIn("private-env", str(report))
        self.assertNotIn("unrelated", str(report))

    def test_reconciliation_detects_same_size_changed_content(self):
        files = {"iptv/version.json": b"good"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "iptv/version.json", b"evil")
            report = reconcile_storage_manifest(self._manifest(files), root)
        self.assertFalse(report["ok"])
        self.assertEqual(report["source_bytes"], report["target_bytes"])
        self.assertEqual(report["mismatched_keys"], ["iptv/version.json"])

    def test_reconciliation_detects_missing_and_extra_mytv_files(self):
        files = {"apk-update/tv.json": b"tv", "backend-hu21.zip": b"zip"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "iptv/unexpected.m3u", b"extra")
            report = reconcile_storage_manifest(self._manifest(files), root)
        self.assertFalse(report["ok"])
        self.assertEqual(report["target_objects"], 1)
        self.assertEqual(report["mismatched_keys"], ["apk-update/tv.json", "backend-hu21.zip", "iptv/unexpected.m3u"])

    def test_reconciliation_rejects_out_of_scope_and_unsafe_manifest_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key in ("other-app/file", "backend-hu1.zip/part", "iptv/../secret", "apk-update/%2e%2e/secret", "iptv/\\secret", "/iptv/file"):
                manifest = self._manifest({key: b"x"})
                with self.subTest(key=key), self.assertRaises(ValueError):
                    reconcile_storage_manifest(manifest, root)

    def test_reconciliation_rejects_symlink_in_target_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.write_bytes(b"outside")
            (root / "iptv").mkdir()
            try:
                (root / "iptv" / "linked.json").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "unsafe target object"):
                reconcile_storage_manifest(self._manifest({}), root)

    def test_inventory_selects_only_mytv_storage_prefixes(self):
        selected, excluded = select_mytv_objects(
            [
                {"name": "apk-update/tv.json", "metadata": {"size": 120}},
                {"name": "backend-hu21.zip", "metadata": {"size": 300}},
                {"name": "iptv/version.json", "metadata": {"size": 10}},
                {"name": "other-app/private.db", "metadata": {"size": 99}},
            ]
        )
        self.assertEqual([item["key"] for item in selected], [
            "apk-update/tv.json", "backend-hu21.zip", "iptv/version.json"
        ])
        self.assertEqual(excluded, 1)

    def test_inventory_rejects_path_traversal_and_invalid_sizes(self):
        with self.assertRaisesRegex(ValueError, "unsafe object key"):
            select_mytv_objects([{"name": "iptv/../private", "metadata": {"size": 1}}])
        with self.assertRaisesRegex(ValueError, "invalid object size"):
            select_mytv_objects([{"name": "iptv/version.json", "metadata": {"size": -1}}])


if __name__ == "__main__":
    unittest.main()
