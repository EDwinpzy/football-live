"""好好看目录快照容灾。

快照只包含由好好看页面解析得到的目录、卡片和详情。它不读取 macCMS，
因此云函数被源站风控时也不会让补充播放源反向成为卡片数据源。
"""
from __future__ import annotations

import copy
import html
import json
import re
from pathlib import Path


SNAPSHOT_PATH = Path(__file__).with_name("hhkan_snapshot.json")

_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#\d{2,6}|#x[0-9a-fA-F]{2,6});")

# 双重转义在随包快照里是真实存在的（见 _decode_entities 注释），所以要多解几轮。
# 上限只是防御：正常数据一两轮就收敛，`decoded == value` 会提前退出。
_MAX_DECODE_PASSES = 5


def _decode_entities(value):
    """还原一次 HTML 实体。

    快照是离线抓取时落盘的，落盘前没走 `hhkan._clean_text()`（其中含
    `html.unescape`），所以实体被固化进 json。读取端若不解码，客户端就会
    直接上屏 `&#039;` / `&quot;` / `&amp;` 字面量（BUG-02）。

    解到**不再含实体**为止。随包快照里确实存在双重转义——实测
    `details.378068.desc` 磁盘上是 `&amp;quot;Bridal Shower&amp;quot;`，
    只解一层会把 `&quot;` 字面量留给用户看，正是 BUG-02 的残留症状。
    循环以「本轮是否还检出实体」为终止条件：收敛后必然停下，
    而裸 `&`（`A&B`）不含分号实体，不会被 `html.unescape` 吃掉。

    非字符串原样返回——快照里 id/评分等字段是数字，不该被 coerce。
    """
    if not isinstance(value, str):
        return value
    for _ in range(_MAX_DECODE_PASSES):
        if not _ENTITY.search(value):
            break
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return value


def _decode_tree(node):
    """就地还原本节点树里的所有字符串实体（快照结构固定，全是 dict/list/str）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            node[key] = _decode_tree(value)
        return node
    if isinstance(node, list):
        for index, value in enumerate(node):
            node[index] = _decode_tree(value)
        return node
    return _decode_entities(node)


class SnapshotCatalog:
    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
        # 在构造时就解码：无论数据来自 load_default() 还是测试/调用方直接
        # `SnapshotCatalog(raw)`，「读出的文本不含 HTML 实体」这条不变式都成立。
        # 放在这里而不是各读取方法里，是为了不会漏掉某个新增的读取路径。
        self.data = _decode_tree(data) if data.get("source") == "hhkan" else {}

    @classmethod
    def load_default(cls):
        try:
            return cls(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return cls()

    @property
    def available(self):
        return bool(self.data.get("home") or self.data.get("channels"))

    @property
    def generated_at(self):
        return self.data.get("generatedAt", "")

    def _tag(self, payload):
        if not isinstance(payload, dict):
            return None
        result = copy.deepcopy(payload)
        result["source"] = "hhkan-snapshot"
        result["stale"] = True
        if self.generated_at:
            result["snapshotAt"] = self.generated_at
        return result

    def home(self):
        return self._tag(self.data.get("home"))

    def channel(self, cid):
        return self._tag((self.data.get("channels") or {}).get(str(cid)))

    def channel_sections(self, cid):
        return self._tag((self.data.get("channelSections") or {}).get(str(cid)))

    def filters(self, cid):
        return self._tag((self.data.get("filters") or {}).get(str(cid)))

    def show(self, cid, by="3", page=1, type_="", area="", lang="", year=""):
        # 部署快照保存未筛选的常用排序页。带筛选请求通过详情元数据在本地过滤，
        # 无法精确判断语言时宁可少返回，也不混入其他来源卡片。
        shows = self.data.get("shows") or {}
        if int(cid) == 0:
            merged, seen, more = [], set(), False
            for channel_id in (1, 2, 3, 4, 6):
                part = self.show(channel_id, by, page, type_, area, lang, year)
                if not part:
                    continue
                more = more or bool(part.get("has_more"))
                for item in part.get("items", []):
                    if item.get("id") not in seen:
                        seen.add(item.get("id"))
                        merged.append(item)
            return self._tag({"cid": 0, "page": int(page), "items": merged,
                              "has_more": more}) if merged else None
        key = "%s|%s|%s" % (cid, by or "3", page)
        payload = copy.deepcopy(shows.get(key))
        if not payload:
            return None
        if any((type_, area, lang, year)):
            details = self.data.get("details") or {}

            def matches(item):
                detail = details.get(str(item.get("id"))) or {}
                haystack = " / ".join(str(detail.get(k) or "") for k in
                                      ("meta", "year", "desc"))
                return all(not value or value in haystack
                           for value in (type_, area, lang, year))

            payload["items"] = [item for item in payload.get("items", []) if matches(item)]
            payload["has_more"] = False
        return self._tag(payload)

    def latest(self, page=1):
        return self._tag((self.data.get("latest") or {}).get(str(page)))

    def detail(self, vid):
        details = self.data.get("details") or {}
        payload = details.get(str(vid))
        if payload:
            return self._tag(payload)
        card = (self.data.get("items") or {}).get(str(vid))
        if not card:
            return None
        return self._tag({
            "id": int(vid),
            "title": card.get("title", ""),
            "cover": card.get("cover", ""),
            "score": card.get("score", 0),
            "year": "",
            "meta": card.get("remark", ""),
            "director": "",
            "actors": "",
            "desc": "",
            "sources": [],
        })

    def search(self, keyword, page=1, page_size=30):
        needle = "".join(str(keyword or "").lower().split())
        if not needle:
            return []
        found = []
        for item in (self.data.get("items") or {}).values():
            title = "".join(str(item.get("title") or "").lower().split())
            if needle in title:
                found.append(copy.deepcopy(item))
        start = max(0, int(page) - 1) * page_size
        return found[start:start + page_size]
