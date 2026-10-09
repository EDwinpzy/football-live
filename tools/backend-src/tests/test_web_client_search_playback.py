import json
import pathlib
import subprocess
import unittest


APP_JS = pathlib.Path(__file__).resolve().parents[1] / "www" / "app.js"


def run_node(script):
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


class WebClientSearchPlaybackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")

    def test_detail_and_play_routes_normalize_hhkan_references(self):
        helpers = self.source[
            self.source.index("function vodIdentity("):
            self.source.index("async function vodBrief(")
        ]
        result = run_node(helpers + """
process.stdout.write(JSON.stringify([
  vodDetailPath('hhkan:4422228'),
  vodDetailPath('douban:1292052'),
  vodPlayPath('hhkan:4422228', 41, 4422228),
  vodPlayPath('douban:1292052', 'source/one', 2),
]));
""")
        self.assertEqual(result, [
            "/hhkan/detail/4422228",
            "/vod/detail/1292052",
            "/hhkan/play/4422228/41/4422228",
            "/vod/play/1292052/source%2Fone/2",
        ])

    def test_search_classification_uses_the_hhkan_detail_route(self):
        helpers = self.source[
            self.source.index("function vodIdentity("):
            self.source.index("async function vodBrief(")
        ]
        function = self.source[
            self.source.index("async function classifyResults("):
            self.source.index("async function doSearch(")
        ]
        result = run_node("""
const BASE = '/web';
const search = {seq:1,catMap:{}};
const requests = [];
async function fetchJSON(url) { requests.push(url); return {title:'红楼梦',meta:{}}; }
function classifyVod() { return '电视剧'; }
function renderSearch() {}
""" + helpers + function + """
(async () => {
  await classifyResults([{id:'hhkan:721',categoryId:'hhkan:unknown'}], 1);
  process.stdout.write(JSON.stringify({requests,category:search.catMap['hhkan:721']}));
})();
""")
        self.assertEqual(result, {
            "requests": ["/web/hhkan/detail/721"],
            "category": "电视剧",
        })

    def test_search_appends_pages_without_duplicate_ids(self):
        function = self.source[
            self.source.index("async function doSearch("):
            self.source.index("function searchInputChanged(")
        ]
        result = run_node("""
const search = {q:'海边', remote:[], local:[], loading:false, loadingMore:false, seq:0,
  page:0, hasMore:false, moreError:'', catMap:{}};
const calls = [];
const pages = [
  {items:[{id:'douban:1',title:'甲',category:'movie'}],has_more:true},
  {items:[{id:'douban:1',title:'甲重复',category:'movie'},{id:'douban:2',title:'乙',category:'tv'}],has_more:false},
];
const BASE = '/web';
async function fetchJSON(url) { calls.push(url); return pages[calls.length - 1]; }
function renderSearch() {}
function classifyResults() {}
""" + function + """
(async () => {
  await doSearch('海边');
  await doSearch('海边', 2);
  process.stdout.write(JSON.stringify({ids:search.remote.map(x=>x.id),page:search.page,
    hasMore:search.hasMore,calls}));
})();
""")
        self.assertEqual(result["ids"], ["douban:1", "douban:2"])
        self.assertEqual(result["page"], 2)
        self.assertFalse(result["hasMore"])
        self.assertIn("page=1", result["calls"][0])
        self.assertIn("page=2", result["calls"][1])

    def test_search_reset_invalidates_pending_results_and_clears_loading(self):
        function = self.source[
            self.source.index("function searchReset()"):
            self.source.index("function searchResults()")
        ]
        result = run_node("""
const search = {q:'old',remote:[{id:'old'}],local:[],catMap:{old:'电影'},seq:7,
  loading:true,loadingMore:true,lastSearched:'old',timer:9,page:2,hasMore:true,moreError:'more',cat:'电视剧'};
const elements = {'#searchInput':{value:'old'},'#searchClear':{hidden:false},
  '#searchGrid':{innerHTML:'old'},'#searchTabs':{innerHTML:'old'},'#searchHint':{hidden:false}};
function $(selector) { return elements[selector]; }
let cleared = 0;
function clearTimeout(value) { if (value === 9) cleared++; }
""" + function + """
searchReset();
process.stdout.write(JSON.stringify({q:search.q,seq:search.seq,loading:search.loading,
  loadingMore:search.loadingMore,timer:search.timer,page:search.page,hasMore:search.hasMore,
  input:elements['#searchInput'].value,cleared}));
""")
        self.assertEqual(result, {
            "q": "", "seq": 8, "loading": False, "loadingMore": False,
            "timer": 0, "page": 0, "hasMore": False, "input": "", "cleared": 1,
        })

    def test_hhkan_line_failover_uses_episode_pid_when_line_has_no_id(self):
        function = self.source[
            self.source.index("function chooseNextVodLine("):
            self.source.index("function nextSource(")
        ]
        result = run_node(function + """
const selected = chooseNextVodLine(
  {playRef:{pid:35,vid:2},epIdx:2,failedLineIds:[]},
  [
    {name:'线路一',episodes:[{pid:35,vid:0},{pid:35,vid:1},{pid:35,vid:2}]},
    {name:'线路二',episodes:[{pid:102,vid:0},{pid:102,vid:1},{pid:102,vid:2}]},
  ]
);
process.stdout.write(JSON.stringify(selected));
""")
        self.assertEqual(result, {"index": 1, "playRef": {"pid": 102, "vid": 2}})


if __name__ == "__main__":
    unittest.main()
