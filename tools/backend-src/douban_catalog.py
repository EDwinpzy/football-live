"""Device-local Douban catalogue parsing, caching and snapshot fallback."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from media_index import normalize_text


HOME_SECTION_NAMES = ("最近热门", "最新上映", "豆瓣高分")
CATEGORY_VALUES = {"movie", "tv", "anime", "variety", "short"}
# 豆瓣 Explore 的搜索接口：`recent_hot` 会忽略 category/type 参数（三种排序拿到
# 同一份列表），只有这个接口真正支持排序与类型/地区/年份/评分筛选。
EXPLORE_API = "https://movie.douban.com/j/new_search_subjects"
SEARCH_API = "https://m.douban.com/rexxar/api/v2/search/subjects"
SUGGEST_API = "https://movie.douban.com/j/subject_suggest"
EXPLORE_PAGE_SIZE = 20
EXPLORE_TAGS = {"movie": "电影", "tv": "电视剧", "anime": "动漫", "variety": "综艺", "short": "短剧"}
SORT_CODES = {"hot": "U", "new": "R", "rating": "S"}  # U 热门 / R 最新 / S 高分
# 带筛选时本地补抓的页数（2026-09-18）：豆瓣 Explore 对筛选参数的支持按分类而异——
# 电影认 `genres=`，电视剧/综艺/短剧传 `genres=` 会直接返回空页。所以筛选在拿到行之后
# 再用行内 genres/regions 兜底过滤一遍，并多抓两页把一页 20 条补足。
SHOW_FILTER_EXTRA_PAGES = 2
# 豆瓣限流时会回 HTTP 200 + 空 data（tools/build_douban_snapshot.py 已按此处理）。
# 首页三栏是「不带筛选、按排序取前 20」，空列表只可能是限流；旧实现把它当成功结果
# 写进 6 小时缓存，用户看到「最近热门有内容、另外两栏空」且在过期前不再恢复。
EXPLORE_RETRY_DELAYS = (0.4, 1.2)

# ---- 分组内容规则（2026-09-17 用户要求）----------------------------------------
# 豆瓣把演唱会场次、音乐剧/话剧现场实录挂在「电影」下，把竖屏短剧挂在「电视剧」下，
# 列表接口又不给类型字段，所以这两条规则要补详情后再判：范围见 SECTION_POLICIES。
STAGE_PERFORMANCE_TOKENS = (
    "演唱会", "音乐会", "音乐剧", "音乐节", "舞台剧", "话剧", "相声", "脱口秀",
    "纪念演出", "巡演", "巡回演出", "现场演出", "剧院现场", "国家剧院", "不插电", "歌友会",
    "开幕式",
    # 英文原名的现场演出品牌（例：雷曼兄弟三部曲 original_title = "National Theatre Live: …"）
    "national theatre live", "national theatre", "nt live",
)
# 单独出现在「类型」里的演出标记：脱口秀/相声专场等的类型就是它，而电影的类型里没有这些词。
# 「音乐剧」不在此列——歌舞片（《爱乐之城》《悲惨世界》电影版）的类型也可能沾边，风险留给标题/又名。
STAGE_GENRE_TOKENS = (
    "演唱会", "音乐会", "音乐节", "舞台剧", "话剧", "相声", "脱口秀", "开幕式", "不插电", "歌友会", "巡演",
)
# 「音乐剧的电影版」仍是电影（例：《悲惨世界》2012 又名「音乐剧《悲惨世界》电影版」）
FILM_VERSION_TOKENS = ("电影版", "剧场版")
SHORT_FORM_GENRES = ("短片", "短剧", "微短剧", "竖屏剧", "迷你剧")
SHORT_EPISODE_MINUTES = 5    # 单集 ≤ 5 分钟 → 竖屏短剧（例：152 集 × 2 分钟）
# original_title 也要并进来：现场演出品牌常常只出现在原名里（例：National Theatre Live: …）
DETAIL_MERGE_FIELDS = ("genres", "regions", "year", "aliases", "tags", "durations", "episodes_count",
                       "original_title")
ENRICH_WORKERS = 8           # 补详情的并发数
ENRICH_MAX_EXTRA_PAGES = 2   # 剔除后不足一栏时，最多再翻 2 页补足
SECTION_POLICIES = {"movie": "stage", "tv": "short"}
# 三栏并行后详情会同时从三个方向打过来（3 栏 × 8 池），上游却是同一个豆瓣；
# 用一个全局闸把「在飞的详情请求」压回 ENRICH_WORKERS，避免把自己打成限流。
# 2026-09-17 实测（冷缓存 home(movie)）：闸=8 → 6.95s，闸=16 → 4.99s，
# 闸=24 触发豆瓣限流回落快照（source=douban-snapshot）。取 16 为上限。
_DETAIL_GATE = threading.Semaphore(ENRICH_WORKERS * 2)
SUBJECT_FIELDS = (
    "id", "douban_id", "title", "original_title", "aliases", "year",
    "category", "genres", "regions", "season", "rating", "rating_count",
    "summary", "directors", "actors", "poster_url", "hero_url", "backdrop_url", "release_date",
)
TTL = {"home": 21600, "filters": 86400, "detail": 604800, "search": 1800, "suggest": 60, "show": 21600}
CACHE_SCHEMA = 1
CACHE_ENVELOPE_KEYS = {"schema", "fetched_at", "value"}
SNAPSHOT_PATH = Path(__file__).with_name("douban_snapshot.json")
FILTER_MATRIX_PATH = Path(__file__).with_name("douban_filter_matrix.json")
FILTER_DIMENSIONS = ("genre", "region", "year")
# 本地判定某个维度时，条目上必须拿到的字段（评分不在其中：列表行的 `rate` 一定给得到）。
LOCAL_FIELDS = {"genre": "genres", "region": "regions", "year": "year"}
# 无实测数据时的保守缺省：只有电影类型有直接证据（线上 `genre=` 能返回正常结果，
# 2026-09-18 `show(tv, genre=古装)` 也返回过结果），其余维度一律本地判定 —— 宁可少给，
# 不给错。探测脚本（tools/probe_douban_filter_matrix.py）会用实测覆盖这份缺省。
DEFAULT_FILTER_MATRIX = {
    "movie": {"genre": True, "region": True, "year": True},
    "tv": {"genre": False, "region": False, "year": False},
    "anime": {"genre": False, "region": False, "year": False},
    "variety": {"genre": False, "region": False, "year": False},
    "short": {"genre": False, "region": False, "year": False},
}


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


def _minute_length(value):
    """时长串（`2分钟` / `45分钟` / `1小时20分钟`）→ 分钟数；解析不出返回 0。"""
    text = _text(value)
    hours = re.search(r"(\d+)\s*小时", text)
    minutes = re.search(r"(\d+)\s*分钟", text)
    total = int(hours.group(1)) * 60 if hours else 0
    return total + (int(minutes.group(1)) if minutes else 0)


def is_stage_performance(item):
    """演唱会 / 舞台实录（含音乐剧现场、国家剧院现场）。

    豆瓣把这些条目也归在「电影」类型下，列表接口只给标题，类型字段要补详情才有，
    因此这里按标题 + 原名 + 又名 + 豆瓣标签判定；`电影版/剧场版` 命中则仍算电影。
    另外补一层「类型」判定，兜住脱口秀/相声专场、大型开幕式这类标题看不出来的现场演出。
    """
    parts = [item.get("title"), item.get("original_title")]
    parts.extend(_list(item.get("aliases")))
    parts.extend(_list(item.get("tags")))
    text = " ".join(_text(part) for part in parts if part).lower()
    if any(token in text for token in FILM_VERSION_TOKENS):
        return False
    if any(token in text for token in STAGE_PERFORMANCE_TOKENS):
        return True
    genres = " ".join(_list(item.get("genres")))
    return any(token in genres for token in STAGE_GENRE_TOKENS)


def looks_like_short_drama(item):
    """竖屏短剧：豆瓣把短剧也标成 type=tv，靠这几条判据从「电视剧」里剔出去。

    1. 类型/标签含 短片/短剧/微短剧…；
    2. 单集时长 ≤ 5 分钟（例：152 集 × 2 分钟）；
    3. 补过详情后仍没有任何剧集信息（短剧常态；正常剧集都有集数）。
       只有确实拿到详情（`detail_meta`）才用第 3 条，避免把「还没补到详情」当短剧误杀。
    """
    if is_short_drama(item):
        return True
    genres = " ".join(_list(item.get("genres")))
    if any(token in genres for token in SHORT_FORM_GENRES):
        return True
    lengths = [length for length in (_minute_length(value) for value in _list(item.get("durations"))) if length]
    if lengths and min(lengths) <= SHORT_EPISODE_MINUTES:
        return True
    if item.get("detail_meta") and not lengths and not _integer(item.get("episodes_count"), 0):
        return True
    return False


def section_excluded(category, item):
    """栏目内容规则：电影栏不出现演唱会/舞台实录，电视剧栏不出现短剧。"""
    policy = SECTION_POLICIES.get(category)
    if policy == "stage":
        return is_stage_performance(item)
    if policy == "short":
        return looks_like_short_drama(item)
    return False


def _merge_detail(item, detail):
    """把详情里的分组判据并入列表条目（列表接口本身没有类型/又名/集数）。"""
    merged = dict(item)
    for key in DETAIL_MERGE_FIELDS:
        value = detail.get(key)
        if value and not merged.get(key):
            merged[key] = value
    merged["detail_meta"] = True
    return merged


def _apply_section_policy(category, value):
    """对（缓存或快照）首页结果再套一次规则：写缓存前已过滤，这里只兜兜底。"""
    if category not in SECTION_POLICIES or not isinstance(value, dict):
        return value
    result = dict(value)
    sections = []
    for section in value.get("sections") or []:
        if isinstance(section, dict):
            section = dict(section)
            section["items"] = [item for item in (section.get("items") or [])
                                if isinstance(item, dict) and not section_excluded(category, item)]
        sections.append(section)
    result["sections"] = sections
    return result


_CARD_YEAR = re.compile(r"^\d{4}$")

# 类型/地区候选词的合法形态：纯中文 2~12 字。豆瓣这两栏的值全是中文词
# （「剧情」「中国大陆」「波斯尼亚和黑塞哥维那」最长也就 10 字）；凡带拉丁字母、
# 括号、标点或过长的，都是 card_subtitle 错位混进来的剧情简介残段——2026-09-19
# 实测「年份 / 剧情简介」形态的摘要（无地区/类型段）会把整段简介按空格切进
# regions 槽，地区候选词里于是出现「热爱音乐的米格尔（安东尼·冈萨雷兹」「Anthony」
# 「Austria」（豆瓣偶尔给英文国名，countries= 参数也不认）这类脏词。
_FACET_TOKEN = re.compile(r"^[一-鿿]{2,12}$")


def _clean_facet_tokens(values):
    """只保留长得像类型/地区词的值；脏值丢弃（判成未知，比显示脏候选词强）。"""
    return [v for v in values if _FACET_TOKEN.match(str(v or ""))]


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
        names = [name for name in re.split(r"[\s、]+", part) if name]
        # 地区/类型槽可能混进剧情简介（摘要只有「年份 / 简介」两段时按位置错位）：
        # 简介残段不是合法候选词，丢弃；导演/主演槽保持原样（人名形态自由）。
        if key in ("regions", "genres"):
            names = _clean_facet_tokens(names)
        result[key] = names
    return result


def _merge_card_subtitle(raw):
    """仅在条目缺少结构化字段时用 `card_subtitle` 补齐（详情响应里的字段优先）。"""
    if not raw.get("card_subtitle"):
        return raw
    merged = dict(raw)
    for key, value in parse_card_subtitle(raw.get("card_subtitle")).items():
        if not merged.get(key):
            merged[key] = value
    return merged


# 豆瓣同一张剧照有多个尺寸前缀，列表接口只给最小号：
#   s_ratio_poster ≈ 240×360（卡片够用）… large/public = 1142×1600（同一张，实测 667KB）
# 影视页顶部 hero 是全宽铺图，240 宽拉到 1920 会糊成一片，所以额外给一条 hero_url。
_DOUBAN_PHOTO_RE = re.compile(
    r"^(https?://[^/]*doubanio\.com)/view/photo/[^/]+/(?:public/)?(p\d+\.[A-Za-z]+)$")


def hero_poster_url(url):
    """同一张豆瓣剧照换最大尺寸（`/view/photo/large/public/`）；非豆瓣图返回空串。"""
    match = _DOUBAN_PHOTO_RE.match(_text(url))
    if not match:
        return ""
    return "%s/view/photo/large/public/%s" % (match.group(1), match.group(2))


# 豆瓣在该条目没有剧照时不给空串，而是回一张「默认占位图」。两种形态实测（2026-10-01）：
#   asset.doubanio.com/cuphead/{movie,tv}-static/pics/{movie,tv}_default_large.png
#   imgN.doubanio.com/f/frodo/<hash>/pics/subject/{movie,tv}_large.jpg
# 两者都返回 HTTP 200 + 真图片，客户端无法与真海报区分，只能安静地显示成灰卡片
# （线上图片代理实测：占位图 69B~6591B，真海报 100KB+，全部 200）。
# 所以这里在**入库前**就把它识别成「没有海报」存空串，让卡片走占位样式而不是假图。
_PLACEHOLDER_POSTER_TOKENS = (
    "default_large", "default_poster", "movie_large", "tv_large",
    "/f/frodo/", "cuphead/movie-static", "cuphead/tv-static",
)


def is_placeholder_poster(url):
    """True 表示这是豆瓣的默认占位图而非真实剧照。"""
    text = _text(url).lower()
    if not text:
        return True
    return any(token in text for token in _PLACEHOLDER_POSTER_TOKENS)


def real_poster_url(url):
    """占位图→空串；真海报原样返回（`_text` 归一）。"""
    text = _text(url)
    return "" if is_placeholder_poster(text) else text


def normalize_subject(raw, requested_category="movie"):
    raw = _merge_card_subtitle(raw if isinstance(raw, dict) else {})
    subject_id = _text(raw.get("douban_id") or raw.get("id")).removeprefix("douban:")
    rating = raw.get("rating") or raw.get("aggregateRating") or {}
    poster = raw.get("poster_url") or raw.get("cover_url") or raw.get("image")
    if isinstance(raw.get("pic"), dict):
        poster = poster or raw["pic"].get("large") or raw["pic"].get("normal")
    # 占位图当空串存（见 is_placeholder_poster）：否则灰卡片会被当成真海报下发给所有端。
    poster = real_poster_url(poster)
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
        "tags": _list(raw.get("tags")),
        "durations": _list(raw.get("durations")),
        "episodes_count": _integer(raw.get("episodes_count"), 0),
        "season": _integer(raw.get("season"), 0),
        "rating": _number(rating),
        "rating_count": _integer(rating),
        "summary": _text(raw.get("summary") or raw.get("description") or raw.get("intro")),
        "directors": _list(raw.get("directors") or raw.get("director")),
        "actors": _list(raw.get("actors") or raw.get("actor")),
        "poster_url": poster,
        "hero_url": hero_poster_url(poster) or real_poster_url(raw.get("backdrop_url")) or poster,
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
        # 2026-09-18：列表行的 card_subtitle（`年份 / 地区 / 类型 / 导演 / 主演`）必须透传，
        # 否则行内没有 regions/genres，筛选只能靠上游参数——而电视剧/综艺/短剧等分类上游
        # 不认 genres=，用户就会看到"选了地区/类型等于没筛"。
        "card_subtitle": raw.get("card_subtitle"),
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
        # 2026-09-18 修「片库筛选不生效」根因：旧实现把 key 里的非 ASCII 字符整段替换成
        # 下划线，于是 `show:movie:::美国::U:1` 与 `show:movie:::::U:1` 会落成同一个文件名
        # ——带中文的地区/类型筛选直接命中「未筛选」的缓存结果（year/rating 因为带数字，
        # 文件名仍不同，所以当时看着是好的）。现在保留可读前缀 + 末尾拼 key 的短哈希，
        # 任何 key 都不再撞车。
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)[:80]
        return self.directory / ("%s-%s.json" % (safe, digest))

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


class DoubanUnavailable(RuntimeError):
    """上游异常（IP 被拦 / 非 JSON / 缺 data 键）——不是「这个筛选没有结果」。

    2026-09-18：豆瓣对异常 IP 回 HTTP 200 + `{"msg": "检测到有异常请求…"}`（没有 `data`
    键）。旧实现把它当成空列表，空壳于是被当作成功结果写进 6 小时缓存 —— 用户看到假空，
    豆瓣恢复后也不会刷新。
    """


def explore_rows(payload):
    """校验 Explore 响应形状；异常一律抛 [DoubanUnavailable]，合法空结果返回 `[]`。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        message = payload.get("msg") if isinstance(payload, dict) else ""
        raise DoubanUnavailable(str(message or "unexpected Douban explore payload"))
    return payload["data"]


def early_year_cutoff(now=None):
    """「更早」的上界：年份 chip 覆盖 [今年 .. 今年-11]，所以更早 = 今年-12 及以前。"""
    return (now or time.localtime()).tm_year - 12


def year_range_value(year):
    """年份 chip → 豆瓣 `year_range` 参数；「全部」/空返回空串。"""
    text = str(year or "").strip()
    if not text or text == "全部":
        return ""
    if text == "更早":
        return "1,%d" % early_year_cutoff()
    return "%s,%s" % (text, text) if text.isdigit() else ""


# 上游被拦后的冷却：冷却期内不再打上游（避免用户原地等超时），到期自动放行一次
# 真实回源试探，成功即恢复正常。
DOUBAN_UNAVAILABLE_COOLDOWN = 60.0
# 冷却只针对「目录检索」这一类上游（movie.douban.com）。详情走的是另一个域
# （m.douban.com/rexxar），实测在被拦时仍然可用，绝不能一起被打进冷却。
COOLDOWN_KINDS = ("home", "show")


def _section_items(value, title):
    """取（缓存或快照）分类首页里同名栏目的条目；没有则返回空列表。"""
    if not isinstance(value, dict):
        return []
    for section in value.get("sections") or []:
        if isinstance(section, dict) and section.get("title") == title:
            return [item for item in (section.get("items") or []) if isinstance(item, dict)]
    return []


class DoubanCatalog:
    def __init__(self, fetch=None, cache=None, snapshot=None, media_index=None, matrix_path=None):
        self.fetch = fetch or self._fetch
        self._search_fetch = fetch or (lambda url: self._fetch(url, timeout=4))
        self._suggest_fetch = fetch or (lambda url: self._fetch(url, timeout=2))
        self.cache = cache
        self.snapshot = snapshot if isinstance(snapshot, dict) else self._load_snapshot()
        self.media_index = media_index
        self._conditions = {}
        self._condition_lock = threading.Lock()
        self._matrix_path = Path(matrix_path) if matrix_path else FILTER_MATRIX_PATH
        self._matrix_cache = None
        self._unavailable_until = 0.0
        self._search_unavailable_until = 0.0
        self._suggest_unavailable_until = 0.0
        self._index_warmed = False

    def _filter_matrix(self):
        """分类 → 上游是否可信该维度。缺文件/缺字段用保守缺省补齐，绝不抛错。"""
        if self._matrix_cache is not None:
            return self._matrix_cache
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

    def _mark_unavailable(self):
        with self._condition_lock:
            self._unavailable_until = time.monotonic() + DOUBAN_UNAVAILABLE_COOLDOWN

    def _upstream_cooling(self):
        with self._condition_lock:
            return time.monotonic() < self._unavailable_until

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

    def _snapshot_facets(self, category):
        """随包快照里该分类真实出现过的类型/地区（用来保证筛选项「点了有内容」）。"""
        genres, regions = [], []
        for item in self.snapshot_subjects():
            if str(item.get("category") or "") != category:
                continue
            genres.extend(item.get("genres") or [])
            regions.extend(item.get("regions") or [])
        # 老快照里可能已烙进脏词（见 [_FACET_TOKEN] 注释），候选词出口再拦一道，
        # 不必等快照刷新（>24h 才重抓）才能让筛选面板恢复干净。
        return _clean_facet_tokens(genres), _clean_facet_tokens(regions)

    @staticmethod
    def _merge_options(base, extra):
        """保留 base 的顺序（「全部」在首位），附加 extra 里还没出现过的值。"""
        options = list(base)
        for value in extra:
            name = str(value or "").strip()
            if name and name != "全部" and name not in options:
                options.append(name)
        return options

    def warm_index(self):
        """补齐快照新增条目，保留设备上已抓到的更完整详情。"""
        if not self.media_index:
            return 0
        with self._condition_lock:
            if self._index_warmed:
                return 0
            known = self.media_index.subject_ids()
            items = [item for item in self.snapshot_subjects()
                     if str(item.get("douban_id") or "") not in known]
            self._ingest(items)
            self._index_warmed = True
            return len(items)

    @staticmethod
    def _fetch(url, timeout=12):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/html",
                                                       "Referer": "https://m.douban.com/"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            content_type = response.headers.get("Content-Type", "").lower()
            return json.loads(body) if "json" in content_type or body.lstrip().startswith(("{", "[")) else body

    def _refresh_async(self, key, producer):
        """后台刷新（stale-while-revalidate）：同一 key 已有刷新在跑就跳过。"""
        with self._condition_lock:
            condition = self._conditions.setdefault(key, threading.Condition())
        with condition:
            if getattr(condition, "_running", False):
                return
            condition._running = True

        def run():
            try:
                value = producer()
                if value and self.cache:
                    self.cache.put(key, value)
            except Exception as exc:
                # 后台刷新失败不打扰用户：旧值继续用，下次请求再试。但上游不可用要记一笔
                # 冷却，否则用户的每次请求都会在原地重等一轮上游超时。
                if (key.split(":", 1)[0] in COOLDOWN_KINDS
                        and isinstance(exc, (DoubanUnavailable, DoubanThrottled, OSError))):
                    self._mark_unavailable()
            finally:
                with condition:
                    condition._running = False
                    condition.notify_all()

        threading.Thread(target=run, name="douban-swr", daemon=True).start()

    @staticmethod
    def _has_content(payload):
        """回落结果里到底有没有东西给用户看（show 看 items，home 看 sections，detail 看 title）。"""
        if not isinstance(payload, dict) or not payload:
            return False
        if payload.get("items") or payload.get("title"):
            return True
        return any((section or {}).get("items") for section in (payload.get("sections") or []))

    def _degraded(self, key, snapshot_value):
        """上游不可用时的回落：同筛选条件的最近成功 → 随包快照 → 明确不可用。

        「上游不可用」与「没有符合条件的内容」必须能被区分：前者 `upstream_available=False`
        且 source 为 douban-unavailable，客户端显示「暂时获取不到，请重试」。
        """
        stale = self.cache.get_stale(key) if self.cache else None
        fallback = stale if stale is not None else copy.deepcopy(snapshot_value)
        if not isinstance(fallback, dict):
            fallback = {}
        fallback["source"] = ("douban-cache" if stale is not None else "douban-snapshot") \
            if self._has_content(fallback) else "douban-unavailable"
        fallback["stale"] = True
        fallback["upstream_available"] = False
        return fallback

    def _coalesced(self, key, kind, producer, snapshot_value=None, allow_snapshot_first=True,
                   force_refresh=False):
        if self.cache and not force_refresh:
            fresh = self.cache.get(key, TTL[kind])
            if fresh is not None:
                return fresh
            # 过期值先上屏、后台刷新：首页 6h TTL 一过（冷实例换了缓存目录更常见）旧实现
            # 要用户原地等三栏抓完（本机冷态实测 2.3s，弱网更久）。带 stale 标记返回，
            # 上层回落链与客户端仍按「旧数据」处理。
            stale = self.cache.get_stale(key)
            if isinstance(stale, dict):
                self._refresh_async(key, producer)
                fallback = copy.deepcopy(stale)
                fallback["stale"] = True
                fallback["source"] = "douban-cache"
                return fallback
            # 2026-09-18 冷缓存（首装 / 热更换了缓存文件名后第一次运行）不再同步等源站：
            # 目录类（home/show）内置快照足以撑起首屏，先秒回快照 + 后台预热。旧实现要等
            # 「5 个分类 × 3 栏 × 最多 3 页 + 每条补详情」整轮跑完，手机上极易超过客户端
            # 45s 读超时 → 5 路全失败 → 影视页整页空掉（用户报障「影视界面崩了」）。
            # detail 不走这条：快照详情可能没有线路，秒回会让详情页显示「暂无片源」。
            if (allow_snapshot_first and kind in ("home", "show")
                    and isinstance(snapshot_value, dict) and snapshot_value):
                self._refresh_async(key, producer)
                fallback = copy.deepcopy(snapshot_value)
                fallback["stale"] = True
                fallback["source"] = "douban-snapshot"
                return fallback
        # 目录检索的上游刚失败过：冷却期内不再排队等网络，直接给回落结果。
        if kind in COOLDOWN_KINDS and self._upstream_cooling():
            return self._degraded(key, snapshot_value)
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
            if isinstance(value, dict):
                value.setdefault("upstream_available", True)
            if self.cache:
                self.cache.put(key, value)
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError, DoubanUnavailable):
            if kind in COOLDOWN_KINDS:
                self._mark_unavailable()
            return self._degraded(key, snapshot_value)
        finally:
            with condition:
                condition._running = False
                condition.notify_all()

    def _explore(self, category, sort, page=1, genre="", region="", year="", rating="",
                 strict=False, send=FILTER_DIMENSIONS):
        """豆瓣 Explore 列表：排序与类型/地区/年份/评分筛选都在服务端完成。

        `strict=True`（首页三栏）：空 data 视为限流，退避重试后仍为空则抛
        [DoubanThrottled]，由上层回落最近一次缓存或随包快照——绝不把空栏当成功
        结果缓存。带筛选的「全部」页保持宽松：空结果是合法结果。

        `send` 决定哪些筛选维度交给上游：矩阵判定「上游不可信」的维度必须排除掉 ——
        某些分类收到 `genres=` 会直接回空页，那会让用户看到一片空白。
        """
        # `tags=` 只放大类。地区/年份/类型各有专用参数：旧实现把它们拼进 tags=，豆瓣
        # 服务端其实没按这些条件筛（`tags=电影,日本` 只按「电影」筛），于是「选了地区
        # 像没选」；「更早」更是等于不筛选。年份区间还能让「更早」真正生效。
        query = {"sort": sort, "range": rating_range(rating),
                 "tags": ",".join([EXPLORE_TAGS[category]]),
                 "start": max(0, int(page) - 1) * EXPLORE_PAGE_SIZE}
        if genre and genre != "全部" and "genre" in send:
            query["genres"] = genre
        if region and region != "全部" and "region" in send:
            query["countries"] = region
        span = year_range_value(year) if "year" in send else ""
        if span:
            query["year_range"] = span
        url = EXPLORE_API + "?" + urllib.parse.urlencode(query)
        for attempt in range(len(EXPLORE_RETRY_DELAYS) + 1):
            rows = explore_rows(self.fetch(url))
            if rows:
                return [normalize_explore_row(row, category) for row in rows if isinstance(row, dict)]
            if not strict:
                return []
            if attempt < len(EXPLORE_RETRY_DELAYS):
                time.sleep(EXPLORE_RETRY_DELAYS[attempt])
        raise DoubanThrottled("empty Douban explore data for %s" % category)

    def _enrich(self, items, needs=()):
        """并发补详情，把分组判据并进列表条目；详情走 7 天磁盘缓存，失败就跳过。

        `needs` 是调用方真正缺的字段（见 [LOCAL_FIELDS]）。旧实现「带筛选就把整页逐条
        补详情」，一次浏览最多约 60 次上游请求；现在只有缺判据的行才补 —— 列表行自带的
        `card_subtitle` 解析结果（年份/地区/类型）已经覆盖大多数行。
        """
        fields = tuple(needs)
        pending = [item for item in items
                   if item.get("douban_id") and not item.get("detail_meta")
                   and (not fields or any(not item.get(field) for field in fields))]
        if not pending:
            return [dict(item) for item in items]
        with ThreadPoolExecutor(max_workers=min(ENRICH_WORKERS, len(pending))) as pool:
            details = list(pool.map(self._detail_meta, [item["douban_id"] for item in pending]))
        by_id = {item["douban_id"]: detail for item, detail in zip(pending, details) if detail}
        return [_merge_detail(item, by_id[item["douban_id"]]) if by_id.get(item.get("douban_id"))
                else dict(item) for item in items]

    def _detail_meta(self, douban_id):
        """单条详情；拿不到有效详情返回空字典（叫它别把「未知」当判据）。"""
        with _DETAIL_GATE:
            try:
                detail = self.detail(douban_id)
            except Exception:
                return {}
        return detail if isinstance(detail, dict) and detail.get("title") else {}

    def _section_or_empty(self, category, sort):
        """一栏的抓取结果；限流返回空列表，由上层用同栏旧值/随包快照补位。"""
        try:
            return self._section_items(category, sort)
        except (DoubanThrottled, DoubanUnavailable):
            return []

    def _section_items(self, category, sort):
        """首页一栏的条目：拉列表 → 补详情 → 按分类规则剔除 → 不够就翻页补足。

        不带规则的分类维持原样（一次请求、原样返回）；带规则的分类在剔除后
        继续翻页，避免电影高分栏被演唱会占掉大半后只剩几部片子。
        """
        if category not in SECTION_POLICIES:
            return self._explore(category, sort, strict=True)
        accepted, seen = [], set()
        for page in range(1, 2 + ENRICH_MAX_EXTRA_PAGES):
            # 第一页沿用严格模式（空 data = 限流，要回落）；翻页时空 data 是正常的列表末尾。
            if page == 1:
                rows = self._explore(category, sort, strict=True)
            else:
                rows = self._explore(category, sort, page=page)
            if not rows:
                break
            for item in self._enrich(rows):
                key = str(item.get("douban_id") or item.get("id") or "")
                if key and key in seen:
                    continue
                seen.add(key)
                if not section_excluded(category, item):
                    accepted.append(item)
            if len(accepted) >= EXPLORE_PAGE_SIZE:
                break
        return accepted[:EXPLORE_PAGE_SIZE]

    def home(self, category, force_refresh=False):
        if category not in CATEGORY_VALUES:
            raise ValueError("unsupported category")
        key = "home:" + category
        snapshot_value = ((self.snapshot.get("home") or {}).get(category))

        def produce():
            """三栏分别抓取：某栏被限流就用该分类上一版缓存/随包快照的同一栏补位。

            三栏全被限流才整体抛错，交给 [_coalesced] 回落——这样用户看到的
            三栏不会出现「一栏新鲜、两栏空」的错位，也不会把空栏写进 6 小时缓存。
            三栏并行抓（每栏 ≈0.6s 上游）：串行曾让单次 /vod/home 至少 1.8s，
            本机实测并行后 1.80s → 0.63s。
            """
            previous = self.cache.get_stale(key) if self.cache else None
            with ThreadPoolExecutor(max_workers=len(HOME_SECTION_NAMES)) as pool:
                fetched = list(pool.map(
                    lambda pair: self._section_or_empty(category, pair[1]),
                    list(zip(HOME_SECTION_NAMES, ("U", "R", "S")))))
            sections, degraded = [], 0
            for title, items in zip(HOME_SECTION_NAMES, fetched):
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

        result = self._coalesced(
            key, "home", produce, snapshot_value, force_refresh=force_refresh)
        # 兜底路径（快照缺失/为空）可能只有 stale/source 两个键；补齐标准三栏，
        # 客户端不必再处理「没有 sections」这种形态。
        if not result.get("sections"):
            result["sections"] = [{"title": title, "items": []} for title in HOME_SECTION_NAMES]
        # 快照/缓存兜底路径没有经过 [_section_items]，这里再套一次规则。
        result = _apply_section_policy(category, result)
        self._ingest([item for section in (result.get("sections") or []) for item in (section.get("items") or [])])
        return result

    def refresh_home(self, category):
        """Revalidate a visible catalogue even while its normal six-hour cache is fresh."""
        return self.home(category, force_refresh=True)

    def filters(self, category):
        if category not in CATEGORY_VALUES:
            raise ValueError("unsupported category")
        current_year = time.localtime().tm_year
        # 2026-09-18 用户报障「类型条件太少了，豆瓣上肯定不止这些」：类型表按随包快照里
        # 真实出现过的 genres 汇总（外加豆瓣 Explore 实测可用的几个电影类型），保证每个
        # 选项筛出来都有内容，不再只给 6~10 个。
        genres = {
            "movie": ["全部", "剧情", "喜剧", "动作", "爱情", "科幻", "动画", "悬疑", "惊悚", "恐怖",
                      "犯罪", "纪录片", "战争", "奇幻", "冒险", "灾难", "历史", "传记", "音乐",
                      "歌舞", "家庭", "运动", "同性", "武侠", "古装"],
            "tv": ["全部", "剧情", "爱情", "喜剧", "犯罪", "古装", "短片", "悬疑", "惊悚", "动作",
                   "奇幻", "冒险", "传记", "同性", "历史"],
            "anime": ["全部", "动画", "短片", "喜剧", "奇幻", "动作", "科幻", "冒险", "剧情", "音乐",
                      "爱情", "惊悚", "悬疑", "家庭", "古装"],
            "variety": ["全部", "真人秀", "音乐", "歌舞", "脱口秀", "喜剧"],
            "short": ["全部", "剧情", "喜剧", "爱情", "悬疑", "古装", "奇幻", "动作", "家庭"],
        }[category]
        regions = ["全部", "中国大陆", "中国香港", "中国台湾", "美国", "英国", "加拿大", "法国",
                   "德国", "意大利", "西班牙", "日本", "韩国", "印度", "泰国", "澳大利亚",
                   "新西兰", "俄罗斯"]
        years = ["全部"] + [str(year) for year in range(current_year, current_year - 12, -1)] + ["更早"]
        ratings = ["全部", "9分以上", "8分以上", "7分以上", "暂无评分"]
        # 三层合并：内置词汇表（上一段）∪ 随包快照里真实出现过的值。后者保证「点了有内容」。
        # 豆瓣官方 `search_tags` 给的是浏览标签（热门/最新/华语/欧美…），不是探索页的
        # 类型/地区词汇表，表达不了「类型」，所以不作为一层。
        seen_genres, seen_regions = self._snapshot_facets(category)
        genres = self._merge_options(genres, seen_genres)
        regions = self._merge_options(regions, seen_regions)
        return {"category": category, "types": genres, "areas": regions, "years": years,
                "ratings": ratings, "upstream_available": True, "rows": [
            {"name": "类别", "key": "category", "options": ["电影", "电视剧", "动漫", "综艺", "短剧"]},
            {"name": "类型", "key": "genre", "options": genres},
            {"name": "地区", "key": "region", "options": regions},
            {"name": "年份", "key": "year", "options": years},
            {"name": "评分", "key": "rating", "options": ratings},
            {"name": "排序", "key": "sort", "options": ["热门", "最新上映", "豆瓣高分"]},
        ], "source": "douban"}

    @staticmethod
    def _matches(item, genre="", region="", year="", rating=""):
        """本地三态判定：只有明确匹配才通过，判据未知一律剔除。

        这个方法只在「上游不可信该维度」时被调用（见 [_filter_matrix]）：上游已经筛过的
        维度不会走到这里，所以不存在「该有却被本地砍掉」的情况。

        旧实现是「条目缺某个字段就放行」—— 综艺/短剧的条目录从未补过类型/地区，于是
        筛选等于没筛（实测返回 20 条、真实匹配 0 条）。准确度优先：宁可少给，不给错。
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

    def show(self, category, genre="", region="", year="", rating="", sort="hot", page=1):
        if category not in CATEGORY_VALUES:
            raise ValueError("unsupported category")
        sort_code = SORT_CODES.get(sort, "U")
        key = "show:%s:%s:%s:%s:%s:%s:%s" % (category, genre, region, year, rating, sort_code, page)
        filtered = any(v and v != "全部" for v in (genre, region, year, rating))
        # 矩阵决定每个维度由谁负责：可信的交给上游（本地不再判，避免误杀），不可信的
        # 一律本地判，且**不发该参数给上游** —— 某些分类收到 `genres=` 会直接回空页。
        # 评分永远本地判：列表行的 `rate` 一定拿得到，不涉及「未知剔除」。
        matrix = self._filter_matrix().get(category) or {}
        active = {name: value for name, value in
                  (("genre", genre), ("region", region), ("year", year), ("rating", rating))
                  if value and value != "全部"}
        local = tuple(name for name in active if name == "rating" or not matrix.get(name))
        send = tuple(name for name in active if name not in local)
        needs = tuple(LOCAL_FIELDS[name] for name in local if name in LOCAL_FIELDS)

        def produce():
            # 上游回来的一页可能被本地筛掉一些，多抓两页把一页补足。
            pages = [int(page)]
            if filtered:
                pages += [int(page) + i for i in range(1, SHOW_FILTER_EXTRA_PAGES + 1)]
            collected, seen, rows, last_full = [], set(), [], False
            for p in pages:
                rows = self._explore(category, sort_code, p, genre, region, year, rating, send=send)
                last_full = len(rows) >= EXPLORE_PAGE_SIZE
                if not rows:
                    break
                fresh = []
                for item in rows:
                    key_id = str(item.get("douban_id") or item.get("id") or "")
                    if key_id and key_id in seen:
                        continue
                    if key_id:
                        seen.add(key_id)
                    fresh.append(item)
                candidates = self._enrich(fresh, needs) if needs else fresh
                for item in candidates:
                    if self._matches(item,
                                     genre if "genre" in local else "",
                                     region if "region" in local else "",
                                     year if "year" in local else "",
                                     rating if "rating" in local else ""):
                        collected.append(item)
                if not filtered or len(collected) >= EXPLORE_PAGE_SIZE:
                    break
            items = collected[:EXPLORE_PAGE_SIZE]
            # 诚实：只有「最后一次上游请求拿到满页」才说明还有下一页。旧实现带筛选时用
            # `bool(rows)`（拿到任意一行就算有更多），于是「加载更多」点下去常常是空的。
            has_more = last_full
            return {"category": category, "page": int(page), "items": items,
                    "has_more": has_more, "source": "douban", "stale": False}

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
        # 带筛选是用户显式查询：冷缓存不能先回快照就结束 —— 快照里能匹配的条目极少，
        # 用户会再次看到「筛选没内容」。无筛选仍保留「秒回快照 + 后台刷新」。
        result = self._coalesced(key, "show", produce, fallback, allow_snapshot_first=not filtered)
        self._ingest(result.get("items") or [])
        return result

    def _search_suggestions(self, query):
        key = "search-suggest:" + query.casefold()

        def produce():
            try:
                rows = self._suggest_fetch(SUGGEST_API + "?" + urllib.parse.urlencode({"q": query}))
                if not isinstance(rows, list):
                    raise DoubanUnavailable("unexpected Douban suggestions payload")
                rows = [row for row in rows if isinstance(row, dict)
                        and str(row.get("id") or "").isdigit() and row.get("title")][:8]

                def enrich(row):
                    subject_id = str(row["id"])
                    cached = self.cache.get_stale("detail:" + subject_id) if self.cache else None
                    known = cached or self._snapshot_detail(subject_id)
                    if not known and self.media_index:
                        known = self.media_index.get(subject_id)
                    if known and (known.get("genres") or known.get("episodes_count")):
                        return known
                    # Suggestions label TV as movie too; only canonical metadata can classify it.
                    try:
                        raw = self._suggest_fetch("https://m.douban.com/rexxar/api/v2/subject/" + subject_id)
                        if isinstance(raw, dict):
                            kind = "tv" if raw.get("type") == "tv" or raw.get("episodes_count") else "movie"
                            item = normalize_subject(raw, kind)
                        else:
                            item = parse_subject_html(raw, subject_id)
                        return item if item.get("title") and item.get("douban_id") == subject_id else None
                    except (OSError, ValueError, TypeError, DoubanUnavailable):
                        return None

                with ThreadPoolExecutor(max_workers=8) as pool:
                    items = [item for item in pool.map(enrich, rows) if item]
                if rows and not items:
                    raise DoubanUnavailable("suggestion metadata unavailable")
                return {"items": items, "source": "douban-suggest"}
            except (OSError, ValueError, TypeError, DoubanUnavailable):
                with self._condition_lock:
                    self._suggest_unavailable_until = time.monotonic() + DOUBAN_UNAVAILABLE_COOLDOWN
                raise

        cached = self.cache.get(key, TTL["suggest"]) if self.cache else None
        with self._condition_lock:
            cooling = time.monotonic() < self._suggest_unavailable_until
        if cached is None and cooling:
            return self._degraded(key, {"items": []})
        return self._coalesced(key, "suggest", produce, {"items": []})

    def search(self, query, page=1, limit=30):
        query = str(query or "").strip()
        page = max(1, int(page))
        limit = max(1, min(100, int(limit)))
        if not query:
            return {"query": "", "page": page, "limit": limit, "items": [],
                    "total": 0, "has_more": False, "source": "local", "local": True}
        key = "search-discovery:" + query.casefold()

        def discover():
            try:
                payload = self._search_fetch(SEARCH_API + "?" + urllib.parse.urlencode(
                    {"q": query, "type": "movie", "count": 100}))
                subjects = payload.get("subjects") if isinstance(payload, dict) else None
                rows = subjects.get("items") if isinstance(subjects, dict) else None
                if not isinstance(rows, list) or payload.get("banned"):
                    raise DoubanUnavailable("unexpected Douban search payload")
                found = []
                for row in rows:
                    if not isinstance(row, dict) or row.get("target_type") not in ("movie", "tv"):
                        continue
                    target = row.get("target")
                    if not isinstance(target, dict) or not str(target.get("id") or "").isdigit():
                        continue
                    item = normalize_subject(target, row["target_type"])
                    if item["title"]:
                        found.append(item)
            except (OSError, ValueError, TypeError, DoubanUnavailable):
                with self._condition_lock:
                    self._search_unavailable_until = time.monotonic() + DOUBAN_UNAVAILABLE_COOLDOWN
                raise
            if not found:
                if self.cache:
                    self.cache.put(key + ":empty", {"items": []})
                    return None
                return {"items": [], "source": "douban-search", "empty": True}
            return {"items": found, "source": "douban-search"}

        cached = self.cache.get(key, TTL["search"]) if self.cache else None
        negative = self.cache.get(key + ":empty", 60) if self.cache else None
        with self._condition_lock:
            cooling = time.monotonic() < self._search_unavailable_until
        discovered = {"items": [], "source": "local", "upstream_available": not cooling}
        if negative is None and (cached is not None or not cooling):
            discovered = self._coalesced(key, "search", discover, {"items": []})
        negative = self.cache.get(key + ":empty", 60) if self.cache else negative
        if negative is not None and not discovered.get("items"):
            discovered = {"items": [], "source": "douban-search", "upstream_available": True}
        if negative is not None or discovered.get("empty") or not discovered.get("upstream_available", True):
            backup = self._search_suggestions(query)
            if backup.get("items") or (negative is None and not discovered.get("items") and not discovered.get("empty")):
                discovered = backup
        found = discovered.get("items") or []
        if found and self.media_index:
            enriched = []
            changed = []
            for item in found:
                stored = self.media_index.get(item["douban_id"])
                if stored:
                    merged = dict(stored)
                    for field, value in item.items():
                        if not merged.get(field) and value:
                            merged[field] = value
                    if item.get("genres"):
                        merged["category"] = item["category"]
                    if merged != stored:
                        changed.append(merged)
                    item = merged
                else:
                    changed.append(item)
                enriched.append(item)
            self._ingest(changed)
            found = enriched
        items = self.media_index.search(query, limit=100) if self.media_index else []
        combined = {item["douban_id"]: item for item in items}
        normalized = normalize_text(query)
        # 上游能按译名、别名、演员等命中；不能用本地标题包含规则再次丢弃这些结果。
        remote_order = {item["douban_id"]: position for position, item in enumerate(found)}
        for item in found:
            title = normalize_text(item["title"])
            score = 120 if title == normalized else 108 if title.startswith(normalized) else 82 if normalized in title else 70
            existing = combined.get(item["douban_id"])
            if existing is None:
                combined[item["douban_id"]] = dict(item, search_score=score)
            else:
                existing["search_score"] = max(existing.get("search_score", 0), score)
        items = sorted(combined.values(), key=lambda item: (
            -(item.get("search_score", 0) >= 115),
            remote_order.get(item["douban_id"], len(found)),
            -item.get("search_score", 0), -float(item.get("rating") or 0),
            -int(item.get("rating_count") or 0), item["douban_id"]))
        offset = (page - 1) * limit
        return {"query": query, "page": page, "limit": limit, "items": items[offset:offset + limit],
                "total": len(items), "has_more": offset + limit < len(items),
                "source": discovered.get("source", "local"), "local": True,
                "upstream_available": discovered.get("upstream_available", True)}

    def detail(self, douban_id, force_refresh=False):
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
        result = self._coalesced(
            key, "detail", produce, fallback, force_refresh=force_refresh)
        self._ingest([result])
        return result

    def refresh_detail(self, douban_id):
        """Revalidate visible metadata even while its normal seven-day cache is fresh."""
        return self.detail(douban_id, force_refresh=True)
