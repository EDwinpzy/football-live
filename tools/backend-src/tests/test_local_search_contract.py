import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class LocalSearchClientContractTest(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_android_search_uses_local_api_and_douban_ids(self):
        source = self.read("android/app/src/main/java/com/qiubo/optimaltv/data/repo/LiveRepository.kt")
        block = re.search(r"suspend fun searchRemote\(.*?\n    \}\n\n", source, re.S).group(0)
        self.assertIn('/api/search?q=', block)
        self.assertNotIn('/hhkan/search', block)
        self.assertIn('id = "douban:$id"', block)
        self.assertIn('sourceId = "douban"', block)

    def test_android_debounces_latin_queries_without_chinese_gate(self):
        source = self.read("android/app/src/main/java/com/qiubo/optimaltv/ui/search/SearchScreen.kt")
        # Slice on stable code anchors: the explanatory comments around this
        # block move as the implementation changes, the code does not.
        search_block = source[
            source.index("var remote by remember"):source.index("var localHits by remember")]
        self.assertNotIn("if (!hasChinese)", search_block)
        self.assertIn("delay(450)", search_block)
        self.assertIn("Graph.live.searchRemote(searchable", search_block)

    def test_web_search_uses_local_api_without_retry_storm(self):
        source = self.read("tools/backend-src/www/app.js")
        start = source.index("async function doSearch")
        block = source[start:source.index('$("#searchInput")', start)]
        self.assertIn('BASE + "/api/search?q="', block)
        self.assertNotIn('/hhkan/search', block)
        self.assertNotIn("for (let attempt = 0; attempt < 3", block)


if __name__ == "__main__":
    unittest.main()
