"""契约测试：首装离线目录种子（assets/catalog_seed.json）必须满足 App 的解码口径。

2026-09-18 的真实故障：种子还是退役的好好看格式（v:1 / s:"hhkan"），被
VodRepository.decodeSnapshot 的豆瓣来源校验整条拒绝 → 首装那一瞬没有目录可上屏。
这里把口径钉死，并确认发布流程会随快照重新生成它。
"""
import json
import pathlib
import re
import unittest
from urllib.parse import unquote

ROOT = pathlib.Path(__file__).resolve().parents[3]
SEEDS = (
    ROOT / "android" / "app" / "src" / "main" / "assets" / "catalog_seed.json",
    ROOT / "internal" / "mobile" / "app" / "src" / "main" / "assets" / "catalog_seed.json",
)
GENERATOR = ROOT / "tools" / "build_catalog_seed.py"
RELEASE = ROOT / "tools" / "release_all.py"
VOD_REPO = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv" / "data" / "repo" / "VodRepository.kt"
# 客户端写盘使用的当前快照版本。随包发布的种子由真实后端导出、CI 无法重新生成，
# 因此它的版本只需要落在客户端显式接受的种子版本集合内，不必等于 SNAPSHOT_VERSION。
SNAPSHOT_VERSION = 4
SEED_ACCEPTED_VERSIONS = (3, 4)
CATEGORY_KEYS = ("movie", "tv", "anime", "variety", "short")


class CatalogSeedContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seeds = [json.loads(p.read_text(encoding="utf-8")) for p in SEEDS]
        cls.vod = VOD_REPO.read_text(encoding="utf-8")

    def test_version_matches_client_policy(self):
        """种子 v 必须落在客户端显式接受的种子版本集合内。"""
        m = re.search(r"SNAPSHOT_VERSION\s*=\s*(\d+)", self.vod)
        self.assertIsNotNone(m, "VodRepository 里找不到 SNAPSHOT_VERSION")
        self.assertEqual(int(m.group(1)), SNAPSHOT_VERSION)
        seed_versions = re.search(
            r"SEED_ACCEPTED_VERSIONS\s*=\s*setOf\(([^)]*)\)", self.vod)
        self.assertIsNotNone(seed_versions, "VodRepository 里找不到 SEED_ACCEPTED_VERSIONS")
        accepted = {int(value) for value in re.findall(r"\d+", seed_versions.group(1))}
        self.assertEqual(accepted, set(SEED_ACCEPTED_VERSIONS))
        for seed in self.seeds:
            self.assertIn(seed["v"], SEED_ACCEPTED_VERSIONS)

    def test_只包含豆瓣卡片(self):
        """CatalogOriginPolicy.allows：sourceId 必须是 douban、id 必须是 douban: 前缀。"""
        for seed in self.seeds:
            for item in seed["items"]:
                self.assertEqual(item["s"], "douban", item.get("id"))
                self.assertTrue(str(item["id"]).startswith("douban:"), item.get("id"))
                self.assertTrue(str(item["d"]).strip(), item.get("id"))
                self.assertTrue(str(item["t"]).strip(), item.get("id"))

    def test_覆盖五个分类且有足够条目(self):
        for seed in self.seeds:
            ids = [c["id"] for c in seed["categories"]]
            self.assertEqual(ids, ["douban:" + k for k in CATEGORY_KEYS])
            self.assertGreaterEqual(len(seed["items"]), 100)
            used = {i["c"] for i in seed["items"]}
            self.assertEqual(used, set(ids), "每个分类都要有条目，否则分类卡会空")

    def test_海报走内置后端中继(self):
        """豆瓣图床直连会被防盗链拦（HTTP 418），种子必须存中继地址。"""
        for seed in self.seeds:
            relayed = [i for i in seed["items"] if "/hhkan/proxy?u=" in str(i["p"])]
            self.assertGreaterEqual(len(relayed), len(seed["items"]) * 0.9)
            self.assertNotIn("doubanio.com", json.dumps(
                [i["p"] for i in seed["items"] if "/hhkan/proxy?u=" not in str(i["p"])]))

    def test_快照保留顶部高清海报字段(self):
        """首屏不能从只含 240px poster 的旧快照恢复，必须持久化 hero 字段。"""
        for seed in self.seeds:
            hero_items = [item for item in seed["items"] if item.get("h")]
            self.assertGreaterEqual(len(hero_items), len(seed["items"]) * 0.9)
            self.assertTrue(all("large/public" in unquote(str(item["h"])) for item in hero_items[:10]))
        for path in (
            ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv" / "data" / "repo" / "VodRepository.kt",
            ROOT / "internal" / "mobile" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv" / "data" / "repo" / "VodRepository.kt",
        ):
            source = path.read_text(encoding="utf-8")
            self.assertIn('.put("h", it.heroUrl)', source, path)
            self.assertIn('heroUrl = e.optString("h")', source, path)

    def test_影视首页hero优先使用高清字段(self):
        """目录/热门板块进入 hero 时不能退回 240px posterUrl。"""
        internal_home = ROOT / "internal" / "mobile" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv" / "ui" / "home" / "HomeScreen.kt"
        self.assertIn('it.heroUrl.ifBlank { it.posterUrl }', internal_home.read_text(encoding="utf-8"))
        for path in (
            ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv" / "data" / "repo" / "VodRepository.kt",
            ROOT / "internal" / "mobile" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv" / "data" / "repo" / "VodRepository.kt",
        ):
            source = path.read_text(encoding="utf-8")
            self.assertIn('it.heroUrl.ifBlank { it.posterUrl }', source, path)

    def test_两份工程种子完全一致(self):
        self.assertEqual(SEEDS[0].read_text(encoding="utf-8"),
                         SEEDS[1].read_text(encoding="utf-8"))

    def test_发布流程会重新生成种子(self):
        """种子是快照的派生物：必须由 release_all 串进流程，否则会再次过期。"""
        self.assertTrue(GENERATOR.is_file(), "缺少 tools/build_catalog_seed.py")
        self.assertIn("build_catalog_seed.py", RELEASE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
