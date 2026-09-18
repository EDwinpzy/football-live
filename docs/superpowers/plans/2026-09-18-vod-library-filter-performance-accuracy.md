# 片库筛选性能与准确度重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让片库（「全部影视」）筛选在设备内嵌后端里做到「每条都真匹配、上游可用时来自豆瓣全站、上游不可用时不假空」。

**Architecture:** 仍以豆瓣 Explore 接口为唯一数据源。三处核心改动：① 按豆瓣语义传参（`genres`/`countries`/`year_range`），上游筛过的维度本地不再复判；② 本地只做三态严格判定（未知剔除），且只为「上游不可信」的维度按需补详情；③ 上游异常（空壳响应）与「真的没结果」分开，异常走降级链且绝不写缓存。

**Tech Stack:** Python 3（Chaquopy 内嵌后端，标准库 only）、unittest、Kotlin/Jetpack Compose、原生 JS + HTML/CSS。

**Spec:** `docs/superpowers/specs/2026-09-18-vod-library-filter-performance-accuracy-design.md`

## Global Constraints

- 不新增任何后台服务、第三方代理、公开镜像或第三方代码依赖；后端只用 Python 标准库。
- 片库筛选必须保持「豆瓣全站实时检索」：上游可用时结果来自豆瓣，不缩水到本地目录。
- 准确优先：宁可少给，不给错；判据未知一律剔除。
- 上游异常的响应一律不得写入缓存。
- 改动范围仅限片库筛选；不动播放线路、详情页数据链路、直播。
- 发布收尾按仓库 `AGENTS.md` 的长期授权执行（构建 → 部署 → 双 APK → 热更 → 文档 → 推送），无需再逐项确认。

---

### Task 1: 上游空壳响应不得当成空结果（降级链 + 冷却）

**Files:**
- Modify: `tools/backend-src/douban_catalog.py`
- Test: `tools/backend-src/tests/test_douban_catalog.py`

**Interfaces:**
- Produces: `DoubanUnavailable(RuntimeError)`；`explore_rows(payload) -> list`（不可用即抛）；`DOUBAN_UNAVAILABLE_COOLDOWN = 60.0`；`DoubanCatalog._mark_unavailable()` / `_upstream_cooling()`；所有 `home`/`show`/`filters` 响应新增布尔字段 `upstream_available`。
- Consumes: 现有 `_coalesced(key, kind, producer, snapshot_value)`、`JsonDiskCache.get_stale`。

- [ ] **Step 1: 写失败测试**

```python
    def test_blocked_upstream_response_is_never_cached_as_empty(self):
        """豆瓣对异常 IP 回 HTTP 200 + {"msg": ...}（无 data 键）。旧实现按「没有结果」处理，
        空结果被写进 6 小时缓存，用户看到假空且恢复后也不刷新。"""
        cache = douban_catalog.JsonDiskCache(tempfile.mkdtemp())
        blocked = {"msg": "检测到有异常请求从您的IP发出，请登录再试", "r": 1}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: blocked, cache=cache, snapshot={})
        with self.assertRaises(douban_catalog.DoubanUnavailable):
            douban_catalog.explore_rows(blocked)
        result = catalog.show("movie", region="日本", page=1)
        self.assertFalse(result["upstream_available"])
        self.assertEqual(result["source"], "douban-unavailable")
        time.sleep(0.2)
        self.assertEqual(list(pathlib.Path(cache.directory).rglob("*.json")), [])

    def test_blocked_upstream_falls_back_to_last_good_result(self):
        cache = douban_catalog.JsonDiskCache(tempfile.mkdtemp())
        good = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(
            [{"id": "1", "title": "好结果", "rate": "9.0"}]), cache=cache, snapshot={})
        good.show("movie", page=1)
        blocked = douban_catalog.DoubanCatalog(
            fetch=lambda _url: {"msg": "异常"}, cache=cache, snapshot={})
        result = blocked.show("movie", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["好结果"])
        self.assertEqual(result["source"], "douban-cache")
        self.assertTrue(result["stale"])
        self.assertFalse(result["upstream_available"])

    def test_valid_empty_page_keeps_upstream_available_true(self):
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload([]), snapshot={})
        result = catalog.show("movie", genre="灾难", page=1)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["upstream_available"])
        self.assertEqual(result["source"], "douban")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q -k "blocked_upstream or valid_empty_page"`
Expected: FAIL —`AttributeError: module 'douban_catalog' has no attribute 'DoubanUnavailable'`

- [ ] **Step 3: 实现**

在 `douban_catalog.py` 常量区加入冷却时长，并新增异常类型与响应校验：

```python
# 上游被拦（豆瓣回 HTTP 200 + {"msg": ...}）后的冷却：冷却期内不再打上游，
# 到期自动放行一次真实回源试探，成功即恢复正常。
DOUBAN_UNAVAILABLE_COOLDOWN = 60.0


class DoubanUnavailable(RuntimeError):
    """上游异常（IP 被拦 / 非 JSON / 非 200 / 缺 data 键）——不是「没有结果」。"""


def explore_rows(payload):
    """校验 Explore 响应形状；异常一律抛 [DoubanUnavailable]。

    合法的空结果是 `{"data": []}`；豆瓣拦 IP 时回的是 `{"msg": ...}`（没有 data 键）。
    旧实现把两者都当成空列表，于是空壳被当作成功结果写进 6 小时缓存。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        message = payload.get("msg") if isinstance(payload, dict) else ""
        raise DoubanUnavailable(str(message or "unexpected Douban explore payload"))
    return payload["data"]
```

`_explore` 改用 `explore_rows(payload)`；`_section_or_empty` 同时吞掉两种上游异常：

```python
    def _section_or_empty(self, category, sort):
        try:
            return self._section_items(category, sort)
        except (DoubanThrottled, DoubanUnavailable):
            return []
```

`DoubanCatalog.__init__` 增加 `self._unavailable_until = 0.0`，并实现冷却：

```python
    def _mark_unavailable(self):
        with self._condition_lock:
            self._unavailable_until = time.monotonic() + DOUBAN_UNAVAILABLE_COOLDOWN

    def _upstream_cooling(self):
        with self._condition_lock:
            return time.monotonic() < self._unavailable_until
```

`_coalesced` 里：冷却期内直接走回落，不调 producer；producer 抛异常时标记冷却并按
「同条件最近成功 → 随包快照 → 明确不可用」的顺序回落，同时统一写入 `upstream_available`：

```python
    def _coalesced(self, key, kind, producer, snapshot_value=None):
        ...  # 命中 fresh 缓存的分支：result["upstream_available"] = True 后 return
        if self._upstream_cooling():
            return self._degraded(key, snapshot_value)
        ...  # 原有 stale / 冷缓存先回快照分支不变（标记 upstream_available=False）
        try:
            value = producer()
            if not value:
                raise ValueError("empty Douban response")
            value["upstream_available"] = True
            if self.cache:
                self.cache.put(key, value)
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError,
                DoubanUnavailable, DoubanThrottled):
            self._mark_unavailable()
            return self._degraded(key, snapshot_value)

    @staticmethod
    def _has_content(payload):
        """回落结果里到底有没有东西可给用户看（home 看 sections，show 看 items）。"""
        if not isinstance(payload, dict):
            return False
        if payload.get("items"):
            return True
        return any((section or {}).get("items") for section in (payload.get("sections") or []))

    def _degraded(self, key, snapshot_value):
        """上游不可用时的回落：同条件最近成功 → 随包快照 → 明确不可用。

        「不可用」与「没有符合条件的内容」必须能被区分：前者 `upstream_available=False`
        且 `source=douban-unavailable`，客户端显示「暂时获取不到，请重试」。
        """
        stale = self.cache.get_stale(key) if self.cache else None
        fallback = stale if stale is not None else copy.deepcopy(snapshot_value)
        if not isinstance(fallback, dict):
            fallback = {}
        if self._has_content(fallback):
            fallback["source"] = "douban-cache" if stale is not None else "douban-snapshot"
        else:
            fallback["source"] = "douban-unavailable"
        fallback["stale"] = True
        fallback["upstream_available"] = False
        return fallback
```

`show()`/`home()` 的返回值若来自 producer 之外的路径（快照兜底、降级），必须带
`upstream_available`；`filters()` 同样新增该字段（本任务先固定为「上游可达时为 True」）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q`
Expected: PASS（含既有 `test_filtered_show_empty_result_is_not_treated_as_throttling`：`{"data": []}` 是合法空结果）

- [ ] **Step 5: 提交**

```bash
git add tools/backend-src/douban_catalog.py tools/backend-src/tests/test_douban_catalog.py
git commit -m "fix(vod): 豆瓣空壳响应不再被当成空结果（降级链 + 冷却自愈）"
```

---

### Task 2: 上游参数按豆瓣语义传参

**Files:**
- Modify: `tools/backend-src/douban_catalog.py`
- Test: `tools/backend-src/tests/test_douban_catalog.py`

**Interfaces:**
- Produces: `early_year_cutoff(now=None) -> int`；`year_range_value(year) -> str`。
- Consumes: Task 1 的 `explore_rows`。

- [ ] **Step 1: 写失败测试**

```python
    def test_explore_uses_dedicated_country_and_year_params(self):
        """地区/年份必须走 countries= / year_range=，不能塞进 tags= ——否则豆瓣服务端
        根本没按地区筛，「选了地区像没选」就是这么来的。"""
        calls = []
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda url: calls.append(url) or search_payload([]), snapshot={})
        catalog.show("movie", region="日本", year="1994", page=1)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        self.assertEqual(query["tags"], ["电影"])
        self.assertEqual(query["countries"], ["日本"])
        self.assertEqual(query["year_range"], ["1994,1994"])

    def test_early_year_uses_a_range_instead_of_being_ignored(self):
        """「更早」旧实现等价于不筛选（网页端直接发空年份）。"""
        current = time.localtime().tm_year
        self.assertEqual(douban_catalog.year_range_value("更早"), "1,%d" % (current - 12))
        calls = []
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda url: calls.append(url) or search_payload([]), snapshot={})
        catalog.show("movie", year="更早", page=1)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        self.assertEqual(query["year_range"], ["1,%d" % (current - 12)])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q -k "dedicated_country or early_year"`
Expected: FAIL —`AttributeError: module 'douban_catalog' has no attribute 'year_range_value'`

- [ ] **Step 3: 实现**

```python
def early_year_cutoff(now=None):
    """「更早」的上界：年份 chip 覆盖 [今年 .. 今年-11]，所以更早 = 今年-12 及以前。"""
    return (now or time.localtime()).tm_year - 12


def year_range_value(year):
    """年份 chip → 豆瓣 `year_range` 参数。"""
    text = str(year or "").strip()
    if not text or text == "全部":
        return ""
    if text == "更早":
        return "1,%d" % early_year_cutoff()
    return "%s,%s" % (text, text) if text.isdigit() else ""
```

`_explore()` 的查询构造改为：

```python
        query = {"sort": sort, "range": rating_range(rating),
                 "tags": ",".join([EXPLORE_TAGS[category]]),
                 "start": max(0, int(page) - 1) * EXPLORE_PAGE_SIZE}
        if genre and genre != "全部":
            query["genres"] = genre
        if region and region != "全部":
            query["countries"] = region
        span = year_range_value(year)
        if span:
            query["year_range"] = span
        url = EXPLORE_API + "?" + urllib.parse.urlencode(query)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q`
Expected: PASS；同时更新既有 `test_show_passes_sort_and_filters_to_douban_search` 的断言
（`tags == ["电影"]`、新增 `countries == ["美国"]`、`year_range == ["1994,1994"]`）

- [ ] **Step 5: 提交**

```bash
git add tools/backend-src/douban_catalog.py tools/backend-src/tests/test_douban_catalog.py
git commit -m "fix(vod): 片库筛选改用豆瓣 countries/year_range 参数（更早用年份区间）"
```

---

### Task 3: 本地判定三态，且只为必要维度补详情

**Files:**
- Modify: `tools/backend-src/douban_catalog.py`
- Test: `tools/backend-src/tests/test_douban_catalog.py`

**Interfaces:**
- Produces: `DoubanCatalog._matches(item, genre="", region="", year="", rating="")`（严格三态）；`DoubanCatalog._enrich(items, needs=())`。
- Consumes: Task 4 的 `_filter_matrix()`（本任务先用 `{"movie": {"genre": True}}` 的临时表，Task 4 换成数据文件）。

- [ ] **Step 1: 写失败测试**

```python
    def test_unknown_metadata_is_dropped_instead_of_passed_through(self):
        """旧实现「缺字段就放行」，导致综艺/短剧筛选等于没筛（实测返回 20 条、真匹配 0 条）。"""
        rows = [
            {"id": "1", "title": "无元数据", "rate": "8.0"},
            {"id": "2", "title": "真日本片", "rate": "8.0", "card_subtitle": "2024 / 日本 / 剧情"},
        ]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows), snapshot={})
        result = catalog.show("variety", region="日本", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["真日本片"])

    def test_upstream_filtered_dimension_is_not_judged_again_locally(self):
        """电影的类型由豆瓣服务端筛（实测 `genre=` 有效），本地不得再判 ——否则列表行
        没带 genres 的条目会被误杀。"""
        rows = [{"id": "1", "title": "科幻片", "rate": "8.0"}]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows), snapshot={})
        result = catalog.show("movie", genre="科幻", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["科幻片"])

    def test_detail_is_fetched_only_for_rows_missing_the_needed_field(self):
        """带筛选不再逐条补详情：只有缺判据字段的行才补。"""
        fetched = []

        def fetch(url):
            if "/subject/" in url:
                fetched.append(url.rsplit("/", 1)[-1])
                return {"title": "补详情", "countries": ["日本"], "genres": ["剧情"]}
            return search_payload([
                {"id": "1", "title": "有元数据", "rate": "8.0", "card_subtitle": "2024 / 日本 / 剧情"},
                {"id": "2", "title": "缺元数据", "rate": "8.0"},
            ])

        catalog = douban_catalog.DoubanCatalog(fetch=fetch, snapshot={})
        result = catalog.show("variety", region="日本", page=1)
        self.assertEqual([item["title"] for item in result["items"]], ["有元数据", "补详情"])
        self.assertEqual(fetched, ["2"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q -k "unknown_metadata or not_judged_again or only_for_rows"`
Expected: FAIL — 第一个用例返回两条（未知放行）、第三个用例会补两次详情。

- [ ] **Step 3: 实现**

`_matches` 改为严格三态（去掉「未知放行」），年份与 `early_year_cutoff()` 对齐：

```python
    @staticmethod
    def _matches(item, genre="", region="", year="", rating=""):
        """本地三态判定：只有明确匹配才通过，判据未知一律剔除。

        这个方法只在「上游不可信该维度」时被调用（见 `_filter_matrix`）；上游已经筛过的
        维度不会走到这里，所以不存在「该有却被本地砍掉」的情况。
        """
        if genre and genre != "全部" and genre not in (item.get("genres") or []):
            return False
        if region and region != "全部" and region not in (item.get("regions") or []):
            return False
        if year and year != "全部":
            value = str(item.get("year") or "")[:4]
            if not value.isdigit():
                return False
            if year == "更早":
                if int(value) > early_year_cutoff():
                    return False
            elif value != str(year):
                return False
        if rating and rating != "全部":
            score = float(item.get("rating") or 0)
            if rating == "暂无评分":
                if score > 0:
                    return False
            else:
                threshold = re.search(r"\d+", rating)
                if threshold and score > 0 and score < int(threshold.group(0)):
                    return False
        return True
```

`_enrich` 只在缺所需字段时补详情：

```python
    def _enrich(self, items, needs=()):
        """并发补详情，只补「缺 `needs` 里字段且没有 detail_meta」的条目。"""
        pending = [item for item in items if item.get("douban_id")
                   and not item.get("detail_meta")
                   and any(not item.get(field) for field in needs)]
        if not pending:
            return [dict(item) for item in items]
        ...  # 以下与现状一致（ENRICH_WORKERS + _DETAIL_GATE + _merge_detail）
```

`show()` 按「上游不可信」的维度决定本地判哪些、补哪些：

```python
    FIELD_OF = {"genre": "genres", "region": "regions", "year": "year"}

    def show(self, category, genre="", region="", year="", rating="", sort="hot", page=1):
        ...
        matrix = self._filter_matrix().get(category) or {}
        active = {"genre": genre, "region": region, "year": year, "rating": rating}
        active = {k: v for k, v in active.items() if v and v != "全部"}
        local = {k: v for k, v in active.items() if not matrix.get(k)}
        needs = tuple(self.FIELD_OF[k] for k in local if k in self.FIELD_OF)
        upstream = {k: "" if k in local else v for k, v in active.items()}

        def produce():
            ...
            candidates = self._enrich(fresh, needs) if needs else fresh
            for item in candidates:
                if self._matches(item, upstream.get("genre", ""), upstream.get("region", ""),
                                  upstream.get("year", ""), upstream.get("rating", "")):
                    collected.append(item)
```

注意 `_matches` 的 `rating` 维度始终本地判（评分来自列表行的 `rate`，永远已知，不涉及未知剔除），
因此 `upstream["rating"]` 传 `""`，由本地判。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q`
Expected: PASS；既有 `test_explore_rows_keep_card_subtitle_so_area_filter_works`、
`test_type_filter_drops_non_matching_rows_locally`、`test_filtered_show_enriches_rows_when_list_has_no_regions` 保持通过

- [ ] **Step 5: 提交**

```bash
git add tools/backend-src/douban_catalog.py tools/backend-src/tests/test_douban_catalog.py
git commit -m "fix(vod): 筛选判据改三态（未知剔除）+ 只在缺字段时补详情"
```

---

### Task 4: 分类 × 参数能力矩阵数据化与探测脚本

**Files:**
- Create: `tools/backend-src/douban_filter_matrix.json`
- Create: `tools/probe_douban_filter_matrix.py`
- Modify: `tools/backend-src/douban_catalog.py`
- Modify: `tools/release_all.py`
- Test: `tools/backend-src/tests/test_douban_catalog.py`

**Interfaces:**
- Produces: `FILTER_MATRIX_PATH`；`DEFAULT_FILTER_MATRIX`；`DoubanCatalog._filter_matrix() -> dict`；探测脚本 `probe(matrix_path, fetch, sleep) -> dict`。
- Consumes: Task 3 的 `show()` 判定流程。

- [ ] **Step 1: 写失败测试**

```python
    def test_matrix_file_makes_upstream_trusted_for_movie_genre(self):
        """矩阵数据说了算：实测某分类支持某参数，就把该维度交给上游，本地不复判。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "douban_filter_matrix.json"
            path.write_text(json.dumps({"movie": {"genre": True}}), encoding="utf-8")
            catalog = douban_catalog.DoubanCatalog(
                fetch=lambda _url: search_payload([{"id": "1", "title": "无元数据", "rate": "8"}]),
                snapshot={}, matrix_path=path)
            self.assertTrue(catalog._filter_matrix()["movie"]["genre"])
            self.assertEqual([i["title"] for i in catalog.show("movie", genre="科幻")["items"]],
                             ["无元数据"])

    def test_missing_matrix_file_falls_back_to_conservative_defaults(self):
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda _url: search_payload([]), snapshot={},
            matrix_path=pathlib.Path(tempfile.mkdtemp()) / "absent.json")
        matrix = catalog._filter_matrix()
        self.assertTrue(matrix["movie"]["genre"])
        self.assertFalse(matrix["tv"]["genre"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q -k matrix`
Expected: FAIL —`TypeError: __init__() got an unexpected keyword argument 'matrix_path'`

- [ ] **Step 3: 实现**

`douban_catalog.py`：

```python
FILTER_MATRIX_PATH = Path(__file__).with_name("douban_filter_matrix.json")
# 无实测数据时的保守缺省：只有电影类型有直接证据（线上 `genre=` 返回正常结果），
# 其余一律本地判定 —— 宁可少给，不给错。探测脚本会在发布流程里用实测覆盖这份缺省。
FILTER_DIMENSIONS = ("genre", "region", "year", "rating")
DEFAULT_FILTER_MATRIX = {
    "movie": {"genre": True, "region": True, "year": True, "rating": True},
    "tv": {"genre": False, "region": False, "year": False, "rating": True},
    "anime": {"genre": False, "region": False, "year": False, "rating": True},
    "variety": {"genre": False, "region": False, "year": False, "rating": True},
    "short": {"genre": False, "region": False, "year": False, "rating": True},
}
```

`DoubanCatalog.__init__(self, fetch=None, cache=None, snapshot=None, media_index=None, matrix_path=None)`
保存 `self._matrix_path = Path(matrix_path) if matrix_path else FILTER_MATRIX_PATH`，
实现：

```python
    def _filter_matrix(self):
        """分类 → 上游是否可信该维度。缺文件/字段时用保守缺省补齐，绝不抛错。"""
        cached = getattr(self, "_matrix_cache", None)
        if cached is not None:
            return cached
        matrix = {category: dict(DEFAULT_FILTER_MATRIX[category]) for category in CATEGORY_VALUES}
        try:
            raw = json.loads(self._matrix_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
        for category, values in (raw.items() if isinstance(raw, dict) else []):
            if category not in matrix or not isinstance(values, dict):
                continue
            for dimension in FILTER_DIMENSIONS:
                if isinstance(values.get(dimension), bool):
                    matrix[category][dimension] = values[dimension]
        self._matrix_cache = matrix
        return matrix
```

`tools/probe_douban_filter_matrix.py`（只用标准库，直接复用 catalog 的 fetch 与参数构造）：
对 5 个分类 × `{"genre": "剧情", "region": "日本", "year": "更早"}` 各发一次带该参数、
其余为空的请求，若返回的行数大于「不带该参数」的行数差异显著则记 `True`，否则 `False`；
输出 `{"<category>": {"genre": bool, "region": bool, "year": bool, "rating": true}}`，
写盘用「临时文件 + replace」；任何一次请求失败就抛错退出，避免把半份数据写进包里。

`tools/release_all.py` 在刷新豆瓣快照成功后、打包前插入：

```python
    if not run([PY, os.path.join(ROOT, "tools", "probe_douban_filter_matrix.py")], ROOT):
        print("[WARN] 筛选能力矩阵未刷新，沿用现有 douban_filter_matrix.json 继续构建")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q`
Expected: PASS

- [ ] **Step 5: 生成一份初版矩阵数据并提交**

Run: `python tools/probe_douban_filter_matrix.py`
（若当前网络被豆瓣拦，探测会失败退出；此时提交脚本里的保守缺省，矩阵文件留待发布流程生成。）

```bash
git add tools/backend-src/douban_catalog.py tools/backend-src/douban_filter_matrix.json \
        tools/probe_douban_filter_matrix.py tools/release_all.py \
        tools/backend-src/tests/test_douban_catalog.py
git commit -m "feat(vod): 分类×参数能力矩阵数据化 + 发布期探测脚本"
```

---

### Task 5: 分页与 has_more 诚实化

**Files:**
- Modify: `tools/backend-src/douban_catalog.py`
- Test: `tools/backend-src/tests/test_douban_catalog.py`

**Interfaces:**
- Consumes: Task 3 的 `show()`。
- Produces: `show()` 的 `has_more` 语义 = 「最后一次上游请求返回了满页」。

- [ ] **Step 1: 写失败测试**

```python
    def test_has_more_is_false_when_upstream_runs_out(self):
        """旧实现带筛选时用 `bool(rows)` 判断，翻页会出现「加载更多」点下去是空的。"""
        catalog = douban_catalog.DoubanCatalog(
            fetch=lambda _url: search_payload([{"id": "1", "title": "唯一一条", "rate": "8"}]),
            snapshot={})
        self.assertFalse(catalog.show("movie", region="日本", page=1)["has_more"])

    def test_has_more_is_true_when_last_upstream_page_is_full(self):
        rows = [{"id": str(i), "title": "片%d" % i, "rate": "8.0",
                 "card_subtitle": "2024 / 日本 / 剧情"} for i in range(douban_catalog.EXPLORE_PAGE_SIZE)]
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: search_payload(rows), snapshot={})
        self.assertTrue(catalog.show("movie", region="日本", page=1)["has_more"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q -k has_more`
Expected: 第一个用例 FAIL（旧逻辑因 `bool(rows)` 判成 True）

- [ ] **Step 3: 实现**

`show().produce()` 记录最后一次上游返回是否满页，并据此定 `has_more`：

```python
            last_full = False
            for p in pages:
                rows = self._explore(...)
                last_full = len(rows) >= EXPLORE_PAGE_SIZE
                if not rows:
                    break
                ...
            return {"category": category, "page": int(page), "items": items,
                    "has_more": last_full, "source": "douban", "stale": False}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tools/backend-src/douban_catalog.py tools/backend-src/tests/test_douban_catalog.py
git commit -m "fix(vod): 片库 has_more 只在最后一页满载时为真"
```

---

### Task 6: 筛选项三层合并（含快照实测汇总）

**Files:**
- Modify: `tools/backend-src/douban_catalog.py`
- Test: `tools/backend-src/tests/test_douban_catalog.py`

**Interfaces:**
- Consumes: Task 1 的 `DoubanCatalog.snapshot_subjects()`。
- Produces: `filters()` 返回的 `types`/`areas` 为「内置词汇表 ∪ 随包快照真实出现值」，`rows` 结构不变。

- [ ] **Step 1: 写失败测试**

```python
    def test_filter_options_merge_values_seen_in_snapshot(self):
        """筛选项要覆盖真实出现过的值，否则会出现「点了没内容」。"""
        snapshot = {"index": [{"douban_id": "1", "title": "片", "genres": ["西部"],
                               "regions": ["爱尔兰"]}]}
        catalog = douban_catalog.DoubanCatalog(fetch=lambda _url: {}, snapshot=snapshot)
        result = catalog.filters("movie")
        self.assertIn("西部", result["types"])
        self.assertIn("爱尔兰", result["areas"])
        self.assertEqual(result["types"][0], "全部")
        self.assertEqual(result["areas"][0], "全部")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q -k merge_values_seen`
Expected: FAIL —`'西部' not found in [...]`

- [ ] **Step 3: 实现**

```python
    def _snapshot_facets(self, category):
        """随包快照里该分类真实出现过的类型/地区（保证筛选项点了有内容）。"""
        genres, regions = [], []
        for item in self.snapshot_subjects():
            if str(item.get("category") or "") != category:
                continue
            genres.extend(item.get("genres") or [])
            regions.extend(item.get("regions") or [])
        return genres, regions

    @staticmethod
    def _merge_options(base, extra):
        """保留 base 的顺序与「全部」在首位，附加 extra 里未出现过的值。"""
        options = list(base)
        for value in extra:
            name = str(value or "").strip()
            if name and name != "全部" and name not in options:
                options.append(name)
        return options
```

`filters()` 里对 `genres`/`regions` 各套一次 `_merge_options`，`rows` 结构保持不变，
并补上 `upstream_available`（本任务固定 `True`：筛选项来自本地词汇表与快照，不依赖本次上游调用）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tools/backend-src/tests/test_douban_catalog.py -q`
Expected: PASS（含既有 `test_filters_offer_the_full_douban_type_vocabulary`）

- [ ] **Step 5: 提交**

```bash
git add tools/backend-src/douban_catalog.py tools/backend-src/tests/test_douban_catalog.py
git commit -m "feat(vod): 筛选项合并随包快照真实值，减少「点了没内容」"
```

---

### Task 7: 三端前端接线（去本地补丁 + 降级提示）

**Files:**
- Modify: `tools/backend-src/www/app.js`
- Modify: `android/app/src/main/java/com/qiubo/optimaltv/ui/all/AllScreen.kt`
- Modify: `internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/all/AllScreen.kt`
- Test: `tools/backend-src/tests/test_web_douban_contract.py`

**Interfaces:**
- Consumes: 后端 `/vod/show/{category}` 与 `/vod/filters/{category}` 的 `stale`/`source`/`upstream_available`。

- [ ] **Step 1: 写失败测试（网页契约）**

```python
def test_web_sends_early_year_instead_of_blanking_it():
    """旧实现把「更早」发成空串，等于不筛选。"""
    source = (BACKEND / "www" / "app.js").read_text(encoding="utf-8")
    assert 'all.sel.year === "更早" ? ""' not in source
    assert "upstream_available" in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tools/backend-src/tests/test_web_douban_contract.py -q -k early_year`
Expected: FAIL

- [ ] **Step 3: 实现**

`www/app.js`：`loadAllPage()` 的查询参数改为直接传 `year: all.sel.year`；`renderAllFilters()` 与
片库页读取响应的 `upstream_available`，为 false 时按「有内容 → 数据可能不是最新」「无内容 →
暂时获取不到，请重试」两种文案渲染，沿用页面现有 toast/提示样式，不新增组件。

`AllScreen.kt`（两端）：`showPage()` 返回对象补 `upstreamAvailable` 字段（`VodApiSource.fetchShow`
解析 `upstream_available`），筛选区下方按同一语义显示一行轻提示；`year` 仍传「更早」原值。

- [ ] **Step 4: 跑测试与编译确认通过**

Run: `python -m pytest tools/backend-src/tests/test_web_douban_contract.py -q`
Run: `cd android; .\gradlew.bat assembleDebug`
Expected: 测试通过、编译通过

- [ ] **Step 5: 提交**

```bash
git add tools/backend-src/www/app.js android/app/src/main/java/com/qiubo/optimaltv/ui/all/AllScreen.kt \
        internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/all/AllScreen.kt \
        android/app/src/main/java/com/qiubo/optimaltv/data/source/VodApiSource.kt \
        internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/source/VodApiSource.kt \
        tools/backend-src/tests/test_web_douban_contract.py
git commit -m "fix(vod): 三端片库筛选去掉本地补丁并显示降级提示"
```

---

### Task 8: 准确度自检脚本（可实测证据）

**Files:**
- Create: `tools/check_vod_filter_accuracy.py`

**Interfaces:**
- Consumes: `tools/backend-src/douban_catalog.py`。

- [ ] **Step 1: 写脚本**

对 5 个分类 × {类型, 地区, 年份, 评分} 各取一个代表值，调用 `show()`，逐条断言
「条目的元数据明确包含该筛选值」（沿用 `_matches` 的判据，但只在字段存在时断言），
输出 `out/vod-filter-accuracy-<timestamp>.json`，内容含：分类、维度、取值、返回条数、
断言通过条数、失败明细、耗时毫秒。

- [ ] **Step 2: 运行并确认输出**

Run: `python tools/check_vod_filter_accuracy.py`
Expected: 生成 JSON；`failed` 明细为空（上游不可用时脚本应明确报告 `upstream_available=false`
并跳过断言，而不是把降级结果当成通过）

- [ ] **Step 3: 提交**

```bash
git add tools/check_vod_filter_accuracy.py
git commit -m "test(vod): 片库筛选准确度自检脚本（输出证据 JSON）"
```

---

### Task 9: 本地构建与验证（发布边界，到此为止）

**Files:**
- 无源码改动；产出物归档到 `apk/TV版/`、`apk/移动版/`

- [ ] **Step 1: 后端全量测试**

Run: `python -m pytest tools/backend-src/tests -q`
Expected: 全绿

- [ ] **Step 2: 重建产物（含快照整理）**

Run: `python tools/release_all.py`
Expected: 成功；`tools/backend-src/www/` 与内嵌后端包内容一致

- [ ] **Step 3: 两端 release 构建**

Run: `cd android; .\gradlew.bat assembleRelease`
Run: `cd internal/mobile; .\gradlew.bat assembleRelease`
Expected: BUILD SUCCESSFUL，两个 APK 自动归档

- [ ] **Step 4: 端到端截图**

TV 模拟器与网页五视口各跑一遍片库筛选路径并截图，存到 `docs/assets/`；记录冷/热缓存首屏耗时。

- [ ] **Step 5: 提交验证记录**

```bash
git add docs/04-records/测试记录.md docs/assets
git commit -m "docs(qa): 片库筛选重构实测记录"
```

---

### Task 10: 发布与记录

**说明：** 本仓库 `AGENTS.md` 已有用户长期授权（「以后更新之后自动构建 APK、自动发布、自动更新文档、
自动上传、自动推送」），因此本任务无需再逐项征求发布确认；若用户在本轮显式收回该授权，则停止并改为询问。

- [ ] **Step 1: 部署网页版并发布热更**

Run: `python tools/release_all.py --deploy`
Run: `node 后台管理系统/scripts/ops/hu-upload.mjs --name "<名称>" --notes "<备注>"`

- [ ] **Step 2: 线上自检**

比对线上 `/web/app.js` 与 `tools/backend-src/www/app.js`、线上热更包与本地 zip 的 SHA-256，
以及两个 APK 内嵌 zip 的四个关键文件与 `tools/backend-src` 逐一一致。

- [ ] **Step 3: 更新文档并推送**

`docs/04-records/版本史.md` 追加变更摘要、`docs/04-records/测试记录.md` 追加实测证据，
然后 `git add -A` → commit → `git push origin main`。
