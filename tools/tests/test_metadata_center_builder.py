import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metadata_center.builder import MetadataBuilder  # noqa: E402
from metadata_center.providers.douban_snapshot import DoubanSnapshotProvider  # noqa: E402
from metadata_center.read_api import MetadataStore  # noqa: E402


def subject(douban_id, title, **kwargs):
    payload = {
        "id": f"douban:{douban_id}",
        "douban_id": douban_id,
        "title": title,
        "original_title": "",
        "aliases": [],
        "year": "1993",
        "category": "movie",
        "genres": ["剧情"],
        "regions": ["中国大陆"],
        "tags": [],
        "durations": ["171分钟"],
        "episodes_count": 0,
        "season": 0,
        "rating": 9.6,
        "rating_count": 2180000,
        "summary": "summary text",
        "directors": ["陈凯歌"],
        "actors": ["张国荣", "张丰毅"],
        "poster_url": "https://img.example/p1.jpg",
        "hero_url": "",
        "backdrop_url": "",
        "release_date": "1993-01-01",
    }
    payload.update(kwargs)
    return payload


SNAPSHOT = {
    "schema": 1,
    "source": "douban",
    "generatedAt": "2026-09-24T00:00:00+00:00",
    "home": {},
    "index": [
        subject("1291546", "霸王别姬"),
        subject("35674595", "长安十二时辰", category="tv", aliases=["The Longest Day In Chang'an"]),
        subject("35674596", "重案六组", category="tv"),
    ],
    "details": {"1291546": subject("1291546", "霸王别姬", aliases=["Farewell My Concubine"])},
    "stats": {"index": 3, "details": 1},
}


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "douban_snapshot.json"
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

    def test_build_writes_rows_and_keeps_integrity(self):
        target = self.root / "metadata.sqlite"
        report = self._build(target)
        self.assertEqual(report.integrity, "ok")
        self.assertEqual(report.counts["media"], 3)
        self.assertEqual(report.counts["media_new"], 3)
        self.assertEqual(report.new_ids, 3)

        connection = sqlite3.connect(target)
        try:
            ids = [row[0] for row in connection.execute("SELECT id FROM media ORDER BY id")]
            self.assertTrue(all(value >= 10_000_001 for value in ids))
            providers = {row[0] for row in connection.execute("SELECT provider FROM external_id")}
            self.assertEqual(providers, {"douban"})
            titles = {row[0] for row in connection.execute("SELECT title FROM media_title")}
            self.assertIn("Farewell My Concubine", titles)
            row = connection.execute(
                "SELECT pinyin, initials FROM search_doc d JOIN media m ON m.id = d.media_id WHERE m.title = ?",
                ("霸王别姬",),
            ).fetchone()
            self.assertEqual(row[0], "bawangbieji")
            self.assertEqual(row[1], "bwbj")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM image").fetchone()[0], 3)
            self.assertEqual(connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "3")
        finally:
            connection.close()

    def test_rebuild_reuses_ids_and_timestamps(self):
        first = self.root / "v1.sqlite"
        self._build(first)
        second = self.root / "v2.sqlite"
        report = self._build(second, previous=first, version="2026092402")
        self.assertEqual(report.counts["media_reused"], 3)
        self.assertEqual(report.counts["media_new"], 0)

        connection = sqlite3.connect(second)
        try:
            stamps_one = connection.execute("SELECT id, created_at, updated_at FROM media ORDER BY id").fetchall()
        finally:
            connection.close()
        connection = sqlite3.connect(first)
        try:
            stamps_two = connection.execute("SELECT id, created_at, updated_at FROM media ORDER BY id").fetchall()
        finally:
            connection.close()
        self.assertEqual(stamps_one, stamps_two)

    def test_search_finds_chinese_pinyin_and_initials(self):
        target = self.root / "metadata.sqlite"
        self._build(target)
        store = MetadataStore(self.root, self.root)
        self.addCleanup(store.close)
        try:
            self.assertEqual(store.db_path.name, "metadata-latest.sqlite")
        finally:
            pass
        # The store expects <metadata-dir>/db/metadata-latest.sqlite.
        db_dir = self.root / "db"
        db_dir.mkdir(exist_ok=True)
        (db_dir / "metadata-latest.sqlite").write_bytes(target.read_bytes())
        for query, expected in (
            ("bwbj", "霸王别姬"),
            ("bawang", "霸王别姬"),
            ("霸王", "霸王别姬"),
            ("changanshiershichen", "长安十二时辰"),
            ("zhanganshiershichen", "长安十二时辰"),
            ("重案", "重案六组"),
        ):
            titles = [item["title"] for item in store.search(query, 5)]
            self.assertIn(expected, titles, f"{query} did not find {expected}: {titles}")
        detail = store.detail(store.search("bwbj", 1)[0]["id"])
        self.assertEqual(detail["external_ids"]["douban"], "1291546")
        self.assertEqual(detail["legacy_ref"], "douban:1291546")
        self.assertTrue(store.credits(detail["id"]))


if __name__ == "__main__":
    unittest.main()
