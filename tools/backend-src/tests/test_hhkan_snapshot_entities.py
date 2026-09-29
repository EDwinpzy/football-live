"""hhkan 快照的 HTML 实体必须在读取端还原（BUG-02）。

背景：`hhkan.py` 的实时抓取路径 `_clean_text()` 含 `html.unescape`，但
`hhkan_snapshot.py` 的快照读取路径直接 `json.loads` 原始字符串，不解码。
快照是离线抓取时落盘的，落盘前也没走 `_clean_text`，于是实体被固化进
`hhkan_snapshot.json` 并原样送到客户端——简介/标题上屏显示 `&#039;`、
`&quot;`、`&amp;` 字面量（实测 19/49 份 UI dump 命中）。

两条路径不对称是根因：
    实时抓取：hhkan.py:418  _clean_text() 含 html.unescape   -> 会解码
    快照读取：hhkan_snapshot.py  load_default()/detail()     -> 不解码
"""
import json
import pathlib
import re
import sys
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import hhkan_snapshot  # noqa: E402
import hhkan  # noqa: E402


ENTITY = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#\d{2,6}|#x[0-9a-fA-F]{2,6});")


def _walk_strings(node, path=""):
    """产出快照里所有 (路径, 字符串)。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, "{}.{}".format(path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, "{}[{}]".format(path, index))
    elif isinstance(node, str):
        yield path, node


class HhkanSnapshotEntityTest(unittest.TestCase):
    def test_decode_entities_in_a_string(self):
        self.assertEqual(
            hhkan_snapshot._decode_entities("Troy &#039;Assassin&#039; Kelly"),
            "Troy 'Assassin' Kelly",
        )

    def test_decode_leaves_plain_text_untouched(self):
        self.assertEqual(hhkan_snapshot._decode_entities("普通简介"), "普通简介")

    def test_decode_handles_double_escaped_entities(self):
        """随包快照里存在**双重转义**：`&amp;quot;` 解一次只是 `&quot;`。

        实测 details.378068.desc 磁盘上是 `&amp;quot;Bridal Shower&amp;quot;`，
        只解一层会把 `&quot;` 字面量留给用户看（这正是 BUG-02 的残留症状）。
        """
        self.assertEqual(
            hhkan_snapshot._decode_entities("&amp;quot;A&amp;quot;"),
            '"A"',
        )

    def test_decode_stops_at_plain_ampersand(self):
        """解到不再含实体为止，但**不能**把裸 `&` 也吃掉。"""
        self.assertEqual(hhkan_snapshot._decode_entities("A&B"), "A&B")
        self.assertEqual(hhkan_snapshot._decode_entities("100% & 200%"), "100% & 200%")

    def test_decode_is_idempotent_after_convergence(self):
        """收敛后再解一次不应改变结果（避免重复读取时越解越少）。"""
        self.assertEqual(hhkan_snapshot._decode_entities("诛仙1&amp;2季"), "诛仙1&2季")
        once = hhkan_snapshot._decode_entities("&amp;quot;A&amp;quot;")
        self.assertEqual(hhkan_snapshot._decode_entities(once), once)

    def test_decode_handles_non_string_values(self):
        self.assertIsNone(hhkan_snapshot._decode_entities(None))
        self.assertEqual(hhkan_snapshot._decode_entities(123), 123)

    def test_detail_returns_decoded_desc(self):
        catalog = hhkan_snapshot.SnapshotCatalog({
            "source": "hhkan",
            "details": {
                "1": {
                    "id": 1,
                    "title": "诛仙1&amp;2季",
                    "desc": "他说：&quot;走&quot; &#039;别&#039; 留下 &gt; 危险",
                    "sources": [{
                        "name": "线路",
                        "episodes": [
                            {"index": 1, "name": "回顾特辑:五哈团被&quot;黑粉&quot;疯狂追击"},
                        ],
                    }],
                },
            },
        })
        detail = catalog.detail(1)
        self.assertEqual(detail["title"], "诛仙1&2季")
        self.assertEqual(detail["desc"], "他说：\"走\" '别' 留下 > 危险")
        self.assertEqual(
            detail["sources"][0]["episodes"][0]["name"],
            "回顾特辑:五哈团被\"黑粉\"疯狂追击",
        )

    def test_search_and_channel_results_carry_no_entities(self):
        catalog = hhkan_snapshot.SnapshotCatalog({
            "source": "hhkan",
            "items": {"1": {"id": 1, "title": "诛仙1&amp;2季"}},
            "channels": {
                "1": {
                    "cid": 1,
                    "sections": [{
                        "name": "最近热门",
                        "items": [{
                            "id": 1,
                            "title": "Troy &#039;Assassin&#039;",
                            "remark": "评分&#039;8.5&#039;",
                        }],
                    }],
                },
            },
        })
        found = catalog.search("诛仙")
        self.assertEqual(found[0]["title"], "诛仙1&2季")
        channel = catalog.channel(1)
        item = channel["sections"][0]["items"][0]
        self.assertEqual(item["title"], "Troy 'Assassin'")
        self.assertEqual(item["remark"], "评分'8.5'")

    def test_shipped_snapshot_has_no_entity_left_after_reading(self):
        """真实随包快照：经 detail() 读出的文本不得再含实体。"""
        catalog = hhkan_snapshot.SnapshotCatalog.load_default()
        self.assertTrue(catalog.available, "随包快照不可用，测试无法验证")
        details = catalog.data.get("details") or {}
        self.assertTrue(details, "随包快照没有 details")

        offenders = []
        for vid in list(details)[:200]:
            detail = catalog.detail(vid)
            if not detail:
                continue
            for field in ("title", "desc", "meta", "director", "actors"):
                value = detail.get(field)
                if isinstance(value, str) and ENTITY.search(value):
                    offenders.append((vid, field, ENTITY.findall(value)[:3]))
        self.assertEqual(
            offenders, [],
            "detail() 仍返回带 HTML 实体的文本：%r" % (offenders[:5],),
        )

    def test_shipped_snapshot_raw_json_still_contains_entities(self):
        """反向确认：磁盘上的 json 确实脏（否则上面的测试就是假通过）。"""
        raw = hhkan_snapshot.SNAPSHOT_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        found = [p for p, s in _walk_strings(data) if ENTITY.search(s)]
        self.assertTrue(
            found,
            "随包快照里已经没有 HTML 实体了——本测试的前提失效，需重新评估",
        )


class HhkanLiveDetailEntityTest(unittest.TestCase):
    """实时抓取路径也必须解码（BUG-02 的另一半）。

    `/hhkan/detail` 的实现是「先实时抓取，失败才回落快照」：

        d = hhkan.parse_detail(_hhkan_origin_call(lambda: hhkan.get_page(...)), vid)
        except Exception: d = _HHKAN_SNAPSHOT.detail(vid) or {}

    所以**实时抓取才是常态路径**，快照只是容灾。只修快照等于没修——
    实机验证时接口仍返回 `&#039;` 就是这么来的。

    `hhkan.parse_detail` 里 `desc` 只做了「去标签 + 替换 <br>」，从未调用
    含 `html.unescape` 的 `_clean_text`；`title` 更是直接 `re.search` 出来的
    原文。源站页面里这些字段是 HTML 转义过的，于是字面量原样进客户端。
    """

    DETAIL_HTML = (
        '<div class="detail-title"><strong>求救信号</strong>'
        '<strong>Troy &#039;Assassin&#039;</strong></div>'
        '<div class="detail-pic"><img data-original="https://img/1.jpg"></div>'
        '<a class="detail-tags-item">动作</a>'
        '<div class="detail-info-row-main">导演: 张三</div>'
        '<div class="detail-desc">\s*<p>\s*'
        '他被迫说出：&quot;走&quot; 与 &#039;别&#039; 留下 &amp; 危险 &gt; 深渊'
        '</p></div>'
    )

    def test_live_detail_decodes_entities_in_desc(self):
        detail = hhkan.parse_detail(self.DETAIL_HTML.replace(r"\s*", ""), 1)
        self.assertNotRegex(detail["desc"], ENTITY)
        self.assertIn('"走"', detail["desc"])
        self.assertIn("'别'", detail["desc"])
        self.assertIn("& 危险", detail["desc"])
        self.assertIn("> 深渊", detail["desc"])

    def test_live_detail_decodes_entities_in_title(self):
        detail = hhkan.parse_detail(self.DETAIL_HTML.replace(r"\s*", ""), 1)
        self.assertNotRegex(detail["title"], ENTITY)
        self.assertIn("'Assassin'", detail["title"])

    def test_live_detail_keeps_plain_text_untouched(self):
        html = (
            '<div class="detail-title"><strong>片</strong><strong>普通标题</strong></div>'
            '<div class="detail-desc"><p>AT&T 正常简介 A&B</p></div>'
        )
        detail = hhkan.parse_detail(html, 1)
        self.assertEqual(detail["title"], "普通标题")
        self.assertIn("AT&T", detail["desc"])
        self.assertIn("A&B", detail["desc"])


if __name__ == "__main__":
    unittest.main()
