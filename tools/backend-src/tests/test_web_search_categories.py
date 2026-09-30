import json
import pathlib
import subprocess
import unittest


class WebSearchCategoriesTest(unittest.TestCase):
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
