"""Device-local Douban catalogue parsing, caching and snapshot fallback."""

from __future__ import annotations

import copy
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path


HOME_SECTION_NAMES = ("最近热门", "最新上映", "豆瓣高分")
CATEGORY_VALUES = {"movie", "tv", "anime", "variety", "short"}
# 豆瓣 Explore 的搜索接口：`recent_hot` 会忽略 category/type 参数（三种排序拿到
# 同一份列表），只有这个接口真正支持排序与类型/地区/年份/评分筛选。
EXPLORE_API = "https://movie.douban.com/j/new_search_subjects"
EXPLORE_PAGE_SIZE = 20
EXPLORE_TAGS = {"movie": "电影", "tv": "电视剧", "anime": "动漫", "variety": "综艺", "short": "短剧"}
SORT_CODES = {"hot": "U", "new": "R", "rating": "S"}  # U 热门 / R 最新 / S 高分
# 豆瓣限流时会回 HTTP 200 + 空 data（tools/build_douban_snapshot.py 已按此处理）。
# 首页三栏是「不带筛选、按排序取前 20」，空列表只可能是限流；旧实现把它当成功结果
# 写进 6 小时缓存，用户看到「最近热门有内容、另外两栏空」且在过期前不再恢复。
EXPLORE_RETRY_DELAYS = (0.4, 1.2)
SUBJECT_FIELDS = (
    "id", "douban_id", "title", "original_title", "aliases", "year",
    "category", "genres", "regions", "season", "rating", "rating_count",
    "summary", "directors", "actors", "poster_url", "backdrop_url", "release_date",
)
TTL = {"home": 21600, "filters": 86400, "detail": 604800, "search": 1800, "show": 21600}
CACHE_SCHEMA = 1
CACHE_ENVELOPE_KEYS = {"schema", "fetched_at", "value"}
SNAPSHOT_PATH = Path(__file__).with_name("douban_snapshot.json")


def _text(value):
    return html.unescape(str(value or "")).strip()


def _list(value):
    if isinstance(value, list):
        return [_text(x.get("name") if isinstance(x, dict) else x) for x in value if x]
    if value:
        return [_text(value)]
    return []


def _number(value, default=0):
    if isinstance(value, dict):
        value = value.get("value", value.get("ratingValue", default))
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value, default=0):
    if isinstance(value, dict):
        value = value.get("count", value.get("ratingCount", default))
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_short_drama(item):
    # 列表接口只给 genres（由 card_subtitle 解析而来），详情接口才给 tags。
    tags = " ".join(_list(item.get("tags")) + _list(item.get("genres"))).lower()
    positive = ("短剧", "微短剧", "网络短剧", "竖屏剧", "迷你剧")
    return any(token in tags for token in positive)


def _category(item, requested="movie"):
    genres = set(_list(item.get("genres")))
    tags = set(_list(item.get("tags")))
    if is_short_drama(item):
        return "short"
    if "综艺" in genres or "真人秀" in genres or "综艺" in tags or "真人秀" in tags:
        return "variety"
    if "动画" in genres or "动画" in tags or "动漫" in tags:
        return "anime"
    return requested if requested in ("movie", "tv") else "movie"


_CARD_YEAR = re.compile(r"^\d{4}$")


def parse_card_subtitle(value):
    """解析列表接口的 `card_subtitle`（`年份 / 地区 / 类型 / 导演 / 主演`）。

    豆瓣列表接口只给这一行摘要，没有结构化的地区/类型/主创字段。不解析它，
    「全部」页的类型/地区/年份筛选拿不到数据，按主创搜索也无从索引。剧集条目
    常常省略末尾的导演/主演段，所以按位置对齐、按段数截断。
    """
    parts = [part.strip() for part in str(value or "").split("/")]
    parts = [part for part in parts if part]
    if not parts:
        return {}
    result = {}
    if _CARD_YEAR.match(parts[0]):
        result["year"] = parts[0]
        parts = parts[1:]
    for key, part in zip(("regions", "genres", "directors", "actors"), parts):
        result[key] = [name for name in re.split(r"[\s、]+", part) if name]
    return result


def _merge_card_subtitle(raw):
    """仅在条目缺少结构化字段时用 `card_subtitle` 补齐（详情响应里的字段优先）。"""
    if not raw.get("card_subtitle") or raw.get("genres") or raw.get("regions"):
        return raw
    merged = dict(raw)
    for key, value in parse_card_subtitle(raw.get("card_subtitle")).items():
        merged.setdefault(key, value)
    return merged


def normalize_subject(raw, requested_category="movie"):
    raw = _merge_card_subtitle(raw if isinstance(raw, dict) else {})
    subject_id = _text(raw.get("douban_id") or raw.get("id")).removeprefix("douban:")
    rating = raw.get("rating") or raw.get("aggregateRating") or {}
    poster = raw.get("poster_url") or raw.get("cover_url") or raw.get("image")
    if isinstance(raw.get("pic"), dict):
        poster = poster or raw["pic"].get("large") or raw["pic"].get("normal")
    result = {
        "id": "douban:" + subject_id if subject_id else "",
        "douban_id": subject_id,
        "title": _text(raw.get("title") or raw.get("name")),
        "original_title": _text(raw.get("original_title") or raw.get("originalName") or raw.get("alternateName")),
        "aliases": _list(raw.get("aliases") or raw.get("aka")),
        "year": _text(raw.get("year")),
        "category": _category(raw, requested_category),
        "genres": _list(raw.get("genres") or raw.get("genre")),
        "regions": _list(raw.get("regions") or raw.get("countries")),
        "season": _integer(raw.get("season"), 0),
        "rating": _number(rating),
        "rating_count": _integer(rating),
        "summary": _text(raw.get("summary") or raw.get("description") or raw.get("intro")),
        "directors": _list(raw.get("directors") or raw.get("director")),
        "actors": _list(raw.get("actors") or raw.get("actor")),
        "poster_url": _text(poster),
        "backdrop_url": _text(raw.get("backdrop_url")),
        "release_date": _text((raw.get("release_date") or raw.get("pubdate") or raw.get("datePublished") or [""])[0]
                              if isinstance(raw.get("release_date") or raw.get("pubdate"), list)
                              else raw.get("release_date") or raw.get("datePublished")),
    }
    if not result["year"] and result["release_date"]:
        result["year"] = result["release_date"][:4]
    return result


def parse_explore(payload, category="movie"):
    if not isinstance(payload, dict):
        return []
    rows = payload.get("items") or payload.get("subjects") or payload.get("subject_collection_items") or payload.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("subjects") or []
    return [normalize_subject(row, category) for row in rows if isinstance(row, dict)]


def rating_range(value):
    """`9分以上` → `9,10`（豆瓣 Explore 的 range 参数）。"""
    match = re.search(r"(\d+)\s*分", str(value or ""))
    return "%d,10" % int(match.group(1)) if match else "0,10"


def normalize_explore_row(raw, category="movie"):
    """Explore 搜索接口的一行（title/rate/cover/directors/casts）→ 目录条目。"""
    raw = raw if isinstance(raw, dict) else {}
    return normalize_subject({
        "id": raw.get("id"),
        "title": raw.get("title"),
        "poster_url": raw.get("cover"),
        "directors": raw.get("directors"),
        "actors": raw.get("casts"),
        "rating": {"value": raw.get("rate"), "count": 0},
        "url": raw.get("url"),
    }, category)


def _json_ld(source):
    match = re.search(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', source, re.I | re.S)
    if not match:
        return {}
    try:
        value = json.loads(html.unescape(match.group(1)).strip())
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def parse_subject_html(source, douban_id):
    data = _json_ld(source)
    info_match = re.search(r'<div[^>]+id=["\']info["\'][^>]*>(.*?)</div>', source, re.I | re.S)
    info = re.sub(r"<[^>]+>", "\n", info_match.group(1)) if info_match else ""
    info = html.unescape(info)
    alias_match = re.search(r"又名\s*:\s*([^\r\n<]+)", info)
    region_match = re.search(r"制片国家/地区\s*:\s*([^\r\n<]+)", info)
    data["id"] = str(douban_id)
    data["aliases"] = [x.strip() for x in re.split(r"\s*/\s*", alias_match.group(1)) if x.strip()] if alias_match else []
    data["regions"] = [x.strip() for x in re.split(r"\s*/\s*", region_match.group(1)) if x.strip()] if region_match else []
    item = normalize_subject(data)
    title = item["title"]
    year_match = re.search(r"\((\d{4})\)\s*$", title)
    if year_match:
        item["year"] = item["year"] or year_match.group(1)
        item["title"] = title[:year_match.start()].strip()
    return item


class JsonDiskCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)
        return self.directory / (safe + ".json")

    def put(self, key, value, fetched_at=None):
        envelope = {"schema": CACHE_SCHEMA, "fetched_at": fetched_at or time.time(), "value": value}
        path = self._path(key)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def envelope(self, key):
        try:
            value = json.loads(self._path(key).read_text(encoding="utf-8"))
            if isinstance(value, dict) and CACHE_ENVELOPE_KEYS <= set(value) and value["schema"] == CACHE_SCHEMA:
                return value
        except (OSError, ValueError, TypeError):
            pass
        return None

    def get(self, key, ttl, now=None):
        envelope = self.envelope(key)
        now = time.time() if now is None else now
        if envelope and now - float(envelope["fetched_at"]) <= ttl:
            return copy.deepcopy(envelope["value"])
        return None

    def get_stale(self, key, now=None):
        envelope = self.envelope(key)
        return copy.deepcopy(envelope["value"]) if envelope else None


class DoubanThrottled(ValueError):
    """豆瓣 Explore 限流（HTTP 200 + 空 data）：按失败处理，走缓存/快照回落。"""


def _section_items(value, title):
    """取（缓存或快照）分类首页里同名栏目的条目；没有则返回空列表。"""
    if not isinstance(value, dict):
        return []
    for section in value.get("sections") or []:
        if isinstance(section, dict) and section.get("title") == title:
            return [item for item in (section.get("items") or []) if isinstance(item, dict)]
    return []


class DoubanCatalog:
    def __init__(self, fetch=None, cache=None, snapshot=None, media_index=None):
        self.fetch = fetch or self._fetch
        self.cache = cache
        self.snapshot = snapshot if isinstance(snapshot, dict) else self._load_snapshot()
        self.media_index = media_index
        self._conditions = {}
        self._condition_lock = threading.Lock()

    def _ingest(self, items):
        if not self.media_index:
            return
        try:
            self.media_index.upsert_many([item for item in items or [] if isinstance(item, dict)])
        except Exception:
            # Search indexing must never make the catalogue response unavailable.
            pass

    @staticmethod
    def _load_snapshot():
        try:
            value = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _snapshot_section_items(self, category):
        """快照中该分类首页三段的条目并集。"""
        items = []
        sections = ((self.snapshot.get("home") or {}).get(category) or {}).get("sections") or []
        for section in sections:
            for item in section.get("items") or []:
                if isinstance(item, dict):
                    items.append(item)
        return items

    def _snapshot_detail(self, douban_id):
        """快照里预存的详情（云端出口到不了豆瓣，详情页只能靠它）。

        快照不可能给每条都补详情（每次发布都有限额），补不到的条目回落到快照
        列表行——标题/封面/评分/主创还在，总比详情页整页空掉强。
        """
        key = str(douban_id)
        details = self.snapshot.get("details")
        value = details.get(key) if isinstance(details, dict) else None
        if not isinstance(value, dict) or not value.get("title"):
            value = self._snapshot_lookup().get(key)
        if isinstance(value, dict) and value.get("title"):
            item = copy.deepcopy(value)
            item["source"] = "douban-snapshot"
            return item
        return None

    def _snapshot_lookup(self):
        cached = getattr(self, "_snapshot_index", None)
        if cached is None:
            cached = {str(item.get("douban_id") or ""): item for item in self.snapshot_subjects()}
            cached.pop("", None)
            self._snapshot_index = cached
        return cached

    def snapshot_subjects(self):
        """快照携带的全部条目：首页三段 + 搜索池 `index`（按豆瓣 ID 去重）。"""
        items = []
        for category in (self.snapshot.get("home") or {}):
            items.extend(self._snapshot_section_items(category))
        pool = self.snapshot.get("index")
        items.extend(pool if isinstance(pool, list) else [])
        unique, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("douban_id") or item.get("id") or "").removeprefix("douban:")
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def warm_index(self):
        """用内置快照预热搜索索引，冷实例无需触网即可本地搜索。

        云端出口到不了豆瓣，`home()` 只能回落到快照；若访客直接搜索、从未打开
        影视首页，索引就会是空的（v1.26 线上「搜索没有匹配的影片」）。索引里
        已有数据时立刻返回，避免每次启动重复写入。
        """
        if not self.media_index:
            return 0
        try:
            if self.media_index.count() > 0:
                return 0
        except Exception:
            pass
        items = self.snapshot_subjects()
        self._ingest(items)
        return len(items)

    @staticmethod
    def _fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/html",
                                                       "Referer": "https://m.douban.com/"})
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read().decode("utf-8", "replace")
            content_type = response.headers.get("Content-Type", "").lower()
            return json.loads(body) if "json" in content_type or body.lstrip().startswith(("{", "[")) else body

    def _coalesced(self, key, kind, producer, snapshot_value=None):
        if self.cache:
            fresh = self.cache.get(key, TTL[kind])
            if fresh is not None:
                return fresh
        with self._condition_lock:
            condition = self._conditions.setdefault(key, threading.Condition())
        with condition:
            if getattr(condition, "_running", False):
                condition.wait_for(lambda: not getattr(condition, "_running", False), timeout=15)
                fresh = self.cache.get(key, TTL[kind]) if self.cache else None
                if fresh is not None:
                    return fresh
            condition._running = True
        try:
            value = producer()
            if not value:
                raise ValueError("empty Douban response")
            if self.cache:
                self.cache.put(key, value)
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            stale = self.cache.get_stale(key) if self.cache else None
            fallback = stale if stale is not None else copy.deepcopy(snapshot_value)
            if isinstance(fallback, dict):
                fallback["stale"] = True
                fallback["source"] = "douban-cache" if stale is not None else "douban-snapshot"
            return fallback or {"stale": True, "source": "douban-snapshot"}
        finally:
            with condition:
                condition._running = False
                condition.notify_all()

    def _explore(self, category, sort, page=1, genre="", region="", year="", rating="", strict=False):
        """豆瓣 Explore 列表：排序与类型/地区/年份/评分筛选都在服务端完成。

        `strict=True`（首页三栏）：空 data 视为限流，退避重试后仍为空则抛
        [DoubanThrottled]，由上层回落最近一次缓存或随包快照——绝不把空栏当成功
        结果缓存。带筛选的「全部」页保持宽松：空结果是合法结果。
        """
        tags = [EXPLORE_TAGS[category]]
        if region and region != "全部":
            tags.append(region)
        if year and year not in ("全部", "更早"):
            tags.append(year)
        query = {"sort": sort, "range": rating_range(rating), "tags": ",".join(tags),
                 "start": max(0, int(page) - 1) * EXPLORE_PAGE_SIZE}
        if genre and genre != "全部":
            query["genres"] = genre
        url = EXPLORE_API + "?" + urllib.parse.urlencode(query)
        for attempt in range(len(EXPLORE_RETRY_DELAYS) + 1):
            payload = self.fetch(url)
            rows = payload.get("data") if isinstance(payload, dict) else None
            if rows:
                return [normalize_explore_row(row, category) for row in rows if isinstance(row, dict)]
            if not strict:
                return []
            if attempt < len(EXPLORE_RETRY_DELAYS):
                time.sleep(EXPLORE_RETRY_DELAYS[attempt])
        raise DoubanThrottled("empty Douban explore data for %s" % category)

    def home(self, category):
        if category not in CATEGORY_VALUES:
            raise ValueError("unsupported category")
        key = "home:" + category
        snapshot_value = ((self.snapshot.get("home") or {}).get(category))

        def produce():
            """三栏分别抓取：某栏被限流就用该分类上一版缓存/随包快照的同一栏补位。

            三栏全被限流才整体抛错，交给 [_coalesced] 回落——这样用户看到的
            三栏不会出现「一栏新鲜、两栏空」的错位，也不会把空栏写进 6 小时缓存。
            """
            previous = self.cache.get_stale(key) if self.cache else None
            sections, degraded = [], 0
            for title, sort in zip(HOME_SECTION_NAMES, ("U", "R", "S")):
                try:
                    items = self._explore(category, sort, strict=True)
                except DoubanThrottled:
                    items = []
                if not items:
                    items = _section_items(previous, title) or _section_items(snapshot_value, title)
                    degraded += 1
                sections.append({"title": title, "items": items})
            if degraded >= len(HOME_SECTION_NAMES):
                raise DoubanThrottled("all Douban home sections throttled")
            result = {"category": category, "sections": sections, "stale": False, "source": "douban"}
            if degraded:
                result["stale"] = True
                result["source"] = "douban-partial"
            return result

        result = self._coalesced(key, "home", produce, snapshot_value)
        self._ingest([item for section in (result.get("sections") or []) for item in (section.get("items") or [])])
        return result

    def filters(self, category):
        if category not in CATEGORY_VALUES:
            raise ValueError("unsupported category")
        current_year = time.localtime().tm_year
        genres = {
            "movie": ["全部", "剧情", "喜剧", "动作", "爱情", "科幻", "动画", "悬疑", "犯罪", "纪录片"],
            "tv": ["全部", "剧情", "喜剧", "爱情", "悬疑", "犯罪", "科幻"],
            "anime": ["全部", "动画", "奇幻", "冒险", "科幻", "喜剧"],
            "variety": ["全部", "真人秀", "音乐", "脱口秀", "歌舞"],
            "short": ["全部", "剧情", "喜剧", "爱情", "悬疑", "古装"],
        }[category]
        regions = ["全部", "中国大陆", "中国香港", "中国台湾", "美国", "英国", "日本", "韩国", "法国", "德国"]
        years = ["全部"] + [str(year) for year in range(current_year, current_year - 12, -1)] + ["更早"]
        ratings = ["全部", "9分以上", "8分以上", "7分以上", "暂无评分"]
        return {"category": category, "types": genres, "areas": regions, "years": years, "ratings": ratings, "rows": [
            {"name": "类别", "key": "category", "options": ["电影", "电视剧", "动漫", "综艺", "短剧"]},
            {"name": "类型", "key": "genre", "options": genres},
            {"name": "地区", "key": "region", "options": regions},
            {"name": "年份", "key": "year", "options": years},
            {"name": "评分", "key": "rating", "options": ratings},
            {"name": "排序", "key": "sort", "options": ["热门", "最新上映", "豆瓣高分"]},
        ], "source": "douban"}

    @staticmethod
    def _matches(item, genre="", region="", year="", rating=""):
        """快照兜底的筛选：条目缺某个字段时放行。

        列表接口本身不含地区/类型/年份，快照里只有补过详情的条目才有；把「未知」
        当作「不匹配」会让云端「全部」页在筛选后直接空掉。
        """
        if genre and genre != "全部" and item.get("genres") and genre not in item["genres"]:
            return False
        if region and region != "全部" and item.get("regions") and region not in item["regions"]:
            return False
        if year and year != "全部" and item.get("year"):
            if year == "更早":
                if int(str(item["year"])[:4] or 0) >= time.localtime().tm_year - 11:
                    return False
            elif str(item["year"]) != year:
                return False
        if rating and rating != "全部":
            score = float(item.get("rating") or 0)
            if rating == "暂无评分":
                if score > 0:
                    return False
            else:
                threshold_match = re.search(r"\d+", rating)
                if threshold_match and score > 0 and score < int(threshold_match.group(0)):
                    return False
        return True

    def show(self, category, genre="", region="", year="", rating="", sort="hot", page=1):
        if category not in CATEGORY_VALUES:
            raise ValueError("unsupported category")
        sort_code = SORT_CODES.get(sort, "U")
        key = "show:%s:%s:%s:%s:%s:%s:%s" % (category, genre, region, year, rating, sort_code, page)

        def produce():
            items = self._explore(category, sort_code, page, genre, region, year, rating)
            return {"category": category, "page": int(page), "items": items,
                    "has_more": len(items) >= EXPLORE_PAGE_SIZE, "source": "douban", "stale": False}

        # 快照兜底也遵守筛选与分页契约：否则云端「全部」页会忽略用户筛选、
        # 并在一次响应里吐出整份快照。
        snapshot_items = [item for item in self._snapshot_section_items(category)
                          if self._matches(item, genre, region, year, rating)]
        if sort_code == "S":
            snapshot_items.sort(key=lambda item: -float(item.get("rating") or 0))
        elif sort_code == "U":
            snapshot_items.sort(key=lambda item: -int(item.get("rating_count") or 0))
        else:
            snapshot_items.sort(key=lambda item: str(item.get("year") or ""), reverse=True)
        page_size = EXPLORE_PAGE_SIZE
        page_no = max(1, int(page))
        offset = (page_no - 1) * page_size
        fallback = {"category": category, "page": page_no,
                    "items": snapshot_items[offset:offset + page_size],
                    "has_more": len(snapshot_items) > offset + page_size,
                    "source": "douban-snapshot", "stale": True}
        result = self._coalesced(key, "show", produce, fallback)
        self._ingest(result.get("items") or [])
        return result

    def search(self, query, page=1, limit=30):
        query = str(query or "").strip()
        if not query:
            return {"query": "", "page": int(page), "limit": int(limit), "items": [],
                    "total": 0, "has_more": False, "source": "local", "local": True}
        items = self.media_index.search(query, limit=limit) if self.media_index else []
        return {"query": query, "page": int(page), "limit": int(limit), "items": items,
                "total": len(items), "has_more": False, "source": "local", "local": True}

    def detail(self, douban_id):
        douban_id = str(douban_id or "").removeprefix("douban:")
        if not douban_id.isdigit():
            raise ValueError("invalid Douban id")
        key = "detail:" + douban_id

        def produce():
            source = self.fetch("https://m.douban.com/rexxar/api/v2/subject/%s" % douban_id)
            item = normalize_subject(source) if isinstance(source, dict) else parse_subject_html(source, douban_id)
            if not item.get("title"):
                raise ValueError("empty Douban detail")
            item["source"] = "douban"
            item["stale"] = False
            return item

        fallback = self._snapshot_detail(douban_id) or {"id": "douban:" + douban_id,
                                                        "douban_id": douban_id, "title": "", "sources": []}
        result = self._coalesced(key, "detail", produce, fallback)
        self._ingest([result])
        return result
