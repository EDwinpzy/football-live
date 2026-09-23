import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metadata_center.builder import MetadataBuilder, content_fingerprint, sha256_file  # noqa: E402
from metadata_center.providers.douban_snapshot import DoubanSnapshotProvider  # noqa: E402
from metadata_center.publish import (  # noqa: E402
    next_data_version,
    publish,
    read_manifest,
    verify_publication,
)
from test_metadata_center_builder import SNAPSHOT, subject  # noqa: E402

PUBLIC_BASE = "https://example.test/v1/storages/object/hotupdate/metadata"


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata_dir = self.root / "metadata"
        self.public_dir = self.root / "public"
        self.snapshot = self.root / "snapshot.json"
        self.snapshot.write_text(json.dumps(SNAPSHOT, ensure_ascii=False), encoding="utf-8")
        self.builder = MetadataBuilder()

    def _build(self, target, previous=None, version="2026092401"):
        return self.builder.build(
            [DoubanSnapshotProvider(self.snapshot)],
            target,
            data_version=version,
            previous_db=previous,
            now=1_700_000_000,
        )

    def test_publish_writes_manifest_and_verifies(self):
        database = self.root / "metadata.sqlite"
        self._build(database)
        report = publish(database, self.public_dir, self.metadata_dir, "2026092401", PUBLIC_BASE,
                         now=1_700_000_000)
        manifest = read_manifest(self.public_dir)
        self.assertEqual(manifest["latest_version"], "2026092401")
        self.assertEqual(manifest["full"]["url"], f"{PUBLIC_BASE}/metadata-v2026092401.sqlite.gz")
        self.assertEqual(manifest["full"]["sha256"], sha256_file(self.public_dir / manifest["full"]["file"]))
        self.assertEqual(manifest["counts"]["media"], 3)
        self.assertEqual(manifest["deltas"], [])
        self.assertTrue((self.metadata_dir / "db" / "metadata-latest.sqlite").exists())
        self.assertTrue((self.metadata_dir / "db" / "metadata-v2026092401.sqlite").exists())
        self.assertEqual(report.pruned, [])
        self.assertEqual(verify_publication(self.public_dir)["problems"], [])

    def test_verify_detects_tampering(self):
        database = self.root / "metadata.sqlite"
        self._build(database)
        publish(database, self.public_dir, self.metadata_dir, "2026092401", PUBLIC_BASE)
        target = self.public_dir / "metadata-v2026092401.sqlite.gz"
        target.write_bytes(target.read_bytes() + b"tamper")
        problems = verify_publication(self.public_dir)["problems"]
        self.assertTrue(any("size" in item or "sha256" in item for item in problems), problems)

    def test_delta_turns_v1_into_v2(self):
        database_v1 = self.root / "v1.sqlite"
        self._build(database_v1)
        publish(database_v1, self.public_dir, self.metadata_dir, "2026092401", PUBLIC_BASE)

        updated = json.loads(json.dumps(SNAPSHOT))
        updated["index"].append(subject("35700000", "新片上映", year="2026", rating=7.1, rating_count=100))
        updated["index"][1]["aliases"] = ["The Longest Day In Chang'an", "长安24小时"]
        self.snapshot.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")

        database_v2 = self.root / "v2.sqlite"
        self._build(database_v2, previous=database_v1, version="2026092402")
        publish(database_v2, self.public_dir, self.metadata_dir, "2026092402", PUBLIC_BASE,
                previous_db=database_v1, previous_version="2026092401")

        manifest = read_manifest(self.public_dir)
        self.assertEqual(len(manifest["deltas"]), 1)
        delta = manifest["deltas"][0]
        self.assertEqual((delta["from"], delta["to"]), ("2026092401", "2026092402"))

        patched = self.root / "patched.sqlite"
        patched.write_bytes(database_v1.read_bytes())
        payload = gzip.decompress((self.public_dir / delta["file"]).read_bytes()).decode("utf-8")
        connection = sqlite3.connect(patched)
        try:
            connection.executescript(payload)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

        for table in ("media", "external_id", "media_title", "search_doc", "rating"):
            with self.subTest(table=table):
                self.assertEqual(self._rows(patched, table), self._rows(database_v2, table))
        self.assertEqual(self._fts_rows(patched), self._fts_rows(database_v2))
        self.assertEqual(verify_publication(self.public_dir)["problems"], [])

    def test_gzip_output_is_deterministic(self):
        database = self.root / "metadata.sqlite"
        self._build(database)
        publish(database, self.public_dir, self.metadata_dir, "2026092401", PUBLIC_BASE)
        first = sha256_file(self.public_dir / "metadata-v2026092401.sqlite.gz")
        publish(database, self.public_dir, self.metadata_dir, "2026092401", PUBLIC_BASE)
        second = sha256_file(self.public_dir / "metadata-v2026092401.sqlite.gz")
        self.assertEqual(first, second)

    def test_identical_rebuild_keeps_the_fingerprint(self):
        first = self.root / "v1.sqlite"
        self._build(first)
        second = self.root / "v2.sqlite"
        self._build(second, previous=first, version="2026092402")
        self.assertEqual(content_fingerprint(first), content_fingerprint(second))

    def test_next_version_increments_within_a_day(self):
        self.assertEqual(next_data_version(now=1_700_000_000), "2023111501")
        self.assertEqual(next_data_version(now=1_700_000_000, previous_manifest={"latest_version": "2023111501"}),
                         "2023111502")
        self.assertEqual(next_data_version(now=1_700_000_000, previous_manifest={"latest_version": "2020010109"}),
                         "2023111501")

    @staticmethod
    def _rows(path, table):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cursor = connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            return cursor.fetchall()
        finally:
            connection.close()

    @staticmethod
    def _fts_rows(path):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return connection.execute(
                "SELECT media_id, title, aliases, pinyin, initials, people, genres FROM search_fts ORDER BY media_id"
            ).fetchall()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
