import json
import pathlib
import sys
import tempfile
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import media_index  # noqa: E402


PINYIN = {
    "庆": "qing", "余": "yu", "年": "nian", "第": "di", "二": "er", "季": "ji",
    "斗": "dou", "罗": "luo", "大": "da", "陆": "lu",
    "张": "zhang", "若": "ruo", "昀": "yun", "李": "li", "沁": "qin",
    "科": "ke", "幻": "huan", "剧": "ju", "情": "qing",
    "沉": "chen", "默": "mo", "的": "de", "凶": "xiong", "手": "shou",
    "无": "wu", "关": "guan", "剧": "ju", "集": "ji", "花": "hua",
    "絮": "xu", "回": "hui", "盛": "sheng", "世": "shi", "纪": "ji",
    "生": "sheng", "不": "bu", "息": "xi", "原": "yuan", "创": "chuang",
    "作": "zuo", "品": "pin", "歌": "ge", "手": "shou", "第": "di",
}


def subject(douban_id, title, **extra):
    value = {
        "id": "douban:" + douban_id,
        "douban_id": douban_id,
        "title": title,
        "original_title": "",
        "aliases": [],
        "year": "2024",
        "category": "tv",
        "genres": ["剧情"],
        "regions": ["中国大陆"],
        "season": 0,
        "rating": 8.0,
        "rating_count": 1,
        "summary": "",
        "directors": [],
        "actors": [],
        "poster_url": "",
        "backdrop_url": "",
        "release_date": "",
    }
    value.update(extra)
    return value


class MediaIndexTest(unittest.TestCase):
    def setUp(self):
        # Windows 上 WAL 侧文件常被短暂占用；句柄已由 _close_all 归还，
        # 剩下的删除失败与断言无关，不要它污染测试结果。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = pathlib.Path(self.tmp.name) / "media" / "mytv.db"
        self.index = media_index.MediaIndex(self.db, pinyin_map=PINYIN)
        self.index.upsert_many([
            subject("1", "庆余年 第二季", aliases=["庆余年2", "庆余年 第二部"], season=2,
                    actors=["张若昀", "李沁"]),
            subject("2", "斗罗大陆", genres=["动画", "奇幻"]),
        ])

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def assert_first(self, query, douban_id="1"):
        rows = self.index.search(query, limit=10)
        self.assertTrue(rows, query)
        self.assertEqual(rows[0]["douban_id"], douban_id, query)

    def test_searches_title_alias_full_pinyin_initials_and_season(self):
        for query in ("庆余年", "庆余", "余年", "qingyunian", "qingyu", "qyn", "qyn2", "庆余年2"):
            with self.subTest(query=query):
                self.assert_first(query)

    def test_searches_people_and_genres(self):
        for query in ("张若昀", "zhangruoyun", "zry", "李沁", "动画", "奇幻"):
            with self.subTest(query=query):
                self.assert_first(query, "1" if query not in ("动画", "奇幻") else "2")

    def test_fuzzy_fallback_only_repairs_close_queries(self):
        self.assert_first("qinyunian")
        self.assert_first("斗罗大路", "2")
        self.assertEqual(self.index.search("完全不存在的长标题", limit=10), [])

    def test_reopen_preserves_rows_and_schema_version(self):
        self.index.close()
        reopened = media_index.MediaIndex(self.db, pinyin_map=PINYIN)
        try:
            self.assertEqual(reopened.schema_version, media_index.SCHEMA_VERSION)
            self.assertEqual(reopened.search("qyn2")[0]["title"], "庆余年 第二季")
        finally:
            reopened.close()

    def test_upsert_updates_metadata_without_duplicate_rows(self):
        updated = subject("1", "庆余年 第二季", aliases=["庆余年2"], season=2, rating=9.1,
                          actors=["张若昀"])
        self.index.upsert_many([updated])
        rows = self.index.search("庆余年")
        self.assertEqual([row["douban_id"] for row in rows].count("1"), 1)
        self.assertEqual(rows[0]["rating"], 9.1)

    def titles(self, rows, limit=100):
        return [row["title"] for row in self.index.search(rows, limit=limit)]

    def test_accents_in_latin_titles_are_folded_both_sides(self):
        """带音符的外文原名必须能被不带音符的查询命中（两侧折叠，而非只折查询侧）。"""
        self.index.upsert_many([
            subject("fr1", "悲惨世界 音乐会", original_title="Les Misérables"),
            subject("fr2", "美丽人生", original_title="La vita è bella"),
            subject("fr3", "咖啡馆", original_title="Café Detective"),
        ])
        for query, expected in (("les miserables", "fr1"), ("lesmiserables", "fr1"),
                                ("misérables", "fr1"),
                                ("la vita e bella", "fr2"), ("lavitaebella", "fr2"),
                                ("cafe detective", "fr3"), ("cafedetective", "fr3")):
            with self.subTest(query=query):
                results = self.index.search(query, limit=10)
                self.assertTrue(results, query)
                self.assertEqual(results[0]["douban_id"], expected, query)
        # 折叠后仍是可反向匹配的：索引存折叠值，输入带音符也要命中
        self.assertEqual(self.index.search("les misérables", limit=5)[0]["douban_id"], "fr1")
        self.assertEqual(self.index.search("cafe detective", limit=5)[0]["douban_id"], "fr3")

    def test_normalize_text_folds_diacritics_to_ascii(self):
        self.assertEqual(media_index.normalize_text("Amélie"), "amelie")
        self.assertEqual(media_index.normalize_text("Café"), "cafe")
        self.assertEqual(media_index.normalize_text("La vita è bella"), "lavitaebella")
        # 既有行为不能被破坏
        self.assertEqual(media_index.normalize_text("庆余年 第二季"), "庆余年第二季")
        self.assertEqual(media_index.normalize_text("Spider-Man"), "spiderman")

    def test_exact_title_survives_large_candidate_flood(self):
        """候选超过 500 时，最相关的精确命中不能被截断吃掉。"""
        flood = [subject("f%d" % n, "狂飙的花样年华 第%d季" % n) for n in range(600)]
        target = subject("best", "狂飙", rating=9.5)
        self.index.upsert_many(flood + [target])
        self.assertEqual(self.index.count(), 601 + 2)
        rows = self.index.search("狂飙", limit=10)
        self.assertTrue(rows, "flood query must still return candidates")
        self.assertEqual(rows[0]["title"], "狂飙")
        self.assertEqual(rows[0]["search_score"], 120)
        self.assertIn("狂飙", self.titles("狂飙", limit=100))

    def test_exact_alias_survives_large_candidate_flood(self):
        """别名精确命中同样不能被 500 上限截断。"""
        flood = [subject("f%d" % n, "狂飙的花样年华 第%d季" % n) for n in range(600)]
        self.index.upsert_many(flood + [subject("best", "别的东西", aliases=["狂飙"])])
        rows = self.index.search("狂飙", limit=10)
        self.assertEqual(rows[0]["douban_id"], "best")
        self.assertEqual(rows[0]["search_score"], 115)

    def test_fuzzy_still_repairs_typo_beyond_eight_hits(self):
        """≥8 个弱命中时，模糊回退仍要救回错字片名（不能因命中条数门槛被跳过）。"""
        # 弱命中只在人员/类型等低分字段上出现（分数 < STRONG_SCORE），
        # 数量再多也不代表「找到了用户要的片名」，必须继续做错字近似。
        weak = [subject("w%d" % n, "无关剧集第%d季" % n, rating=7.0, genres=["悬疑"])
                for n in range(12)]
        self.index.upsert_many(weak + [subject("target", "沉默的凶手", rating=9.9)])
        self.assertIn("沉默的凶手", self.titles("沈默的凶手", limit=100))

    def test_fuzzy_gate_still_avoids_full_scan_cost_when_hits_are_strong(self):
        """强命中足够多时不必触发全表模糊扫描，但强命中本身不能丢。"""
        strong = [subject("s%d" % n, "庆余年 第%d部" % n) for n in range(10)]
        self.index.upsert_many(strong)
        rows = self.index.search("庆余年", limit=10)
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row["search_score"] > 0 for row in rows))

    def test_fuzzy_runs_when_only_a_few_strong_hits(self):
        """少量强命中时仍要做错字回退：命中数不足 8 不能跳过模糊。"""
        # 只有「同前缀」这一条能拿到 108 的强前缀分；真正的目标片名只能靠错字近似。
        self.index.upsert_many([
            subject("decoy", "沈默的凶手之外的事"),
            subject("target", "沉默的凶手", rating=9.9),
        ])
        rows = self.index.search("沈默的凶手", limit=20)
        self.assertEqual(rows[0]["title"], "沈默的凶手之外的事")
        self.assertIn("沉默的凶手", [row["title"] for row in rows])


def legacy_subject(douban_id, title, **extra):
    """与 subject() 同结构，但用于模拟「旧版归一化规则」写出的库。"""
    return subject(douban_id, title, **extra)


class LegacyNormalizationMigrationTest(unittest.TestCase):
    """重开旧索引时必须自动把归一化列升级到当前规则，且不动 user_version。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "media" / "legacy.db"
        self._open = []
        self.addCleanup(self._cleanup)
        self._cleanup_stack = []

    def _close_all(self):
        for handle in reversed(self._cleanup_stack + self._open):
            try:
                handle.close()
            except Exception:
                pass

    def _cleanup(self):
        self._close_all()
        try:
            self.tmp.cleanup()
        except OSError:
            pass  # Windows WAL 侧文件偶发占用，与断言无关

    def open_index(self):
        """每次开库都登记到清理栈，测试结束必定归还文件句柄。"""
        index = media_index.MediaIndex(self.db, pinyin_map=PINYIN)
        self._cleanup_stack.append(index)
        return index

    @staticmethod
    def _legacy_normalize(value):
        """media_index 修复前使用的规则：NFKC 保留组合音符。"""
        import unicodedata
        import re
        text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
        return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)

    def build_legacy_db(self):
        """用旧规则直接写库，模拟用户设备上已经存在的旧索引。"""
        self.db.parent.mkdir(parents=True, exist_ok=True)
        rich = legacy_subject(
            "fr1", "悲惨世界 音乐会",
            original_title="Les Misérables",
            aliases=["Les Misérables en Concert", "Miserables Live"],
            rating=9.4, rating_count=12345, year="2010",
            summary="富元数据必须保留", poster_url="https://img/p.jpg",
            hero_url="https://img/h.jpg", tags=["经典"], episodes_count=1,
        )
        with media_index.sqlite3.connect(str(self.db)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = media_index.sqlite3.Row
            # 复用当前建表语句，保证只有归一化规则不同、schema 完全一致
            conn.executescript(media_index.SCHEMA_SQL)
            conn.execute("PRAGMA user_version=1")
            # 用当前代码写入行（索引值先按新规则），随后按旧规则改写检索列，
            # 最后清掉标记 —— 等价于「旧版本代码写出的库」。
            writer = media_index.MediaIndex.__new__(media_index.MediaIndex)
            writer._lock = __import__("threading").RLock()
            writer._db = conn
            writer.pinyin_map = PINYIN
            writer.upsert_many([rich])
            conn.execute(
                "UPDATE media_search SET title_norm=?, original_norm=?, alias_norms=?"
                " WHERE douban_id=?",
                (self._legacy_normalize(rich["title"]),
                 self._legacy_normalize(rich["original_title"]),
                 "\x1f".join(self._legacy_normalize(a) for a in rich["aliases"]),
                 "fr1"))
            conn.execute("DELETE FROM media_meta")
            before = json.loads(conn.execute(
                "SELECT data_json FROM media WHERE douban_id=?", ("fr1",)).fetchone()[0])
            before_search = dict(conn.execute(
                "SELECT * FROM media_search WHERE douban_id=?", ("fr1",)).fetchone())
        return before, before_search

    def test_legacy_db_gets_accent_folding_on_reopen(self):
        """旧库里的 amlie/lesmiserables 必须在重开后变成可被无重音查询命中的值。"""
        legacy_original, legacy_search = self.build_legacy_db()
        # 旧规则把重音整个丢掉：Les Misérables -> lesmisrables（连 e 都没了）
        self.assertEqual(legacy_search["original_norm"], "lesmisrables")
        self.assertNotEqual(media_index.normalize_text("Les Misérables"), legacy_search["original_norm"])

        # 重开即自动迁移（构造即迁移，没有「迁移前仍可查询」的中间态）
        fresh = self.open_index()
        row = fresh._db.execute(
            "SELECT * FROM media_search WHERE douban_id=?", ("fr1",)).fetchone()
        self.assertEqual(row["original_norm"], "lesmiserables")
        self.assertEqual(row["title_norm"], "悲惨世界音乐会")
        results = fresh.search("les miserables", limit=5)
        self.assertTrue(results, "migrated db must answer accent-free query")
        self.assertEqual(results[0]["douban_id"], "fr1")

    def test_migration_preserves_rich_metadata(self):
        """迁移只重算检索列，富元数据（评分/简介/海报/别名原文）一个都不能丢。"""
        before, _ = self.build_legacy_db()
        after_index = self.open_index()
        row = after_index._db.execute(
            "SELECT data_json, rating, rating_count FROM media WHERE douban_id=?",
            ("fr1",)).fetchone()
        after = json.loads(row["data_json"])
        self.assertEqual(row["rating"], 9.4)
        self.assertEqual(row["rating_count"], 12345)
        self.assertEqual(after["summary"], "富元数据必须保留")
        self.assertEqual(after["poster_url"], "https://img/p.jpg")
        self.assertEqual(after["hero_url"], "https://img/h.jpg")
        self.assertEqual(after["tags"], ["经典"])
        self.assertEqual(after["aliases"], ["Les Misérables en Concert", "Miserables Live"])
        self.assertEqual(after["original_title"], "Les Misérables")
        # 展示层字段也必须原样
        hit = after_index.search("les miserables", limit=1)[0]
        self.assertEqual(hit["title"], "悲惨世界 音乐会")
        self.assertEqual(hit["summary"], "富元数据必须保留")
        # search() 会临时加 search_score，比较时忽略它
        self.assertEqual(set(before), set(after) - {"search_score"})

    def test_migration_does_not_bump_user_version(self):
        """必须保持 user_version=1，旧版回滚时仍能读。"""
        self.build_legacy_db()
        index = self.open_index()
        self.assertEqual(index.schema_version, media_index.SCHEMA_VERSION)
        self.assertEqual(index.schema_version, 1)

    def test_migration_runs_once_and_is_idempotent(self):
        """第二次重开不应重复改写，标记不前进也不丢。"""
        self.build_legacy_db()
        first = self.open_index()
        first.close()
        self._cleanup_stack.remove(first)
        second = self.open_index()
        self.assertEqual(second.normalization_version,
                         media_index.NORMALIZATION_VERSION)
        self.assertEqual(second._db.execute(
            "SELECT original_norm FROM media_search WHERE douban_id='fr1'"
        ).fetchone()[0], "lesmiserables")

    def test_new_db_records_marker_and_older_reader_still_opens_it(self):
        """全新库写入标记；且它不依赖 user_version，旧版代码仍能打开。"""
        index = self.open_index()
        self.assertEqual(index.normalization_version,
                         media_index.NORMALIZATION_VERSION)
        self.assertEqual(index.schema_version, 1)
        # 模拟旧版读取路径：只认 schema 1 的表结构
        conn = media_index.sqlite3.connect(str(self.db))
        self._open.append(conn)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM media_search").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
