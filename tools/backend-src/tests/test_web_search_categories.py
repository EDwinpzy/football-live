import json
import pathlib
import subprocess
import unittest


class WebSearchCategoriesTest(unittest.TestCase):
    def test_search_cards_do_not_expand_grid_tracks_to_poster_intrinsic_width(self):
        css = (pathlib.Path(__file__).resolve().parents[1] / "www" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".search-grid .pcard{min-width:0}", css)

    def _search_results(self, local, remote):
        """跑真实的 searchResults()（抽函数出来在 node 里执行）。"""
        source = (pathlib.Path(__file__).resolve().parents[1] / "www" / "app.js").read_text(encoding="utf-8")
        function = source[source.index("function searchResults()"):source.index("function searchShown()")]
        script = """
const search = { local: %s, remote: %s };
%s
process.stdout.write(JSON.stringify(searchResults()));
""" % (json.dumps(local), json.dumps(remote), function)
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
        return json.loads(result.stdout)

    def test_backend_identified_result_wins_over_local_catalog_copy(self):
        """后端已识别动漫的条目，不能被分类错误的本地目录副本挤掉。

        buildCatalog() 按「首次出现的频道」去重并给条目打上 `douban:<频道>`，
        那是浏览分区而不是真实分类：鬼灭之刃若先在电影频道露面，本地副本就被
        标成 douban:movie。旧合并顺序 local 在前，localHits/distinctBy 让这个错
        分类副本遮住后端已认定 douban:anime 的同名条目，动漫 tab 就搜不到原版。
        """
        misfiled = {"id": "douban:30210221", "title": "鬼灭之刃", "categoryId": "douban:movie"}
        correct = {"id": "douban:30210221", "title": "鬼灭之刃", "categoryId": "douban:anime"}
        results = self._search_results([misfiled], [correct])
        self.assertEqual(1, len(results), "同 id 应只保留一条")
        self.assertEqual(
            "douban:anime", results[0]["categoryId"],
            "同 id 冲突时应以后端已识别的 remote 分类为准",
        )

    def test_local_only_results_are_kept_as_supplement(self):
        """remote 优先不等于丢掉本地：只出现在本地的条目仍要补进结果。"""
        local = [{"id": "hhkan:1", "title": "本地独家", "categoryId": "douban:variety"}]
        remote = [{"id": "douban:9", "title": "后端结果", "categoryId": "douban:anime"}]
        results = self._search_results(local, remote)
        self.assertEqual(["douban:9", "hhkan:1"], [r["id"] for r in results])

    def test_douban_results_use_returned_categories_without_wrong_detail_requests(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / "www" / "app.js").read_text(encoding="utf-8")
        function = source[source.index("async function classifyResults("):source.index("async function doSearch(")]
        script = """
const search = {catMap: {}};
let requests = 0;
async function fetchJSON() { requests++; throw new Error('wrong catalogue'); }
function renderSearch() {}
""" + function + """
(async () => {
  const items = Array.from({length: 30}, (_, i) => ({id: 'douban:' + i, categoryId: 'douban:variety'}));
  items.push({id: 'douban:100', categoryId: 'douban:movie'});
  await classifyResults(items);
  process.stdout.write(JSON.stringify({requests, categories: search.catMap}));
})();
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["requests"], 0)
        self.assertEqual(value["categories"]["douban:0"], "综艺")
        self.assertEqual(value["categories"]["douban:29"], "综艺")
        self.assertEqual(value["categories"]["douban:100"], "电影")


if __name__ == "__main__":
    unittest.main()
