#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
球迹直播 数据代理服务器
- 足球比赛列表、线路发现与直播播放中继
- 足球队徽、网页静态资源与 APK 更新清单
- GET /api/matches : 抓取 88zhibo.tv → 解析为 JSON（缓存 60s）
- GET /api/health  : 健康检查
用法: python proxy.py [port]   (默认 8090)
"""
import os
import re
import json
import html as _html
from html.parser import HTMLParser
import hashlib
import team_backdrop  # 球队英文名映射
import vlc_hls
import time
import time as _time  # 模块级别名：resolve_team_icon 等用 _time，与 _tdb_search_team 内局部导入一致
import threading
import contextvars
import concurrent.futures
import base64
import http.client
from urllib.parse import urljoin, quote, unquote, urlparse, urlsplit, parse_qs
import urllib.request as urlreq
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timedelta, timezone
try:
    import urllib3
    # retries=0: urllib3 的重试会让超时请求挂起数倍时间（retries=2 实测 47s），
    # 中继层已自行用两种 Referer 策略兜底，不需要 urllib3 再隐式重试。
    # maxsize=16: hls.js 并行拉分片（1-4 路）+ 清单/密钥并发下保住连接复用
    #（每 host 一个池；池满会新建临时连接不复用 → 丢 keep-alive 红利）
    _http = urllib3.PoolManager(maxsize=16, retries=0,
                                timeout=urllib3.Timeout(connect=5, read=15))
    HAS_URLLIB3 = True
except ImportError:
    HAS_URLLIB3 = False

SOURCE_URL = "https://www.88zhibo.tv/"
_VLC_HLS_SESSION = contextvars.ContextVar("otv_vlc_hls_session", default=None)
WWW_ROOT = Path(__file__).parent / "www"


def _relay_cache_key(url):
    return url


def _cached_byte_range(value, size):
    """Parse one RFC-style byte range for a cached complete representation.

    Return (status, start, end), None for malformed/multiple ranges that must
    be forwarded upstream, or (416, None, None) for a valid unsatisfiable range.
    """
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (value or "").strip(), re.I)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    try:
        if not match.group(1):
            suffix_length = int(match.group(2))
            if suffix_length == 0:
                return None
            if size == 0:
                return 416, None, None
            start = max(0, size - suffix_length)
            return 206, start, size - 1

        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    except ValueError:
        return None
    if match.group(2) and end < start:
        return None
    if start >= size:
        return 416, None, None
    return 206, start, min(end, size - 1)

# ---- 网页静态资源缓存合同（内容寻址资源 immutable）----
# tools/build_web_assets.py 生成的 manifest.json 里的 cacheControl 才是
# 缓存策略的唯一真实来源：它知道哪个文件名是内容哈希、哪个是 HTML。
# 之前这里靠 "v=" in path 猜测，可内容寻址产物是 assets/app.<sha16>.js
# （文件名寻址、没有 ?v=），于是哈希资源被误判成 max-age=300 短缓存，
# 而旧式 ?v= 资源反而拿 immutable——缓存强度刚好接反。
# 现在按 manifest 查表，miss 再退回扩展名规则；新增文件类型不用改代码。
_WEB_CACHE_LOCK = threading.Lock()
_web_cache_manifest: dict = {"stamp": None, "cache_control": None}
_WEB_BUILD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _web_asset_health() -> dict:
    """报告当前内容寻址构建与归档保留的构建，供健康检查/监控观察降级。

    任何读取失败都收敛成 ok=False 而不是抛错：健康端点必须始终能回答，
    真正的归档完整性问题由 web_entrypoint 在启动时 fail fast 挡住。
    """
    result = {"ok": False, "build": None, "retained": []}
    try:
        manifest_bytes = (_web_static_root() / "manifest.json").read_bytes()
        result["build"] = hashlib.sha256(manifest_bytes).hexdigest()[:32]
    except (OSError, ValueError):
        return result
    archive_root = os.environ.get("OTV_WEB_ASSET_ARCHIVE", "/var/lib/mytv/web-assets")
    try:
        builds = Path(archive_root) / "builds"
        retained = sorted(
            path.name for path in builds.iterdir()
            if path.is_dir() and not path.is_symlink() and _WEB_BUILD_ID_RE.fullmatch(path.name)
        )
    except OSError:
        retained = []
    result["retained"] = retained
    result["ok"] = bool(retained) and result["build"] in retained
    return result


def _web_cache_control_table():
    """读一次 www/manifest.json 的 cacheControl 映射，带 mtime 检查后进程内缓存。

    每个请求读磁盘会拖慢静态分发，所以只比较 mtime/size；manifest 在
    容器生命周期内不变，mtime 检查只是兜住开发期重建。
    """
    manifest_path = _web_static_root() / "manifest.json"
    try:
        stat = manifest_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    cached = _web_cache_manifest
    if stamp is None or cached.get("stamp") == stamp:
        return cached.get("cache_control")
    with _WEB_CACHE_LOCK:
        if cached.get("stamp") == stamp:
            return cached.get("cache_control")
        table = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw = manifest.get("cacheControl")
            if isinstance(raw, dict) and raw:
                table = {
                    str(key).lower(): str(value)
                    for key, value in raw.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            table = None
        cached["stamp"] = stamp
        cached["cache_control"] = table
        return table


def _web_static_root() -> Path:
    """当前实际服务的静态根。

    容器内与本机默认都是 proxy.py 同级的 www/；解析器允许通过
    OTV_WEB_ROOT 指向别的目录（测试与开发期用生成的 dist），
    缓存表和健康字段都必须跟着实际服务的根走，不能写死。
    """
    override = os.environ.get("OTV_WEB_ROOT", "").strip()
    if override:
        return Path(override)
    return WWW_ROOT
# 球迹直播列表源。88zhibo 首页同时混排篮球，parse_matches 会过滤非足球赛事。
SOURCE_MIRRORS = [SOURCE_URL]
_active_source = {"i": 0, "url": SOURCE_MIRRORS[0]}
# APK 应用内更新清单由后台写入 CloudBase 公开桶；网页函数只负责以稳定的
# /web/apk-update/{tv,mobile}.json 地址代理它，避免每次发 APK 都重新部署网页函数。
APK_UPDATE_OBJECT_BASE = os.environ.get(
    "OTV_APK_UPDATE_OBJECT_BASE",
    "https://appletv-d5ge1bth794873f76.api.tcloudbasegateway.com/v1/storages/object/hotupdate",
).rstrip("/")
CACHE_TTL = 15  # 过期列表由 fresh=1 请求等待单飞刷新；避免把旧状态继续当成成功结果。

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_cache = {"ts": 0, "data": None, "raw_error": None, "refreshing": False}
_cache_refresh_event = threading.Event()
_cache_refresh_event.set()

_cache_lock = threading.Lock()   # build_api 缓存读写锁：防多线程并发抓源站（弱网下重复慢请求）


_MATCH_CLOCK_HOLD_STATES = {
    "postponed", "delayed", "pending", "cancelled", "canceled", "abandoned",
    "延期", "推迟", "待定", "取消", "腰斩",
}


def _match_status_holds_clock(status):
    return str(status or "").strip().lower() in _MATCH_CLOCK_HOLD_STATES


def project_match_state(item, now=None):
    """Project cached schedule state against the current Beijing time.

    This makes kickoff transitions exact without waiting for the next origin scrape.
    Finished matches are terminal and are never reopened by clock projection.
    """
    out = dict(item)
    status = str(out.get("status", "")).strip().lower()
    if (status in ("finished", "ended", "close") or _match_status_holds_clock(status)
            or out.get("_status_authoritative")):
        return out
    now = now or _now_cn()
    try:
        full_date = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(out.get("date_full", "")).strip())
        if full_date:
            year, month, day = [int(x) for x in full_date.groups()]
        else:
            month, day = [int(x) for x in str(out.get("date", "")).split("-")]
            # Legacy snapshots have no year. Schedules only span a few days, so
            # use a nearby year only around New Year; an old snapshot must never
            # turn into a fixture in the next year merely because it is >12h old.
            candidates = [datetime(y, month, day, 0, 0) for y in (now.year - 1, now.year, now.year + 1)]
            nearby = [candidate for candidate in candidates
                      if abs((candidate.date() - now.date()).days) <= 31]
            year = min(nearby, key=lambda candidate: abs((candidate.date() - now.date()).days)).year if nearby else now.year
        hour, minute = [int(x) for x in str(out.get("time", "")).split(":")]
        start = datetime(year, month, day, hour, minute)
    except (TypeError, ValueError):
        return out
    elapsed = int((now - start).total_seconds() // 60)
    if elapsed < 0:
        # 88zhibo often labels not-yet-started games "直播中". Kickoff time is
        # the reliable signal before the game starts.
        if status == "live":
            out["status"] = "upcoming"
            out["minute"] = ""
        return out
    if elapsed >= _match_finish_grace_minutes(out):
        out["status"] = "finished"
        out["minute"] = ""
        return out
    out["status"] = "live"
    played = elapsed if elapsed <= 45 else 45 if elapsed <= 60 else elapsed - 15
    out["minute"] = str(max(0, min(90, played)))
    return out


_KNOCKOUT_MATCH_RE = re.compile(
    r"杯|淘汰|决赛|半决赛|世界杯|欧洲杯|亚洲杯|美洲杯|欧冠|欧联|欧协|世俱杯|友谊赛",
    re.I,
)


def _match_finish_grace_minutes(item):
    """Conservative fallback only when the source has not supplied a final state.

    League matches get room for stoppage time and half-time. Cup/tournament games
    also get room for extra time and penalties. 7m final/live states remain
    authoritative and bypass this clock projection.
    """
    return 210 if _KNOCKOUT_MATCH_RE.search(str(item.get("league", ""))) else 150


def _project_matches_payload(payload, now=None, strip_internal=True):
    """Reproject status on every API read, even while schedule data is cached."""
    out = dict(payload)
    now = now or _now_cn()
    matches = []
    for item in payload.get("matches", []):
        projected = project_match_state(item, now)
        if strip_internal:
            projected.pop("_status_authoritative", None)
        matches.append(projected)
    out["matches"] = matches
    out["count"] = len(matches)
    out["live_count"] = sum(1 for item in matches if item.get("status") == "live")
    return out

_STATIC_CONTENT_HASH_RE = re.compile(r"(?:^|[/._-])[0-9a-f]{8,64}(?=$|[/._-])", re.I)































def fetch_source():
    """抓取 m.html 原页面（镜像回退：主源失败自动切换 5cj.tv 等）"""
    last = None
    for k in range(len(SOURCE_MIRRORS)):
        i = (_active_source["i"] + k) % len(SOURCE_MIRRORS)
        url = SOURCE_MIRRORS[i]
        host = url.rsplit("/", 1)[0] + "/"
        try:
            req = urlreq.Request(url, headers={
                "User-Agent": UA,
                "Referer": host,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            })
            with _direct_opener.open(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            _active_source["i"] = i
            _active_source["url"] = url
            return body
        except Exception as e:
            last = e
            continue
    raise last if last else RuntimeError("全部直播源不可用")


def _now_cn():
    """北京时间（naive datetime）。v1.23（2026-09-06 状态/时间准确性）：云端函数
    跑在 UTC——datetime.now() 比北京慢 8 小时，今日/分钟推算整体错位（北京 02:00
    的比赛被当「明日」永远 upcoming，status 只能靠 7m 绝对时间差兜底、minute 全空，
    网页版「比赛状态/时间不准确」的云端根因）。设备内嵌后端本地时区即北京时间，
    行为不变。"""
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


_88Z_STREAM_SRCS = frozenset(("a2", "migu", "q2", "q3", "c4"))
_88Z_VISIBLE_STREAM_SRCS = ("a2", "migu", "q3", "c4")
_88Z_DYNAMIC_SOURCE_RE = re.compile(r"^88z_(\d{1,4})$")
FOOTBALL_STREAMS_WAIT_BUDGET = 3.5
FOOTBALL_STREAMS_MAX_WAIT = 12.0
FOOTBALL_STREAMS_COALESCE_WINDOW = 0.15
FOOTBALL_STREAMS_MAX_PENDING = 16
_football_stream_slots = threading.BoundedSemaphore(FOOTBALL_STREAMS_MAX_PENDING)
_88Z_SOURCE_LABELS = {
    "a2": "线路A2",
    "migu": "线路migu",
    "q2": "线路Q2",
    "q3": "线路Q3",
    "c4": "线路C4",
}
_88Z_NON_FOOTBALL_RE = re.compile(
    r"篮球|男篮|女篮|NBA|CBA|WNBA|NBL|菲NCAA|排球|羽毛球|乒乓球|网球|"
    r"棒球|冰球|橄榄球|手球|拳击|搏击|综合格斗|斯诺克|台球|高尔夫|赛车|F1|电竞",
    re.I,
)


def _88z_text(fragment):
    """把 88zhibo 页面片段转成紧凑纯文本。"""
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", fragment or "")
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _88z_class_texts(fragment, class_name):
    """遍历 HTML 节点按 class token 取文本（支持目标节点嵌套在赛事 li 内）。"""
    class Collector(HTMLParser):
        VOID_TAGS = {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.values = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() in self.VOID_TAGS:
                return
            attr_map = dict(attrs)
            classes = set((attr_map.get("class") or "").split())
            self.stack.append({
                "tag": tag.lower(),
                "target": class_name in classes,
                "parts": [],
            })

        def handle_startendtag(self, tag, attrs):
            return

        def handle_data(self, data):
            for node in self.stack:
                node["parts"].append(data)

        def handle_endtag(self, tag):
            tag = tag.lower()
            index = next((i for i in range(len(self.stack) - 1, -1, -1)
                          if self.stack[i]["tag"] == tag), None)
            if index is None:
                return
            closed = self.stack[index:]
            del self.stack[index:]
            for node in reversed(closed):
                if node["target"]:
                    value = re.sub(r"\s+", " ", "".join(node["parts"])).strip()
                    if value:
                        self.values.append(value)

    parser = Collector()
    parser.feed(fragment or "")
    parser.close()
    return parser.values


def _88z_list_rows(page):
    """按 HTML 节点深度提取赛事与日期 li，避免嵌套 li 让正则提早截断。"""
    target_classes = {"live-list-group-time", "group-game-item"}

    class RowCollector(HTMLParser):
        VOID_TAGS = {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows = []
            self.parts = None
            self.classes = None
            self.li_depth = 0

        def handle_starttag(self, tag, attrs):
            raw = self.get_starttag_text() or "<%s>" % tag
            if self.parts is None and tag.lower() == "li":
                classes = set((dict(attrs).get("class") or "").split())
                if classes & target_classes:
                    self.parts = [raw]
                    self.classes = classes
                    self.li_depth = 1
                    return
            elif self.parts is not None:
                self.parts.append(raw)
                if tag.lower() == "li":
                    self.li_depth += 1

        def handle_startendtag(self, tag, attrs):
            if self.parts is not None:
                self.parts.append(self.get_starttag_text() or "<%s/>" % tag)

        def handle_data(self, data):
            if self.parts is not None:
                self.parts.append(_html.escape(data, quote=False))

        def handle_endtag(self, tag):
            if self.parts is None:
                return
            self.parts.append("</%s>" % tag)
            if tag.lower() == "li":
                self.li_depth -= 1
                if self.li_depth <= 0:
                    self.rows.append((self.classes or set(), "".join(self.parts)))
                    self.parts = None
                    self.classes = None
                    self.li_depth = 0

    parser = RowCollector()
    parser.feed(page or "")
    parser.close()
    if parser.parts is not None:
        parser.rows.append((parser.classes or set(), "".join(parser.parts)))
    return parser.rows


def _parse_88zhibo_matches(page):
    """解析 88zhibo 首页比赛列表；源站同页包含 NBA/CBA，剔除篮球等非足球项目。"""
    matches = []
    current_date = None
    current_date_full = None
    for classes, row in _88z_list_rows(page):
        if "live-list-group-time" in classes:
            date_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", _88z_text(row))
            current_date_full = "-".join(date_match.groups()) if date_match else None
            current_date = "%s-%s" % (date_match.group(2), date_match.group(3)) if date_match else None
            continue
        if "group-game-item" not in classes:
            continue

        sport_values = _88z_class_texts(row, "game-type")
        league = sport_values[0] if sport_values else ""
        # 上游「法甲联赛 第8轮」与其他联赛（意甲/英超/西甲/中超）口径不一致，展示层统一为「法甲」。
        league = league.replace("法甲联赛", "法甲") if league else league
        if not league or _88Z_NON_FOOTBALL_RE.search(league):
            continue

        id_match = re.search(r"\bdata-id\s*=\s*([\"'])(\d+)\1", row, re.I)
        play_match = re.search(
            r"\bhref\s*=\s*([\"'])/live/(\d+)/play\1[^>]*>(.*?)</a\s*>",
            row, re.I | re.S,
        )
        match_id = id_match.group(2) if id_match else (play_match.group(2) if play_match else "")
        if not match_id:
            continue

        team_names = _88z_class_texts(row, "team-name")
        # 页面存在桌面/移动双份布局，去掉完全相同的重复标签并保留源顺序。
        teams = []
        for name in team_names:
            if name and name not in teams:
                teams.append(name)
        if len(teams) < 2:
            continue

        time_values = _88z_class_texts(row, "category-game-time")
        raw_time = time_values[0] if time_values else ""
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", raw_time)
        match_time = time_match.group(1) if time_match else ""
        status_label = _88z_text(play_match.group(3)) if play_match else ""
        if re.search(r"已结束|完场|完赛|比赛结束|已完赛|回看|赛后|\bFT\b", status_label, re.I):
            status = "finished"
        elif re.search(r"取消", status_label):
            status = "cancelled"
        elif re.search(r"腰斩", status_label):
            status = "abandoned"
        elif re.search(r"延期|推迟", status_label):
            status = "postponed"
        elif re.search(r"待定", status_label):
            status = "pending"
        elif any(word in status_label for word in ("直播", "进行", "半场", "加时", "点球")):
            status = "live"
        else:
            status = "upcoming"

        now = _now_cn()
        date = current_date or "%02d-%02d" % (now.month, now.day)
        date_full = current_date_full or now.strftime("%Y-%m-%d")
        channels = [
            {"src": src, "id": match_id, "name": _88Z_SOURCE_LABELS[src]}
            for src in _88Z_VISIBLE_STREAM_SRCS
        ]
        matches.append({
            "league": league,
            "league_color": "#5E5E5E",
            "time": match_time,
            "date": date,
            "date_full": date_full,
            "home": teams[0],
            "away": teams[1],
            "status": status,
            "minute": "",
            "score": None,
            "match_id": match_id,
            "channels": channels,
            "page_id": "live_" + match_id,
        })

    now = _now_cn()
    matches = [project_match_state(item, now) for item in matches]
    rank = {"live": 0, "upcoming": 1, "finished": 2}
    matches.sort(key=lambda item: (
        rank.get(item.get("status"), 1),
        -(int(item.get("minute") or 0)) if item.get("status") == "live" else item.get("time") or "",
    ))
    return matches


def parse_matches(html):
    """
    解析 m.html → 比赛列表
    列表行: <li><a href="#page_473942"><span class="league" style="background:#CCCC00">东南锦</span> 21:30 印度尼西亚 VS 越南 </a><li>
    区块:   id="page_473942" ... <a href="http://www.yoozb.live/tv/bb-45465001.html">...
    状态语义（站点标记不可靠，需时间兜底）:
      class='close'      → 完场
      class='live'/第XX' → 直播中（站点偶尔把开赛时间当 live，需时间修正）
      无标记裸时间        → 结合日期+开赛时间推断：今日已过→直播中/完场，未到→未开始
    """
    # 88zhibo 以首页 li 分组列比赛，旧解析保留供离线兼容测试与历史缓存恢复。
    if re.search(r"<li\b[^>]*class=[\"'][^\"']*\bgroup-game-item\b", html or "", re.I):
        return _parse_88zhibo_matches(html)

    # 1. 主列表行（顺序解析日期分隔行 → 每场比赛归属日期）
    items = re.findall(
        r'(<li data-role="list-divider"[^>]*>[^<]*</li>|<li>\s*<a href="#(page_\d+)"[^>]*>(.*?)</a>\s*<li>)',
        html, re.S)
    rows = []
    cur_date = None
    cur_date_full = None
    for whole, pid, raw in items:
        if 'list-divider' in whole:
            dm = re.search(r'>([^<]*)</li>', whole)
            if dm:
                m = re.search(r'(\d{4})年(\d{2})月(\d{2})日', dm.group(1))
                cur_date = "%s-%s" % (m.group(2), m.group(3)) if m else None
                cur_date_full = "-".join(m.groups()) if m else None
        elif pid:
            rows.append((cur_date, cur_date_full, pid, raw))

    # 2. 区块 → 提取频道（plu/bb/qqlive 等多路信号）与 matchId
    block_map = {}
    channel_map = {}
    for block in re.findall(r'id="(page_\d+)"(.*?)(?=id="page_|</body>)', html, re.S):
        pid, content = block
        chans = []
        seen = set()
        # 匹配两种频道链接格式：
        #   tv/plu-45417831.html / tv/bb-45417831.html  （带数字ID的专属频道）
        #   tv/qqlive1.html / tv/qqlive100.html          （公共高清线路，无数字ID）
        for cm in re.finditer(r'tv/(plu|bb)-(\d+)\.html"[^>]*>([^<]+)', content):
            key = (cm.group(1), cm.group(2))
            if key in seen:
                continue
            seen.add(key)
            chans.append({
                "src": cm.group(1),
                "id": cm.group(2),
                "name": re.sub(r'\d+$', '', cm.group(3).replace('(无插件)', '').strip()).strip(),
            })
        for cm in re.finditer(r'tv/(qqlive\d+)\.html"[^>]*>([^<]+)', content):
            key = (cm.group(1), '')
            if key in seen:
                continue
            seen.add(key)
            raw_name = cm.group(2).replace('(无插件)', '').strip()
            chans.append({
                "src": cm.group(1),
                "id": '',
                "name": raw_name,
            })
        if chans:
            block_map[pid] = chans[0]["id"]
            channel_map[pid] = chans

    def dkey(s):
        try:
            mm, dd = s.split("-")
            return (int(mm), int(dd))
        except Exception:
            return (99, 99)

    now = _now_cn()
    today_key = dkey("%02d-%02d" % (now.month, now.day))
    now_min = now.hour * 60 + now.minute

    matches = []
    for cur_date, cur_date_full, pid, raw in rows:
        # 1. 时间 span（class='live'/'close'，含直播分钟）先单独提取并移除
        tm = re.search(r"<span class=['\"](live|close)['\"]>\s*(.*?)\s*</span>", raw)
        span_state = ""
        time_str = None
        if tm:
            span_state = tm.group(1)
            time_str = tm.group(2).strip()
            rest = re.sub(r"<span class=['\"](live|close)['\"]>\s*(.*?)\s*</span>", ' ', raw)
        else:
            rest = raw

        # 2. 联赛名 + 颜色
        lm = re.search(r'<span class="league"[^>]*?background:([^">]+)["\']?[^>]*>([^<]+)</span>', rest)
        league = lm.group(2).strip() if lm else "其他"
        # 上游写法不统一：法甲带「联赛」后缀（"法甲联赛 第8轮"），与其他联赛
        # （意甲/英超/西甲/中超）口径不一致。展示层规范化：法甲联赛 -> 法甲。
        league = re.sub(r"^法甲联赛\b", "法甲", league)
        color = lm.group(1).strip() if lm else "#5E5E5E"
        rest = re.sub(r'<span class="league"[^>]*>.*?</span>', '', rest)
        rest = re.sub(r'<[^>]+>', ' ', rest)
        rest = rest.replace('&nbsp;', ' ')

        # 3. 无 span 的裸时间（如 "17:00 洛里昂 VS UNFP"）
        if not time_str:
            tm2 = re.match(r'\s*(\S+)', rest)
            if tm2 and re.match(r'^[\d第\']', tm2.group(1)):
                time_str = tm2.group(1)
                rest = rest[tm2.end():]

        # 4. 对阵: 主队 VS 客队（可能带制表符/空格）
        vs = re.search(r'(.+?)\s*VS\s*(.+)$', rest, re.I)
        if not vs:
            continue
        home = re.sub(r'\s+', ' ', vs.group(1)).strip()
        away = re.sub(r'\s+', ' ', vs.group(2)).strip()

        # 5. 状态判定：站点标记 + 时间兜底
        status = "upcoming"
        minute = None
        if span_state == "close":
            status = "finished"
        elif span_state == "live" or (time_str and ("第" in time_str or "'" in time_str)):
            status = "live"
            # 站点 live span 内容通常是开赛时间（如 19:00）而非实时分钟，
            # 直接提取数字会把开赛小时当分钟（永远显示 19'）。按格式区分：
            tmh = re.match(r'(\d{1,2}):(\d{2})', time_str or '')
            if tmh:
                # HH:MM = 开赛时间 → 用当前时间实时推算分钟（每次刷新递增）
                # 考虑中场休息：上半场 45' → 中场 15 分钟（比赛时间停在 45'）→ 下半场
                # 比赛进行时间 = 实际流逝 - 中场扣除。
                # v1.23（2026-09-06 时间准确性）：去掉旧版「显示分钟减 10」偏移——
                # 与 7m 真实数据并存时两套分钟相差 10'（用户报「时间不准确」根因）；
                # 7m 覆盖时本推算被覆盖，未覆盖时直接按流逝时间计。
                if cur_date:
                    k = dkey(cur_date)
                    t = int(tmh.group(1)) * 60 + int(tmh.group(2))
                    if k == today_key and now_min - t >= 0:
                        elapsed = now_min - t
                        if elapsed <= 45: prog = elapsed            # 上半场
                        elif elapsed <= 60: prog = 45               # 中场休息，停在 45'
                        else: prog = elapsed - 15                   # 下半场，扣 15 分钟中场
                        minute = str(max(min(prog, 90), 0))
            elif "第" in (time_str or "") or "'" in (time_str or ""):
                # "第67'" / "67'" = 实时分钟（数据源已含中场扣除）→ 直接提取
                #（v1.23 同上去掉 -10 偏移）
                mm = re.search(r'(\d+)', time_str or "")
                minute = str(max(int(mm.group(1)), 0)) if mm else None
        # 时间本身由 project_match_state 统一投影；这里不把跨日或 110 分钟
        # 直接当成完场，避免旧解析器抢先写入不可逆的 finished 状态。

        # 6. 比分: 直播行若含 "1:0" / "1 - 0" 形式则提取（数据源当前不提供，预留字段）
        score = None
        if status == "live":
            sm = re.search(r'(\d+)\s*[:：\-]\s*(\d+)', rest)
            if sm:
                score = {"home": int(sm.group(1)), "away": int(sm.group(2))}

        matches.append({
            "league": league,
            "league_color": color,
            "time": time_str,
            "date": cur_date,
            "date_full": cur_date_full,
            "home": home,
            "away": away,
            "status": status,
            # 2026-08-29 深夜 修复「null'」：minute=None 序列化成 JSON null，
            # 安卓端 optString 对 JSON null 返回字面量 "null" 直接上屏（需求 足球#1）
            "minute": minute or "",
            "score": score,
            "match_id": block_map.get(pid),
            "channels": channel_map.get(pid, []),
            "page_id": pid,
        })

    matches = [project_match_state(item) for item in matches]

    # 排序: 直播中在前，未开始次之，完场最后；同状态按时间
    rank = {"live": 0, "upcoming": 1, "finished": 2}
    def sort_key(m):
        if m["status"] == "live":
            return (0, -(int(m["minute"]) if m["minute"] else 0))
        return (rank.get(m["status"], 1), m["time"] or "")
    matches.sort(key=sort_key)

    return matches


def _build_matches_payload(matches):
    """由比赛列表组装 /api/matches 响应体（build_api 与后台刷新共用）。"""
    leagues = []
    seen = {}
    for m in matches:
        if m["league"] not in seen:
            seen[m["league"]] = m["league_color"]
            leagues.append({"name": m["league"], "color": m["league_color"]})
    payload = {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": _active_source["url"],
        "count": len(matches),
        "live_count": sum(1 for m in matches if m["status"] == "live"),
        "leagues": leagues,
        "matches": matches,
    }
    merge_7m_scores(payload)
    return _project_matches_payload(payload, strip_internal=False)


# ===== 7m 即时比分接入（yoozb 源不提供比分；7m 国内可达，含联赛/队名/状态/比分）=====
# sgb.js  = 今日全量赛程   sDt[比赛id]=[联赛,颜色,主队,客队,电视,?,天气,时,分,...]
# csxl.js = 实时状态/比分  sDt2[比赛id]=[状态,主分,客分,红牌主,红牌客,...,'Y,M,D,H,M,S'开赛,...]
# 状态码：0/''未开 1上 2中 3下 4完 5断 6取 7-9加时 10完 11点 12全 13延 14斩 15待 16金
LIVE7M_CACHE = {"ts": 0.0, "data": None}
LIVE7M_TTL = 30.0
LIVE7M_URL = "http://js-live.7m.com.cn/datafile/sgb.js"
LIVE7M_LIVE_URL = "http://js-live.7m.com.cn/livedts/csxl.js"
_LIVE7M_HOSTS = frozenset(("js-live.7m.com.cn", "data.7m.com.cn"))


def _fetch_7m_text(url, timeout=12):
    """Fetch the football score feed from its fixed host allowlist without redirects."""
    import http.client

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("http", "https") or host not in _LIVE7M_HOSTS
            or parsed.username is not None or parsed.password is not None):
        raise RuntimeError("7m fetch: host not allowed: %s" % (parsed.hostname or ""))

    default_port = 443 if parsed.scheme == "https" else 80
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(host, parsed.port or default_port, timeout=timeout)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"User-Agent": UA, "Host": parsed.netloc})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("7m fetch: HTTP %d" % response.status)
        body = response.read()
    finally:
        connection.close()

    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "ignore")

_7M_STATE_LIVE = {1, 2, 3, 5, 7, 8, 9, 11}
_7M_STATE_DONE = {4, 10, 12, 16}
_7M_STATE_VOID = {6, 13, 14, 15}
_7M_VOID_STATUS = {6: "cancelled", 13: "postponed", 14: "abandoned", 15: "pending"}

# 队名别名归一（两家数据源叫法差异）
_7M_TEAM_ALIAS = {
    "曼彻斯特联": "曼联", "曼彻斯特城": "曼城", "托特纳姆热刺": "热刺",
    "皇家马德里": "皇马", "巴塞罗那": "巴萨", "马德里竞技": "马竞",
    "国际米兰": "国米", "AC米兰": "米兰", "拜仁慕尼黑": "拜仁",
    "巴黎圣日耳曼": "巴黎", "纽卡斯尔联": "纽卡",
    "西汉姆联": "西汉姆", "皇家社会": "皇社", "毕尔巴鄂竞技": "毕尔巴鄂",
    "莱比锡红牛": "红牛", "莱斯特城": "莱斯特",
}


def _norm_team7m(n):
    n = re.sub(r"\s+", "", n or "")
    return _7M_TEAM_ALIAS.get(n, n)


def _split7m_row(body):
    """解析 7m JS 数组字面量（['a',1,'b']）→ 字符串列表（引号剥离、逗号切分）。"""
    out, buf, in_str, esc = [], "", False, False
    for ch in body:
        if in_str:
            if esc:
                buf += ch; esc = False
            elif ch == "\\":
                buf += ch; esc = True
            elif ch == "'":
                in_str = False   # 引号闭合：字段可能继续（引号内文本已在 buf）
            else:
                buf += ch
        else:
            if ch == "'":
                in_str = True
            elif ch == ",":
                out.append(buf.strip()); buf = ""
            else:
                buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _fetch_7m_scores():
    """拉 7m 比分数据（30s 缓存）。返回 {(主队norm, 客队norm): {state,hs,as,start,_id}}。"""
    now = time.time()
    if LIVE7M_CACHE["data"] is not None and now - LIVE7M_CACHE["ts"] < LIVE7M_TTL:
        return LIVE7M_CACHE["data"]
    result = {}
    try:
        sgb = _fetch_7m_text(LIVE7M_URL)
        csxl = _fetch_7m_text(LIVE7M_LIVE_URL)
        for mid, body in re.findall(r"sDt\[(\d+)\]=\[([^\]]*)\];", sgb):
            f = _split7m_row(body)
            if len(f) < 4 or not f[2] or not f[3]:
                continue
            result[(_norm_team7m(f[2]), _norm_team7m(f[3]))] = {
                "league": f[0], "state": None, "hs": "", "as": "", "start": "",
                "_id": mid,
            }
        for mid, body in re.findall(r"sDt2\[(\d+)\]=\[([^\]]*)\];", csxl):
            f = _split7m_row(body)
            if len(f) < 3:
                continue
            try:
                state = int(f[0].strip("'\"") or "0")
            except ValueError:
                state = 0
            rec = None
            for v in result.values():
                if v.get("_id") == mid:
                    rec = v; break
            if rec is None:
                continue
            rec["state"] = state
            rec["hs"] = f[1].strip("'\"")
            rec["as"] = f[2].strip("'\"")
            rec["start"] = f[8].strip("'\"") if len(f) > 8 else ""
    except Exception:
        if LIVE7M_CACHE["data"] is not None:
            return LIVE7M_CACHE["data"]  # 拉取失败降级旧缓存
        return {}
    LIVE7M_CACHE.update(ts=now, data=result)
    return result


def _7m_minute(state, start_ymd):
    """由开赛时间推算比赛分钟（上/下半场+中场扣除），与 yoozb 解析语义一致。"""
    try:
        y, mo, d, h, mi = [int(x) for x in start_ymd.split(",")[:5]]
    except (ValueError, AttributeError):
        return ""
    try:
        st = datetime(y, mo, d, h, mi)
    except ValueError:
        return ""
    elapsed = int((_now_cn() - st).total_seconds() // 60)
    if elapsed < 0:
        return ""
    if state == 2:
        return "45"
    if elapsed <= 45:
        return str(max(elapsed, 1))
    if elapsed <= 60:
        return "45"
    return str(min(max(elapsed - 15, 46), 120))


def merge_7m_scores(payload):
    """把 7m 状态/比分并回 yoozb 比赛列表（按归一化队名精确匹配）。"""
    try:
        table = _fetch_7m_scores()
        if not table:
            return
        for m in payload.get("matches", []):
            key = (_norm_team7m(m["home"]), _norm_team7m(m["away"]))
            rec = table.get(key)
            if not rec or rec.get("state") is None:
                continue
            # 日期一致才合并（7m 数据跨多天，避免同名球队隔天比赛错配）
            if rec.get("start"):
                try:
                    p = [int(x) for x in rec["start"].split(",")[:3]]
                    rec_date = "%04d-%02d-%02d" % (p[0], p[1], p[2])
                    expected_date = m.get("date_full")
                    if expected_date and rec_date != expected_date:
                        continue
                    if not expected_date and "%02d-%02d" % (p[1], p[2]) != m.get("date"):
                        continue
                except (ValueError, IndexError):
                    pass
            st = rec["state"]
            if st in _7M_STATE_VOID:
                m["status"] = _7M_VOID_STATUS.get(st, "postponed")
                m["minute"] = ""
                m["_status_authoritative"] = True
                continue
            try:
                score = {"home": int(rec["hs"]), "away": int(rec["as"])}
            except (ValueError, TypeError):
                score = None
            if st in _7M_STATE_LIVE:
                m["status"] = "live"
                m["minute"] = _7m_minute(st, rec.get("start", ""))
                m["_status_authoritative"] = True
                if score:
                    m["score"] = score
            elif st in _7M_STATE_DONE:
                m["status"] = "finished"
                m["minute"] = ""
                m["_status_authoritative"] = True
                if score:
                    m["score"] = score
    except Exception:
        pass


def _refresh_matches_bg():
    """后台刷新比赛数据（stale-while-revalidate）。
    注意：绝不持有 _cache_lock 执行抓取（锁不可重入，锁内做网络 IO 会阻塞
    全部并发读缓存请求——参考 relay 死锁教训）；只在最后回写时短暂加锁。"""
    payload = None
    error = None
    try:
        html = fetch_source()
        payload = _build_matches_payload(parse_matches(html))
    except Exception as e:
        # 旧 CloudBase 赛程接口已退役。返回失败供 Android 切换足球代理，
        # 避免在无效备用接口上再次等待超时。
        error = str(e)[:200] or "比赛列表刷新失败"
    with _cache_lock:
        if payload is not None:
            _cache.update(ts=time.time(), data=payload, raw_error=None)
        else:
            _cache["raw_error"] = error or "比赛列表刷新失败"
        _cache["refreshing"] = False
    _cache_refresh_event.set()


def build_api(wait_for_fresh=False):
    """组装 /api/matches；普通调用保持 SWR，fresh=1 等待单飞刷新结果。"""
    now = time.time()
    run_inline = False
    start_background = False
    should_wait = False
    with _cache_lock:
        if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
            return _project_matches_payload(_cache["data"]), True
        stale = _cache["data"]
        if not _cache["refreshing"]:
            _cache["refreshing"] = True
            _cache_refresh_event.clear()
            if stale is None or wait_for_fresh:
                run_inline = True
            else:
                start_background = True
        elif wait_for_fresh or stale is None:
            should_wait = True
        if stale is not None and not wait_for_fresh:
            if start_background:
                threading.Thread(target=contextvars.copy_context().run,
                                 args=(_refresh_matches_bg,), daemon=True).start()
            return _project_matches_payload(stale), True

    if run_inline:
        _refresh_matches_bg()
    elif start_background:
        threading.Thread(target=contextvars.copy_context().run,
                         args=(_refresh_matches_bg,), daemon=True).start()
        if not wait_for_fresh:
            with _cache_lock:
                return _project_matches_payload(_cache["data"]), True
    if should_wait or wait_for_fresh:
        _cache_refresh_event.wait(timeout=35)

    with _cache_lock:
        data = _cache["data"]
        # A completed refresh replaces the payload. Wall-clock ticks can be equal
        # for quick requests (or move backwards), so timestamps cannot prove it.
        updated = data is not None and data is not stale
        if data is not None:
            return _project_matches_payload(data), not updated
        error = _cache.get("raw_error") or "比赛列表刷新失败"
    return {"error": error, "matches": [], "count": 0}, False


def _matches_keepwarm():
    """（v1.23 已停用，保留代码备查）后台保热线程。2026-09-06 MuMu 实测：App 内嵌
    后端（Chaquopy）跑 ~30 分钟后整体僵死——HTTP server 连 /api/health 都不响应，
    最终拖垮模拟器 adb 需重启实例。桌面同代码无异常，指向 Chaquopy 线程/GIL 环境
    差异；唯一新增常驻负载就是本线程，稳妥起见停用。实时性由 CACHE_TTL=30s +
    SWR（请求触达即后台刷新）+ 前端 25s 轮询承担，效果接近。云端函数若需要可
    通过环境变量 OTV_KEEPWARM=1 单独开启（常驻实例不受 Chaquopy 约束）。"""
    import os as _os
    if _os.environ.get("OTV_KEEPWARM") != "1":
        return
    while True:
        time.sleep(40)
        with _cache_lock:
            if _cache["refreshing"]:
                continue
            _cache["refreshing"] = True
        _refresh_matches_bg()


threading.Thread(target=_matches_keepwarm, daemon=True, name="matches-keepwarm").start()


# ===== 球迹直播线路解析 =====


def fetch_url(url, referer=None, timeout=20):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    if HAS_URLLIB3:
        resp = _http.request(
            "GET", url, headers=headers,
            timeout=urllib3.Timeout(connect=5, read=timeout),
        )
        return resp.data.decode("utf-8", errors="ignore")
    else:
        req = urlreq.Request(url, headers=headers)
        with _direct_opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")


STREAM_SUCCESS_TTL = 60

# 单线路解析成功缓存。动态线路发现与播放续签都会复用它，避免重复探测同一上游。
STREAM_CACHE = {}
STREAM_CACHE_MAX = 500
_stream_lock = threading.Lock()

# 直播「信号源清单」短缓存（2026-10-17 用户报障「球迹直播经常显示无法查找到可用信号源，
# 彻底退出 app 再进又可以播放」）：
# 一次打开播放器 = 源站清单 1 次 + 每条线路 3~4 次探测（大场次 10+ 线 = 40+ 次上游请求）。
# 用户重试/反复进出播放器时这份探测风暴会被源站按 IP 限流/拦爬，之后**每次**解析都拿不到
# 线路 → 播放器「无可用信号源」；过一段时间（或重启进程后）限流窗口过去才自愈，用户感知
# 就是「退出 app 重进就好了」。三层保护：
#   ① 20s 内重复打开直接复用上一次清单（fresh=1 绕过，续签/换源照旧实时）；
#   ② 同一场比赛并发请求单飞（重试风暴不再叠加成更多上游探测）；
#   ③ 解析失败（限流/超时/清单读取失败）时回退最近一次成功清单（180s 内），
#      把上游抖动挡在播放器之外；「所有线路确实都没流」仍如实返回空，不拿旧数据骗用户。
STREAM_ALL_TTL = 20.0
STREAM_ALL_STALE_TTL = 180.0
# fresh=1 的合并窗口（2026-10-17 实测：同场 3 个并发 fresh=1 会串行重解析 3 次，
# 3.5s×3=10.6s，用户连点重试等于把等待叠加）。3 秒内刚完成的解析已经是「刚刚的现场」，
# 直接复用既不影响 fresh 语义，也不会让重试风暴放大成更多上游探测。
STREAM_ALL_FRESH_COALESCE = 3.0
STREAM_ALL_CACHE_MAX = 48
_stream_all_cache = {}
_stream_all_lock = threading.Lock()
_stream_all_locks = {}
_stream_all_locks_guard = threading.Lock()
# 源站线路清单缓存（同一场比赛的清单在一次播放会话里被反复请求：all 枚举 + 单线续签）
_88Z_SOURCE_LINKS_TTL = 20.0
_88z_source_links_cache = {}


def _stream_all_match_lock(match_id):
    """同场解析单飞用的锁（按场次隔离；表超过上限时清掉未持有的旧锁）。"""
    with _stream_all_locks_guard:
        lock = _stream_all_locks.get(match_id)
        if lock is not None:
            return lock
        if len(_stream_all_locks) >= STREAM_ALL_CACHE_MAX:
            for key in [k for k, v in _stream_all_locks.items() if not v.locked()]:
                _stream_all_locks.pop(key, None)
                if len(_stream_all_locks) < STREAM_ALL_CACHE_MAX // 2:
                    break
        lock = threading.Lock()
        _stream_all_locks[match_id] = lock
        return lock


def _decode_88zhibo_source_links(response_text):
    """解开 /live/{id}/source 的前后包装 Base64 数据并返回 links 列表。"""
    outer = json.loads(response_text)
    wrapped = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(wrapped, str) or len(wrapped) <= 8:
        raise ValueError("88zhibo source 响应缺少 data")
    encoded = re.sub(r"\s+", "", wrapped[6:-2])
    if not encoded:
        raise ValueError("88zhibo source data 为空")
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    decoded = base64.b64decode(encoded, altchars=b"-_", validate=True).decode("utf-8")
    payload = json.loads(decoded)
    links = payload.get("links") if isinstance(payload, dict) else None
    if not isinstance(links, list):
        raise ValueError("88zhibo source data 缺少 links")
    return [item for item in links if isinstance(item, dict)]


def _88z_direct_hls_url(link_url):
    """从 88player 的 m3u8.html 包装页提取源 HLS URL，不截断未编码的内层查询串。"""
    parsed = urlsplit(_html.unescape(str(link_url or "").strip()))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if not (parsed.hostname.lower() == "88player.top" or parsed.hostname.lower().endswith(".88player.top")):
        return None
    if not parsed.path.rstrip("/").lower().endswith("/m3u8.html"):
        return None
    match = re.search(r"(?:^|&)url=(.+)$", parsed.query, re.I | re.S)
    if not match:
        return None
    stream_url = _html.unescape(unquote(match.group(1).strip()))
    stream = urlsplit(stream_url)
    if (stream.scheme not in ("http", "https") or not stream.hostname or
            not re.search(r"\.m3u8$", stream.path, re.I) or _blocked_internal_url(stream_url)):
        return None
    return stream_url


def _88z_hls_candidates(text, base_url):
    """从页面/脚本源码中找显式 HLS URL；只解析文本，不执行第三方 JS。"""
    source = _html.unescape(str(text or ""))
    source = source.replace(r"\/", "/").replace(r"\u002f", "/").replace(r"\x2f", "/")
    pattern = re.compile(
        r"(?:(?:https?:)?//|/|\.\.?/)?[A-Za-z0-9._~:/?%+&=,@!-]+?\.m3u8"
        r"(?:\?[A-Za-z0-9._~:/?%+&=,@!-]*)?",
        re.I,
    )
    seen = set()
    for raw in pattern.findall(source):
        candidate = urljoin(base_url, raw.rstrip("),;]"))
        parsed = urlsplit(candidate)
        if (candidate in seen or parsed.scheme not in ("http", "https") or
                not parsed.hostname or not re.search(r"\.m3u8$", parsed.path, re.I) or
                _blocked_internal_url(candidate)):
            continue
        seen.add(candidate)
        yield candidate


def _88z_fetch_embed_script(script_url, page_url, timeout=12):
    """请求 embed.st 的数据脚本时带浏览器脚本请求头，但不执行响应内容。"""
    parsed = urlsplit(script_url)
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() != "capitalhospitals.org" or
            _blocked_internal_url(script_url)):
        raise ValueError("embed 播放脚本不是受支持的 HTTPS 来源")
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": page_url,
        "Origin": "https://embed.st",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "script",
    }
    if HAS_URLLIB3:
        response = _http.request(
            "GET", script_url, headers=headers,
            timeout=urllib3.Timeout(connect=5, read=timeout), redirect=False,
        )
        status = response.status
        body = response.data or b""
        response.release_conn()
    else:
        request = urlreq.Request(script_url, headers=headers)
        try:
            with _no_redirect_opener.open(request, timeout=timeout) as response:
                status = response.getcode()
                body = response.read()
        except urlreq.HTTPError as exc:
            status = exc.code
            body = exc.read()
            exc.close()
    if not 200 <= status < 300:
        raise ValueError("embed 播放脚本请求失败 HTTP %s" % status)
    if not body:
        raise ValueError("embed 播放脚本响应为空")
    return body.decode("utf-8", errors="ignore")


def _88z_embed_st_hls_url(page_url):
    """尝试从 embed.st 的赛事、酒店与高尔夫页面静态提取 HLS；不运行第三方 JS。"""
    parsed = urlsplit(page_url)
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() != "embed.st" or
            not parsed.path.startswith("/embed/")):
        raise ValueError("该独立线路不是受支持的 embed.st 页面")
    page = fetch_url(page_url, referer=SOURCE_URL, timeout=20)
    for stream_url in _88z_hls_candidates(page, page_url):
        return stream_url

    class ScriptSourceParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.urls = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "script":
                return
            src = dict(attrs).get("src")
            if src:
                self.urls.append(urljoin(page_url, _html.unescape(src.strip())))

    parser = ScriptSourceParser()
    parser.feed(page or "")
    parser.close()
    script_urls = list(dict.fromkeys(parser.urls))
    if not script_urls:
        raise ValueError("embed.st 页面没有可静态解析的 HLS 地址或数据脚本")
    script_urls.sort(key=lambda url: 0 if (urlsplit(url).hostname or "").lower() == "capitalhospitals.org" else 1)
    saw_forbidden = False
    for script_url in script_urls[:6]:
        try:
            if (urlsplit(script_url).hostname or "").lower() == "capitalhospitals.org":
                script = _88z_fetch_embed_script(script_url, page_url)
                script_base = script_url
            else:
                script, script_base = _88z_fetch_public_text(script_url, page_url, timeout=8, max_bytes=512 * 1024)
            for stream_url in _88z_hls_candidates(script, script_base):
                return stream_url
        except ValueError as exc:
            if "HTTP 403" in str(exc):
                saw_forbidden = True
        except Exception:
            continue
    try:
        stream_url, _referer = _88z_generic_embed_hls_url(page_url)
        return stream_url
    except Exception:
        pass
    if saw_forbidden:
        raise ValueError("embed.st 数据脚本返回 HTTP 403，无法静态解析 HLS")
    raise ValueError("embed.st 页面脚本未提供可静态解析的 HLS 地址")


def _88z_fetch_public_text(url, referer, timeout=12, max_bytes=1024 * 1024):
    """读取一个公开播放器页面或脚本；所有跳转逐跳经过现有 SSRF 防护。"""
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or _blocked_internal_url(url):
        raise ValueError("播放器页面不是受支持的公开 HTTPS 地址")
    headers = {"User-Agent": UA, "Accept": "text/html,application/javascript,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    response = None
    try:
        response, final_url = _open_upstream(
            url, headers, read_timeout=timeout, preload=False, connect_timeout=5,
        )
        status = getattr(response, "status", None)
        if status is None and hasattr(response, "getcode"):
            status = response.getcode()
        if not status or not 200 <= status < 300:
            raise ValueError("播放器页面请求失败 HTTP %s" % (status or "?"))
        if HAS_URLLIB3:
            body = response.read(max_bytes, decode_content=True)
        else:
            body = response.read(max_bytes)
        if not body:
            raise ValueError("播放器页面响应为空")
        return body.decode("utf-8", errors="ignore"), final_url
    finally:
        if response is not None:
            try:
                if HAS_URLLIB3:
                    response.release_conn()
                else:
                    response.close()
            except Exception:
                pass


def _88z_generic_embed_hls_url(page_url, depth=0, seen=None):
    """静态检查其他公开播放器页及少量 iframe/script；不执行第三方 JavaScript。"""
    seen = seen if seen is not None else set()
    page_url = str(page_url or "").strip()
    if page_url in seen or depth > 2:
        raise ValueError("播放器页面没有可静态解析的 HLS 地址")
    seen.add(page_url)
    page, final_url = _88z_fetch_public_text(page_url, SOURCE_URL)
    candidates = list(_88z_hls_candidates(page, final_url))
    if candidates:
        return candidates[0], final_url

    class EmbeddedPageParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.iframes = []
            self.scripts = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            tag = tag.lower()
            if tag == "iframe" and attrs.get("src"):
                self.iframes.append(urljoin(final_url, _html.unescape(attrs["src"].strip())))
            elif tag == "script" and attrs.get("src"):
                self.scripts.append(urljoin(final_url, _html.unescape(attrs["src"].strip())))

    parser = EmbeddedPageParser()
    parser.feed(page)
    parser.close()

    for frame_url in parser.iframes[:4]:
        try:
            return _88z_generic_embed_hls_url(frame_url, depth + 1, seen)
        except Exception:
            continue

    # Inspect only a few public script files and never evaluate them. Same-origin
    # scripts are checked first; external scripts are still guarded against private
    # hosts and unsafe redirects by _88z_fetch_public_text.
    script_urls = sorted(
        dict.fromkeys(parser.scripts),
        key=lambda candidate: 0 if urlsplit(candidate).hostname == urlsplit(final_url).hostname else 1,
    )
    for script_url in script_urls[:4]:
        try:
            script, script_final = _88z_fetch_public_text(script_url, final_url, timeout=8, max_bytes=512 * 1024)
            candidates = list(_88z_hls_candidates(script, script_final))
            if candidates:
                return candidates[0], final_url
        except Exception:
            continue
    raise ValueError("播放器页面没有可静态解析的 HLS 地址")


def _88z_source_links(match_id, fresh=False):
    if not str(match_id).isdigit():
        raise ValueError("invalid_match_id")
    key = str(match_id)
    now = time.time()
    if not fresh:
        hit = _88z_source_links_cache.get(key)
        if hit and now - hit[0] < _88Z_SOURCE_LINKS_TTL:
            return hit[1]
    endpoint = "https://www.88zhibo.tv/live/%s/source" % match_id
    raw = fetch_url(endpoint, referer=SOURCE_URL, timeout=20)
    links = _decode_88zhibo_source_links(raw)
    _trim_cache(_88z_source_links_cache, STREAM_ALL_CACHE_MAX)
    _88z_source_links_cache[key] = (time.time(), links)
    return links


def _88z_source_link_for_src(src, links):
    dynamic = _88Z_DYNAMIC_SOURCE_RE.fullmatch(str(src or ""))
    if dynamic:
        index = int(dynamic.group(1))
        if index >= len(links):
            raise ValueError("当前比赛没有该线路")
        item = links[index]
        label = _88z_text(str(item.get("name") or "")) or "线路%d" % (index + 1)
        return item, label
    label = _88Z_SOURCE_LABELS.get(src)
    if not label:
        raise ValueError("unsupported_source")
    source_link = next(
        (item for item in links if _88z_text(str(item.get("name") or "")) == label),
        None,
    )
    if source_link is None:
        raise ValueError("当前比赛没有%s" % label)
    return source_link, label


def _resolve_88zhibo_link(source_link, label):
    """将源站任一线路项静态解析为原生播放器可用的 HLS URL 与 Referer。"""
    link_url = source_link.get("url")
    if not isinstance(link_url, str) or not link_url.strip():
        raise ValueError("%s没有播放地址" % label)
    parsed = urlsplit(link_url)
    host = (parsed.hostname or "").lower()
    wrapped = host.endswith(".88player.top")
    if parsed.scheme not in ("http", "https") or not host or _blocked_internal_url(link_url):
        raise ValueError("%s播放地址被安全策略拒绝" % label)
    if re.search(r"\.m3u8$", parsed.path, re.I):
        stream_url, embed_referer = link_url, SOURCE_URL
    elif host == "embed.st":
        stream_url = _88z_embed_st_hls_url(link_url)
        embed_referer = link_url
    else:
        stream_url = _88z_direct_hls_url(link_url)
        if not stream_url:
            stream_url, _player_page = _88z_generic_embed_hls_url(link_url)
        # m3u8.html/播放器页是 CDN 清单和分片校验 Referer 的来源页。
        embed_referer = link_url if wrapped or stream_url != link_url else SOURCE_URL
    if not stream_url:
        raise ValueError("无法从%s提取 HLS 直链" % label)
    if _blocked_internal_url(stream_url):
        raise ValueError("%s播放地址被安全策略拒绝" % label)
    return stream_url, embed_referer, label


def _resolve_88zhibo_direct(match_id, src, source_links=None, fresh=False):
    """解析源站源清单中的一个线路；line IDs 对应本次清单的原始下标。"""
    links = source_links if source_links is not None else _88z_source_links(match_id, fresh=fresh)
    source_link, label = _88z_source_link_for_src(src, links)
    stream_url, referer, _label = _resolve_88zhibo_link(source_link, label)
    return stream_url, referer, label


def resolve_88zhibo_stream(match_id, src="a2", fresh=False, source_links=None):
    """88zhibo 单线路入口；动态 line IDs 指向本场源站清单的下标。"""
    if src not in _88Z_STREAM_SRCS and not _88Z_DYNAMIC_SOURCE_RE.fullmatch(str(src or "")):
        return {"url": None, "error": "unsupported_source"}, False
    if not str(match_id).isdigit():
        return {"url": None, "error": "invalid_match_id"}, False
    now = time.time()
    key = "%s-%s" % (src, match_id)
    with _stream_lock:
        cached = STREAM_CACHE.get(key)
        if not fresh and cached and cached["ts"] > 0 and now - cached["ts"] < STREAM_SUCCESS_TTL:
            return cached["data"], True
    try:
        stream_url, embed_referer, label = _resolve_88zhibo_direct(
            str(match_id), src, source_links=source_links, fresh=fresh,
        )
        if not _probe_88z_hls_playable(stream_url, embed_referer):
            raise ValueError("%s清单或媒体分片当前不可用" % label)
        payload = {
            "url": stream_url,
            "player": "m3u8",
            "source": src,
            "name": label,
            "referer": embed_referer,
            "error": None,
        }
        with _stream_lock:
            _trim_cache(STREAM_CACHE, STREAM_CACHE_MAX)
            STREAM_CACHE[key] = {"ts": now, "data": payload}
        return payload, False
    except Exception as exc:
        payload = {"url": None, "error": str(exc)[:160] or "88zhibo 线路解析失败"}
        return payload, False


def _resolve_88zhibo_streams_uncached(match_id, fresh=False):
    """按源站本场清单枚举线路，只返回 HLS 解析并通过上游探测的线路。"""
    if not str(match_id).isdigit():
        return {"url": None, "sources": [], "error": "invalid_match_id"}, False
    try:
        links = _88z_source_links(str(match_id))
    except Exception as exc:
        # retryable=True：清单读取失败是上游/网络抖动（限流、超时），调用方可以复用
        # 上一次成功清单（见 resolve_88zhibo_streams 的 stale 回退），而不是直接空手。
        return {
            "url": None, "sources": [],
            "error": str(exc)[:160] or "88zhibo 线路列表读取失败",
            "retryable": True,
        }, False

    # 保留源站原顺序，同时压掉完全重复的条目。A2/migu/Q3/C4 仍按用户先前
    # 指定的顺序优先，其余每场比赛实际返回的线路紧随其后。
    entries = []
    seen = set()
    for index, item in enumerate(links):
        raw_name = item.get("name") if isinstance(item, dict) else ""
        raw_url = item.get("url") if isinstance(item, dict) else ""
        name = _88z_text(str(raw_name or ""))
        url = str(raw_url or "").strip()
        if not name or not url or (name, url) in seen:
            continue
        seen.add((name, url))
        entries.append({"src": "88z_%d" % index, "index": index, "name": name, "item": item})

    preferred_order = {"线路A2": 0, "线路migu": 1, "线路Q3": 2, "线路C4": 3}
    entries.sort(key=lambda item: (preferred_order.get(item["name"], 4), item["index"]))
    label_counts = {}
    for item in entries:
        label_counts[item["name"]] = label_counts.get(item["name"], 0) + 1
        if label_counts[item["name"]] > 1:
            item["display_name"] = "%s (%d)" % (item["name"], label_counts[item["name"]])
        else:
            item["display_name"] = item["name"]

    slots = _football_stream_slots
    accepted = []
    for item in entries:
        if not slots.acquire(blocking=False):
            break
        accepted.append(item)

    if entries and not accepted:
        return {
            "url": None, "sources": [],
            "error": "线路解析繁忙，请稍后重试", "retryable": True,
        }, False

    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, max(1, len(accepted))),
        thread_name_prefix="football-stream",
    )
    futures = {}
    for item in accepted:
        try:
            future = pool.submit(
                resolve_88zhibo_stream,
                str(match_id), item["src"], fresh, links,
            )
        except Exception:
            slots.release()
            continue
        future.add_done_callback(lambda _done, semaphore=slots: semaphore.release())
        futures[future] = item

    available = []
    processed = set()

    def collect(completed):
        for future in completed:
            if future in processed:
                continue
            processed.add(future)
            item = futures[future]
            try:
                payload, _cached = future.result()
            except Exception:
                continue
            if not payload.get("url"):
                continue
            payload = dict(payload)
            payload["name"] = item["display_name"]
            payload["source"] = item["src"]
            available.append(payload)

    unresolved = set()
    try:
        first_deadline = time.monotonic() + FOOTBALL_STREAMS_WAIT_BUDGET
        done, unresolved = concurrent.futures.wait(
            futures,
            timeout=max(0.0, first_deadline - time.monotonic()),
            return_when=concurrent.futures.ALL_COMPLETED,
        )
        collect(done)

        # A successful route is enough to start playback; do not wait for weak
        # alternatives. If none succeeded in the quick window, keep listening
        # briefly so a slower but usable route is not reported as unavailable.
        if not available and unresolved:
            final_deadline = time.monotonic() + max(
                0.0,
                FOOTBALL_STREAMS_MAX_WAIT - FOOTBALL_STREAMS_WAIT_BUDGET,
            )
            while unresolved:
                remaining = final_deadline - time.monotonic()
                if remaining <= 0:
                    break
                completed, unresolved = concurrent.futures.wait(
                    unresolved,
                    timeout=remaining,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not completed:
                    break
                collect(completed)
                if available and unresolved:
                    remaining = final_deadline - time.monotonic()
                    if remaining > 0:
                        extra, unresolved = concurrent.futures.wait(
                            unresolved,
                            timeout=min(FOOTBALL_STREAMS_COALESCE_WINDOW, remaining),
                            return_when=concurrent.futures.ALL_COMPLETED,
                        )
                        collect(extra)
                    break
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        # Active requests finish independently; the shared semaphore caps all
        # per-request work, and shutdown cancels queued tasks instead of waiting
        # for the slowest upstream route before answering the player.
        pool.shutdown(wait=False, cancel_futures=True)

    # Future completion order depends on CDN latency. Restore deterministic order.
    order = {item["src"]: position for position, item in enumerate(entries)}
    available.sort(key=lambda item: order.get(item.get("source"), len(order)))
    first = available[0] if available else None
    if not available and unresolved:
        error = "线路解析超时，请稍后重试"
        retryable = True
    elif not available and len(accepted) < len(entries):
        error = "线路解析繁忙，已尝试部分线路，请稍后重试"
        retryable = True
    else:
        error = None if available else "当前没有可播放的线路"
        retryable = False
    return {
        "url": first.get("url") if first else None,
        "player": "m3u8" if first else None,
        "source": first.get("source") if first else None,
        "name": first.get("name") if first else None,
        "referer": first.get("referer") if first else None,
        "sources": available,
        "error": error,
        "retryable": retryable,
    }, False


def resolve_88zhibo_streams(match_id, fresh=False):
    """src=all 入口：短缓存 + 同场单飞 + 失败回退上一次成功清单。

    见 STREAM_ALL_TTL 头注释。返回 (payload, cached)；cached=True 表示本次没有重新
    探测上游（20s 内复用或 stale 回退）。stale 回退的 payload 带 "stale": True，
    客户端可据此提示「正在更新信号源」，也可以完全忽略（字段向后兼容）。"""
    key = str(match_id)
    if not fresh:
        with _stream_all_lock:
            hit = _stream_all_cache.get(key)
            if hit and time.time() - hit["ts"] < STREAM_ALL_TTL:
                return hit["data"], True
    lock = _stream_all_match_lock(key)
    # acquire 必须带超时：上一轮请求即使已给客户端返回（3.5s 快窗口），
    # 其后台残余线程仍在逐线路探活（read 最长 15s/线路，全场最坏约 45s）并持有本锁。
    # 无限 acquire 会让用户的重试请求挂在 HTTP 线程上（真机表现=点播放永远转圈）。
    # 45s 覆盖后台残余的最坏收尾时长；仍未拿到就如实报忙，客户端按 retryable 自愈。
    if not lock.acquire(timeout=45.0):
        return {
            "url": None, "sources": [],
            "error": "线路解析繁忙，请稍后重试", "retryable": True,
        }, False
    try:
        with _stream_all_lock:
            hit = _stream_all_cache.get(key)
            ttl = STREAM_ALL_TTL if not fresh else STREAM_ALL_FRESH_COALESCE
            if hit and time.time() - hit["ts"] < ttl:
                return hit["data"], True
        payload, cached = _resolve_88zhibo_streams_uncached(key, fresh)
        now = time.time()
        with _stream_all_lock:
            if payload.get("sources"):
                _trim_cache(_stream_all_cache, STREAM_ALL_CACHE_MAX)
                _stream_all_cache[key] = {"ts": now, "data": payload}
                return payload, cached
            # 只有「上游抖动型」失败才复用旧清单；「所有线路都没流」是真实无信号，
            # 继续如实返回空（否则会把已结束的比赛伪装成可播）。
            hit = _stream_all_cache.get(key)
            if payload.get("retryable") and hit and now - hit["ts"] < STREAM_ALL_STALE_TTL:
                stale = dict(hit["data"])
                stale["stale"] = True
                stale["cachedAt"] = int(hit["ts"])
                print("[88z] match=%s 解析失败（%s）→ 复用 %ds 前的信号源清单"
                      % (key, payload.get("error"), int(now - hit["ts"])))
                return stale, True
        return payload, cached
    finally:
        lock.release()




def _trim_cache(cache, max_items):
    """缓存上限清理：超出时清掉最旧的一半（必须在持有对应锁的线程内调用）。
    兼容两种值结构：STREAM_CACHE 的 dict（"ts" 键）与 RELAY_CACHE 的元组（[0] 时间戳）。"""
    if len(cache) <= max_items:
        return
    try:
        def ts_of(v):
            return v.get("ts", 0) if isinstance(v, dict) else v[0]
        cutoff = sorted(cache.values(), key=ts_of)[len(cache) // 2]
        cutoff = ts_of(cutoff)
        for k in [k for k, v in cache.items() if ts_of(v) < cutoff]:
            cache.pop(k, None)
    except Exception:
        pass  # 清理失败不影响主流程


# 中继分片内存缓存：hls.js 分片请求高频且重试风暴会重复打 CDN（模拟器/弱网下放大卡顿）。
# .ts 分片短缓存直接返回，m3u8 播放列表永不缓存（需实时刷新）。
# TTL=120s：直播滑动窗口旧分片自然淘汰（LRU trim 兜底），预取只取清单中
# 已存在的后继片，不会预取到不存在的分片。
RELAY_CACHE = {}
RELAY_CACHE_TTL = 120.0
RELAY_CACHE_MAX = 96                  # 条目上限（app 单观看端 96 条足够；原 300 为 web 多观众场景）
RELAY_CACHE_BYTES = 48 * 1024 * 1024  # 字节预算（2026-08-30 长测实测：1.5-2Mbps 码率下 300 条
                                      # ≈ 300-450MB 常驻——app 原生堆锯齿式 +80MB/5min 的主因，
                                      # 低配目标机内存压力卡顿/杀后台风险 → 双上限裁剪）
_relay_lock = threading.Lock()


def _trim_relay_bytes():
    """RELAY_CACHE 字节预算裁剪：总量超预算时从最旧开始丢弃（须持 _relay_lock 调用）。
    保底 8 条：预取窗口(PREFETCH_AHEAD=4)+客户端在途请求，避免把活跃分片裁掉。"""
    total = 0
    for v in RELAY_CACHE.values():
        try:
            total += len(v[3])
        except Exception:
            pass
    while total > RELAY_CACHE_BYTES and len(RELAY_CACHE) > 8:
        oldest_key = None
        oldest_ts = None
        for k, v in RELAY_CACHE.items():
            ts = v[0] if v else 0
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
                oldest_key = k
        if oldest_key is None:
            break
        v = RELAY_CACHE.pop(oldest_key)
        try:
            total -= len(v[3])
        except Exception:
            pass


def _trim_relay():
    """relay 缓存统一裁剪（条目数 + 字节预算；须持 _relay_lock 调用）"""
    _trim_cache(RELAY_CACHE, RELAY_CACHE_MAX)
    _trim_relay_bytes()

# ===== 清单感知多连接分片预取（对抗 CDN 每连接限速）=====
# 实测 2026-08-24 深夜：这些免费 CDN 按"每连接"限速——单连接 ~70KB/s，
# 双连接并发总吞吐 ~155KB/s（2.2 倍）。而 hls.js 只开单连接顺序拉分片，
# 供需比仅 40%（HN 线实测：码率 1878kbps vs 供给 755kbps）→ 必然卡顿。
# 中继在重写 m3u8 时记住各清单的分片顺序；每当一个分片被客户端请求，
# 后台并发预取其后 PREFETCH_AHEAD 片入 RELAY_CACHE —— hls.js 下一片请求
# 直接命中缓存，等效供给速度 = 并发连接数 × 单连接限速（4×70=280KB/s）。
# v1.22（2026-09-05 需求⑥ 球迹直播卡顿）：6→8——直播分片供给波动的余量翻倍，
# 字节预算（48MB）不变仍兜底内存。
PREFETCH_AHEAD = 8
# 直播播放器从清单末端（直播边缘附近）起播；清单首次出现时只预热最老分片会
# 让客户端首片仍要现场下载。最近 4 片按“最新→较旧”提交，优先命中起播窗口；
# 后续清单刷新时已缓存/在途的分片会被去重，不会重复抓取。
STARTUP_PREFETCH_SEGMENTS = 4
MANIFEST_SEGS = {}          # 媒体 m3u8 url -> (原始分片 url 顺序表, ts)
MANIFEST_SEGS_MAX = 60
_MANIFEST_LOCK = threading.Lock()
_prefetch_inflight = set()  # 正在预取的分片 url（防重复提交）
_prefetch_lock = threading.Lock()
try:
    from concurrent.futures import ThreadPoolExecutor
    _prefetch_pool = ThreadPoolExecutor(max_workers=PREFETCH_AHEAD + 2, thread_name_prefix="pf")
except Exception:
    _prefetch_pool = None


def _prefetch_segments(segs, ref):
    """把一批上游分片 URL 提交后台预取入 RELAY_CACHE（去重：已缓存/在途跳过）。"""
    now = time.time()
    for su in segs:
        cache_key = _relay_cache_key(su)
        with _relay_lock:
            hit = RELAY_CACHE.get(cache_key)
            if hit and now - hit[0] < RELAY_CACHE_TTL:
                continue
        with _prefetch_lock:
            if cache_key in _prefetch_inflight:
                continue
            _prefetch_inflight.add(cache_key)

        context = contextvars.copy_context()

        def task(su=su, ref=ref, cache_key=cache_key, context=context):
            try:
                # 预取复用 relay_url 主路径——自带 SSRF 防线（入口 host 校验）、
                # 双 Referer 策略、防盗链垃圾体守卫，≤8MB 响应自动写入 RELAY_CACHE。
                # 此前预取直接引用 _http：内嵌后端（Chaquopy）只有标准库、无 urllib3 →
                # NameError 被 except 吞掉 → **设备上预取从未生效**（供给=单连接 ~1×，
                # 设备上预取使用与直播相同的网络路径。
                context.run(relay_url, su, ref)
            except Exception:
                pass  # 预取失败静默：hls.js 正常请求路径仍可用
            finally:
                with _prefetch_lock:
                    _prefetch_inflight.discard(cache_key)

        if _prefetch_pool:
            _prefetch_pool.submit(task)
        else:
            threading.Thread(target=task, daemon=True).start()


def _prefetch_after(u, ref):
    """预取 u 在其所属清单中的后 PREFETCH_AHEAD 片（找不到清单则忽略）。"""
    try:
        segs = None
        with _MANIFEST_LOCK:
            for _manifest_key, (lst, ts) in MANIFEST_SEGS.items():
                if u in lst:
                    segs = lst
                    break
        if not segs:
            return
        i = segs.index(u)
        _prefetch_segments(segs[i + 1: i + 1 + PREFETCH_AHEAD], ref)
    except Exception:
        pass


def _remember_manifest(u, raw_segs, ref):
    """记录媒体清单的分片顺序，并预热直播边缘的最近分片以加速首帧。"""
    try:
        with _MANIFEST_LOCK:
            MANIFEST_SEGS[_relay_cache_key(u)] = (raw_segs, time.time())
            if len(MANIFEST_SEGS) > MANIFEST_SEGS_MAX:
                oldest = min(MANIFEST_SEGS.items(), key=lambda kv: kv[1][1])[0]
                MANIFEST_SEGS.pop(oldest, None)
        # 播放器通常直接请求倒数第一/第二片；最新优先提交可先占满预取连接，
        # 避免仍从清单头部开始下载而与客户端首片竞争。
        _prefetch_segments(
            list(reversed(raw_segs[-STARTUP_PREFETCH_SEGMENTS:])), ref)
    except Exception:
        pass


def _blocked_internal_url(u):
    """
    SSRF 防护：拒绝指向回环/链路本地/私网/元数据地址的 URL（含 127.0.0.0/8、
    0.0.0.0、::1、169.254.169.254、10/8、172.16/12、192.168/16）。
    直接按 host 字符串判定即可覆盖"等价回环"绕过（如 127.0.0.2、0.0.0.1），
    不做 DNS 解析（避免对外部 host 的额外解析请求）。返回 True=拦截。
    """
    try:
        host = urlparse(u).hostname or ""
    except Exception:
        host = ""
    if not host:
        return True
    h = host.strip().lower().rstrip(".")
    if h == "localhost":
        return True
    # IPv6 规范化（去掉 [] 与作用域），支持 ::1 / [::ffff:127.0.0.1] 等
    if h.startswith("["):
        h = h.strip("[]")
    if "%" in h:
        h = h.split("%", 1)[0]
    if h == "::1" or h == "0:0:0:0:0:0:0:1":
        return True
    # IPv4-mapped IPv6（::ffff:a.b.c.d 或 ::ffff:xxxx:xxxx 十六进制形式）→ 取内嵌 IPv4 判断
    m = re.match(r"^(?:::)?(?:0*:)*0*ffff:([0-9a-f.:]+)$", h)
    if m:
        inner = m.group(1)
        if "." in inner:
            h = inner
        else:
            # 内嵌为十六进制组：4 组（::ffff:c0a8:0101:0101）或 2 组（::ffff:c0a8:0101）→ 转点分 IPv4
            hx = inner.split(":")
            try:
                if len(hx) == 2:
                    a = int(hx[0], 16); b = int(hx[1], 16)
                    h = f"{(a >> 8) & 255}.{a & 255}.{(b >> 8) & 255}.{b & 255}"
                elif len(hx) == 4:
                    h = ".".join(str(int(x, 16)) for x in hx)
                else:
                    # 无法解码的 IPv4-mapped 十六进制形式：保守拦截（真实 CDN 不会用 mapped 地址出流）
                    return True
            except ValueError:
                return True
    # 纯数字简写：0 / 127 等单段 → 等价 0.0.0.0 / 127.0.0.0 网段，一律拦截
    if h.isdigit():
        return True
    if "." in h and ":" not in h:
        # 仅当 host 是合法 IPv4 时才做内网判定；普通域名（如 example.com）放行，交给网络栈解析
        parts = h.split(".")
        ip = None
        if len(parts) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in parts):
            ip = [int(x) for x in parts]
        if ip is not None:
            if ip[0] == 127 or ip[0] == 0:
                return True
            if ip[0] == 10:
                return True
            if ip[0] == 172 and 16 <= ip[1] <= 31:
                return True
            if ip[0] == 192 and ip[1] == 168:
                return True
            if ip[0] == 169 and ip[1] == 254:
                return True
    return False


# 出站 URL 的【解析后 IP】SSRF 二次校验：_blocked_internal_url 按 host 字符串判定，
# 这里按 DNS 解析结果判定——域名解析到私网/环回/链路本地/保留地址（含 DNS rebinding
# 把公网域名解析到内网 IP 的绕过）一律拒绝。结果按 host 缓存 60s 防并发 DNS 风暴。
_dns_public_cache = {}


def _assert_public_http_url(u):
    """校验失败抛 RuntimeError（由调用方的 502 兜底转为 proxy fail 响应）。"""
    import socket
    import ipaddress
    pr = urlparse(u)
    if pr.scheme not in ("http", "https"):
        raise RuntimeError("blocked target: scheme %r not allowed" % pr.scheme)
    host = (pr.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise RuntimeError("blocked target: empty host")
    now = time.time()
    cached = _dns_public_cache.get(host)
    if cached is not None and now - cached[0] < 60:
        if not cached[1]:
            raise RuntimeError("blocked target: %s resolves to non-public address" % host)
        return
    ok = True
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or
                    ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                ok = False
                break
    except Exception:
        # 解析失败按不通过处理（真正的网络错误会在后续 urlopen 体现）
        ok = False
    _dns_public_cache[host] = (now, ok)
    if len(_dns_public_cache) > 500:
        _dns_public_cache.clear()
    if not ok:
        raise RuntimeError("blocked target: %s resolves to non-public address" % host)


def _relay_out_headers(ref):
    """单策略出站请求头（relay_stream/预取用）。ref="none" 时不带 Referer。"""
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if ref != "none":
        h["Referer"] = ref or "http://www.yoozb.live/"
    return h


def _relay_strategies(ref):
    """双策略出站请求头（relay_url 整包拉取用，按序回退）。
    无 Referer 模式先直连，其他请求先带清单来源 Referer。"""
    if ref == "none":
        return [
            {"User-Agent": UA, "Accept": "*/*"},
            {"User-Agent": UA, "Accept": "*/*", "Referer": "http://www.yoozb.live/"},
        ]
    referer = ref or "http://www.yoozb.live/"
    return [
        {"User-Agent": UA, "Accept": "*/*", "Referer": referer},
        {"User-Agent": UA, "Accept": "*/*"},
    ]


def _is_88z_player_embed_referer(ref):
    """88zhibo 的 m3u8.html 包装页是线路 CDN 校验用的真实 Referer。"""
    try:
        parsed = urlsplit(str(ref or ""))
        return (
            parsed.scheme in ("http", "https") and
            (parsed.hostname or "").lower() == "play.88player.top" and
            parsed.path.rstrip("/").lower().endswith("/m3u8.html")
        )
    except (TypeError, ValueError):
        return False


def _is_tiny_junk(u, ctype, size):
    """识别直播分片 CDN 的简短拒绝响应（HTTP 200，但正文只有极少字节）。
    合法小文件（EXT-X-KEY 的 AES 密钥等）不带 .ts 后缀且非 mp2t，不受影响。"""
    if size >= 64:
        return False
    if u.split("?", 1)[0].lower().endswith(".ts"):
        return True
    return "mp2t" in (ctype or "").lower()


class _NoRedirectHandler(urlreq.HTTPRedirectHandler):
    """urlreq 分支禁用自动重定向（改由 _open_upstream 手动逐跳跟随 + 校验）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_direct_opener = urlreq.build_opener(urlreq.ProxyHandler({}))
_no_redirect_opener = urlreq.build_opener(urlreq.ProxyHandler({}), _NoRedirectHandler())
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _ascii_http_url(url):
    """Percent-encode Unicode URL components before issuing live relay requests."""
    parts = urlsplit(str(url or ""))
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    from urllib.parse import urlunsplit
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _open_upstream(u, headers, read_timeout, preload=True, connect_timeout=5):
    """打开上游 GET 连接，返回 (resp, final_url)。

    手动跟随最多 3 次重定向并保留签名 query，防止 urllib3 自动重定向丢失参数：
    curl -L 一跳即 200）。此处 redirect=False + 手动 ≤3 跳跟随：
    - 每跳目标经 _blocked_internal_url 校验（重定向到内网地址直接拒绝，SSRF）；
    - 301/302/303 保持 GET+原头（HLS 清单跳转无 POST 场景）；
    - 返回的 resp 与原两分支同型（urllib3: preload 后 .data 可用；
      urlreq: .read() 取体），status 属性两分支一致。"""
    # 来源 URL 可能在 path 或 query 中包含中文，先做 UTF-8 percent-encoding；每次拼好重定向目标后
    # 也规范化，避免 relay 探测成功但实际播放在 urllib 请求构造时失败。
    cur, hops = _ascii_http_url(u), 0
    while True:
        if HAS_URLLIB3:
            resp = _http.request(
                "GET", cur, headers=headers,
                timeout=urllib3.Timeout(connect=connect_timeout, read=read_timeout),
                preload_content=preload, redirect=False,
            )
            if resp.status in _REDIRECT_CODES:
                loc = resp.headers.get("Location") or ""
                resp.release_conn()
            else:
                return resp, cur
        else:
            req = urlreq.Request(cur, headers=headers)
            try:
                resp = _no_redirect_opener.open(req, timeout=read_timeout)
                return resp, cur
            except urlreq.HTTPError as e:
                if e.code not in _REDIRECT_CODES:
                    raise
                loc = e.headers.get("Location") or ""
                e.close()
        if not loc:
            raise RuntimeError("redirect without Location: %s" % cur)
        cur = _ascii_http_url(urljoin(cur, loc))
        hops += 1
        if hops > 3 or _blocked_internal_url(cur):
            raise RuntimeError("blocked redirect chain: %s -> %s" % (u, cur))


def _probe_alive(u, ref=None, per_try=3):
    """流地址交付前活性探测：任一 Referer 策略在 per_try 秒内拿到 2xx/3xx + 首字节
    → True；全部策略失败（连接/读超时、4xx/5xx、空响应）→ False。

    v1.32（2026-09-19）：奥克兰FC 友谊赛 plu 线 lmpull.va02.com 是「挂起型死地址」
    ——TCP 通、裸 GET 根路径秒回 404、一切 /sport/* 请求挂起 ~16s 后断连（本机
    直连/Clash 代理/腾讯云 SCF 三路径一致，假签名与不存在流 id 行为相同，即边缘
    在线、回源无推流）。relay_url 默认 12s×2 策略的探测要白等 24s+，故探测专用
    短超时（connect/read 均 per_try，双策略最坏 6s）。只读首 4KB：「direct」型
    大文件 URL 不会整段下载；空响应换策略再试，两策略都空也判死。"""
    if not u or not u.startswith(("http://", "https://")):
        return False
    for headers in _relay_strategies(ref):
        resp = None
        try:
            resp, _final = _open_upstream(u, headers, read_timeout=per_try,
                                          preload=False, connect_timeout=per_try)
            if not (200 <= resp.status < 400):
                continue
            if HAS_URLLIB3:
                chunk = resp.read(4096, decode_content=False)
            else:
                chunk = resp.read(4096)
            if chunk:
                return True
        except Exception:
            continue
        finally:
            if resp is not None:
                try:
                    if HAS_URLLIB3:
                        resp.release_conn()
                    else:
                        resp.close()
                except Exception:
                    pass
    return False


def _88z_probe_fetch(url, ref, max_bytes, valid_body, per_try=2):
    """按播放器的 Referer 策略读取有限响应；每次跳转仍经过 SSRF 检查。"""
    if not url or not url.startswith(("http://", "https://")) or _blocked_internal_url(url):
        return None
    for headers in _relay_strategies(ref):
        resp = None
        try:
            resp, final_url = _open_upstream(
                url, headers, read_timeout=per_try, preload=False, connect_timeout=per_try,
            )
            status = getattr(resp, "status", None)
            if status is None and hasattr(resp, "getcode"):
                status = resp.getcode()
            if not status or not 200 <= status < 400:
                continue
            if HAS_URLLIB3:
                body = resp.read(max_bytes, decode_content=True)
            else:
                body = resp.read(max_bytes)
            if body and valid_body(body, getattr(resp, "headers", {})):
                return body, final_url
        except Exception:
            continue
        finally:
            if resp is not None:
                try:
                    if HAS_URLLIB3:
                        resp.release_conn()
                    else:
                        resp.close()
                except Exception:
                    pass
    return None


def _88z_hls_targets(text):
    """Return a master playlist's variants or a media playlist's recent segments."""
    lines = [line.strip() for line in text.splitlines()]
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        variants = []
        for index, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            for candidate in lines[index + 1:]:
                if candidate and not candidate.startswith("#"):
                    variants.append(candidate)
                    break
        return "master", variants[:3], []

    segments = []
    waiting_for_segment = False
    key_uris = []
    for line in lines:
        if line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line.upper():
            match = re.search(r'(?:^|,)URI=(?:"([^"]+)"|([^,]+))', line, re.I)
            if match:
                key_uris.append(match.group(1) or match.group(2).strip())
        if line.startswith("#EXTINF:"):
            waiting_for_segment = True
        elif line.startswith("#EXT-X-PART:") or line.startswith("#EXT-X-PRELOAD-HINT:"):
            match = re.search(r'(?:^|,)URI=(?:"([^"]+)"|([^,]+))', line, re.I)
            if match:
                segments.append(match.group(1) or match.group(2).strip())
        elif line and not line.startswith("#") and waiting_for_segment:
            segments.append(line)
            waiting_for_segment = False
    # A live playlist's newest segment is the best approximation of what a client
    # will request at startup. Keep one fallback in case its URL has just expired.
    return "media", list(reversed(segments[-2:])), list(dict.fromkeys(key_uris))


def _probe_88z_hls_playable(url, ref=None):
    """Require a valid HLS manifest, a recent media segment, and any AES key.

    This is a bounded upstream check, not a codec decoder. Actual decoder playback
    is covered by the client verification path; this prevents dead segments and
    HTML error pages from appearing as selectable sources.
    """
    visited = set()

    def probe(current, depth):
        if depth >= 3:
            return False
        if current in visited:
            return False
        visited.add(current)
        fetched = _88z_probe_fetch(
            current, ref, 96 * 1024,
            lambda body, _headers: body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U"),
        )
        if not fetched:
            return False
        body, final_url = fetched
        text = body.decode("utf-8", errors="replace").lstrip("\ufeff \t\r\n")
        kind, targets, key_uris = _88z_hls_targets(text)
        if kind == "master":
            for target in targets:
                next_url = urljoin(final_url, target)
                if not _blocked_internal_url(next_url) and probe(next_url, depth + 1):
                    return True
            return False
        if not targets:
            return False

        if key_uris:
            key_url = urljoin(final_url, key_uris[-1])
            key = _88z_probe_fetch(key_url, ref, 64, lambda data, _headers: len(data) >= 16)
            if not key:
                return False

        for target in targets:
            segment_url = urljoin(final_url, target)
            segment = _88z_probe_fetch(
                segment_url, ref, 4096,
                lambda data, headers: (
                    len(data) >= 64
                    and "text/html" not in str(headers.get("Content-Type", "")).lower()
                    and not data.lstrip().lower().startswith((b"<!doctype html", b"<html", b"{\"error"))
                ),
            )
            if segment:
                return True
        return False

    return probe(url, 0)


def relay_url(u, ref=None):
    """
    HLS 中继：经本服务转发 m3u8/分片/直链，规避跨域与防盗链。
    播放列表中的每个 URI 被重写为 /api/relay?u=...&ref=...（绝对路径）。

    防盗链策略（实测 2026-08-05，与旧文档相反）：
    - 腾讯云 VCLOUD CDN（letaocm/esportlive 等）：**必须带 Referer** 才会快速响应；
      无 Referer 时 HTTPS 握手后挂起到超时（8s+）。
    - 带 Referer 时未开播 -> 403（快速，0.3s）；开播 -> 200。
    因此 **先带 Referer**（快），403 即视为未开播直接返回（不再无 Referer 重试）；
    仅当带 Referer 也连接/超时失败时，才回退无 Referer 尝试（极少数 CDN 拒绝 Referer）。
    返回 (status, content_type, body_bytes, err_text, is_m3u8)。
    """
    if not u or not (u.startswith("http://") or u.startswith("https://")):
        return 400, "text/plain; charset=utf-8", "bad url".encode("utf-8"), None, False
    if _blocked_internal_url(u):
        return 403, "text/plain; charset=utf-8", "blocked".encode("utf-8"), None, False

    # 分片短缓存（仅 .ts 等非 m3u8 内容）
    is_manifest = "m3u8" in u.lower()
    cache_key = _relay_cache_key(u)
    if not is_manifest:
        with _relay_lock:
            hit = RELAY_CACHE.get(cache_key)
            if hit and time.time() - hit[0] < RELAY_CACHE_TTL:
                return hit[1], hit[2], hit[3], None, hit[4]

    # 出站策略见 _relay_strategies。
    strategies = _relay_strategies(ref)
    last_err = None
    data = ctype = final = None
    for headers in strategies:
        resp = None
        fully_read = False
        try:
            resp, final = _open_upstream(u, headers, read_timeout=12, preload=True)
            status = resp.status
            if status >= 400:
                # 403 = 未开播（腾讯云 VCLOUD 约定），无需再用另一种 Referer 策略重试，
                # 直接返回 502 让前端进入「信号尚未开播」自动重试。
                raise RuntimeError("HTTP %d from %s" % (status, u))
            data = resp.data if HAS_URLLIB3 else resp.read()
            fully_read = True
            ctype = resp.headers.get("Content-Type", "") or ""
            break  # 成功
        except Exception as e:
            last_err = e
            # v1.22（2026-09-05 需求⑦）：403 不再立即终止——实测 plu 线 CDN
            #（bf.njscwh.com）对「带 yoozb Referer」反而 403、无 Referer 秒开；
            # 旧逻辑「403 即判未开播直接 502」把这类可用源整线打死（原站浏览器
            # 可看、App 全挂的根因）。落到下一策略（无 Referer）再试；两策略
            # 都 403 才按未开播 502 上抛（多付一次 0.1~0.3s 快速请求）。
            continue
        finally:
            if resp is not None:
                # urllib3 preload=True has already consumed even an error body;
                # return it to the pool. The stdlib branch is not preloaded, so
                # an unsuccessful response must be closed without being drained.
                try:
                    _release_relay_response(
                        resp, fully_read=(fully_read or HAS_URLLIB3))
                except Exception:
                    pass
    if data is None:
        err = ("relay error: %s" % str(last_err)[:200]).encode("utf-8")
        return 502, "text/plain; charset=utf-8", err, str(last_err), False

    is_m3u8 = ("mpegurl" in ctype.lower()) or u.lower().endswith(".m3u8") \
        or (final or "").lower().endswith(".m3u8")
    if is_m3u8:
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            text = ""

        # 使用重定向后的最终地址拼接相对分片，保留上游签名路径与端口。
        base = final or u
        vlc_session = _VLC_HLS_SESSION.get()
        try:
            text = vlc_hls.normalize(text, base, vlc_session)
        except (ValueError, OverflowError):
            return 502, "text/plain; charset=utf-8", b"invalid live media playlist", "invalid live media playlist", False

        # Segment requests and their background prefetches must use the same
        # anti-hotlink Referer. A media playlist's ordinary parent ref points
        # at its master playlist, while its segment requests use this playlist
        # URL itself. 88zhibo embeds retain their validated embed-page ref, and
        # the explicit "none" mode remains Referer-free.
        segment_ref = (ref if ref == "none" or _is_88z_player_embed_referer(ref)
                       else u)

        def rw(uri):
            full = urljoin(base, uri)
            q = quote(full, safe="").replace("+", "%2B")
            # 88zhibo embed 保留已验证的页面 Referer；普通线路以当前清单 URL
            # 作为后续分片/子清单 Referer。
            rq = ("none" if segment_ref == "none" else
                  quote(segment_ref, safe="").replace("+", "%2B"))
            # v1.21 相对形式「relay?...」：以清单 URL（…/api/relay?u=…）所在目录解析
            # → 同目录 /api/relay；绝对路径 /api/relay 在「网关子路径部署」（网页版
            # https://…/web/）下会被浏览器解析到网关根而 404。设备端（根部署）
            # 解析结果与原绝对路径完全一致（ExoPlayer/hls.js 均按规范相对解析）。
            vlc_query = "&vlc_session=" + vlc_session if vlc_session else ""
            return "relay?u=" + q + "&ref=" + rq + vlc_query

        out = []
        raw_segs = []
        has_extinf = False
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                out.append(ln)
                continue
            if s.startswith("#"):
                if s.startswith("#EXTINF"):
                    has_extinf = True  # 媒体清单标记（master 无 EXTINF，其子清单行不当作分片）
                if s.startswith("#EXT-X-") and 'URI="' in s:
                    s = re.sub(r'URI="([^"]+)"',
                               lambda mo: 'URI="' + rw(mo.group(1)) + '"', s)
                out.append(s)
            else:
                raw_segs.append(urljoin(base, s))  # 原始上游分片 URL（预取定位用）
                out.append(rw(s))
        data = "\n".join(out).encode("utf-8")
        ctype = "application/vnd.apple.mpegurl"
        # 媒体清单 → 记录分片顺序表供预取，并预热头 2 片加速首帧
        if has_extinf and raw_segs:
            _remember_manifest(u, raw_segs, segment_ref)
    else:
        # 防盗链拦截形态（200 + "OK\n"）：按 502 上抛，播放器快速换线路/直连重试，
        # 而不是把 3 字节当分片喂给解复用器（解析异常 + 反复重试 = 卡顿形态）
        if _is_tiny_junk(u, ctype, len(data)):
            return 502, "text/plain; charset=utf-8", (
                "relay rejected: tiny body (%dB)" % len(data)).encode("utf-8"), \
                "tiny junk body", False
        # 非 m3u8（.ts 分片/直链）：仅小文件（≤8MB）写入短缓存，
        # 防 hls.js 重试风暴重复请求 CDN；大文件（直链视频）走流式转发不缓存，
        # 否则内存缓存会吞掉整部影片。
        with _relay_lock:
            if status == 200 and len(data) <= 8 * 1024 * 1024:
                RELAY_CACHE[cache_key] = (time.time(), 200, ctype or "application/octet-stream", data, is_m3u8)
                _trim_relay()
    return 200, ctype or "application/octet-stream", data, None, is_m3u8


def _release_relay_response(resp, fully_read=False):
    """Discard unread bodies before returning a connection to its pool."""
    if resp is None:
        return
    try:
        if not HAS_URLLIB3 or not fully_read:
            resp.close()
    finally:
        if HAS_URLLIB3:
            resp.release_conn()


def _read_relay_chunk(resp, size):
    # read(size) waits for the entire block on slow CDN responses. A seek loses
    # the warm buffer, so forward available bytes instead of waiting for 128 KiB.
    return getattr(resp, "read1", resp.read)(size)


def _supports_byte_ranges(resp):
    value = (resp.headers.get("Accept-Ranges") or "").strip()
    return any(token.strip().lower() == "bytes" for token in value.split(","))


def _strong_etag(resp):
    value = (resp.headers.get("ETag") or "").strip()
    if (len(value) >= 2 and value.startswith('"') and value.endswith('"')
            and not value[:2].lower() == "w/" and '"' not in value[1:-1]):
        return value
    return ""


def _identity_nonchunked(resp):
    transfer_encoding = (resp.headers.get("Transfer-Encoding") or "").strip()
    content_encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
    return not transfer_encoding and content_encoding in ("", "identity")


def _media_content_type(value):
    media_type = (value or "").split(";", 1)[0].strip().lower()
    return (
        media_type.startswith(("video/", "audio/"))
        or media_type in {
            "application/octet-stream",
            "application/mp2t",
            "application/x-mpegts",
        }
    )


def _content_range(value):
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", (value or "").strip(), re.I)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _incomplete_read_partial(exc):
    """Return bytes carried by IncompleteRead, including wrapped transport errors."""
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, http.client.IncompleteRead):
            return current.partial if isinstance(current.partial, bytes) else b""
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)
        pending.extend(arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException))
    return b""


def relay_stream(u, ref=None, rng=None, chunk=128 * 1024):
    """
    媒体流式中继（非 m3u8）：边收边发，避免整包缓冲（直播/大文件首字节更快，低端机播放更跟手）。

    与 relay_url 的分工：
    - relay_url：m3u8 播放列表（需整包重写分片/密钥 URL），返回完整 bytes。
    - relay_stream：.ts 分片 / 直链 mp4/flv 等媒体。**立即打开上游连接**，
      返回真实的 status/Content-Type/extra 头（Range 请求的上游 206/Content-Range 一并回传），
      gen() 只负责把剩余字节流式写给客户端。
      URL 不带 .m3u8 但上游实际返回 mpegurl（清单）→ 整包重写（复用 relay_url）。

    客户端 Range 直接透传（seek/拖动进度条需要）。
    """
    if not u or not (u.startswith("http://") or u.startswith("https://")):
        return 400, "text/plain; charset=utf-8", {}, iter([b"bad url"]), False
    if _blocked_internal_url(u):
        return 403, "text/plain; charset=utf-8", {}, iter([b"blocked"]), False

    # v1.22（2026-09-05 需求⑦）：单策略失败/被 403 → 换另一 Referer 策略重开一次。
    # bf.njscwh.com 等 CDN 带 yoozb Referer 403、无 Referer 200；流式路径旧版
    # 对所有分片使用与清单相同的 Referer 策略顺序。
    if ref != "none":
        header_variants = _relay_strategies(ref)
    else:
        header_variants = [_relay_out_headers("none"), _relay_out_headers(None)]
    state = {}
    resp = None
    last_err = None
    for headers in header_variants:
        if rng:
            headers = dict(headers, Range=rng)
        try:
            resp, _final = _open_upstream(u, headers, read_timeout=30, preload=False)
            if resp.status >= 400:
                _release_relay_response(resp)
                last_err = "HTTP %d from %s" % (resp.status, u)
                resp = None
                continue   # 换下一 Referer 策略再试
            state["request_headers"] = dict(headers)
            break
        except Exception as e:
            last_err = str(e)[:200]
            resp = None
            continue
    if resp is None:
        return 502, "text/plain; charset=utf-8", {}, iter(
            [(str(last_err or "relay stream open failed")).encode("utf-8")]), False
    extra = {}
    try:
        ctype = resp.headers.get("Content-Type", "") or ""
        first = _read_relay_chunk(resp, 64 * 1024)
        # Keep the tiny-junk check about a body, not a short TCP packet.
        while 0 < len(first) < 64:
            more = _read_relay_chunk(resp, 64 - len(first))
            if not more:
                break
            first += more
        state["resp"], state["first"] = resp, first
        for h in ("Content-Range", "Accept-Ranges"):
            v = resp.headers.get(h)
            if v:
                extra[h] = v
        status, detect = resp.status, ctype
    except Exception as e:
        _release_relay_response(resp)
        return 502, "text/plain; charset=utf-8", {}, iter(
            [("relay stream error: %s" % str(e)[:200]).encode("utf-8")]), False

    if "mpegurl" in detect.lower():
        # 罕见：无 .m3u8 后缀的清单 → 关掉流，整包重写（manifest 体积小）
        try:
            _release_relay_response(state["resp"])
        except Exception:
            pass
        status, ctype, body, _e, _m = relay_url(u, ref)
        return status, ctype, {}, iter([body]), False

    # 流式转发：已知 Content-Length（含 Range 段长）→ 带 Content-Length 边收边发，
    # 首字节时间 = 上游首块到达（原先 ≤8MB 分片整包读完才发，TTFB = 整片下载时间，
    # 慢源上吃掉 hls.js 超时预算引发 abort-重试循环）。无 Range 的小分片（≤8MB）
    # 边发边累积，完整收完写入短缓存（保留 hls.js 重试风暴不打 CDN 的防护）。
    clen = ""
    try:
        clen = (state["resp"].headers.get("Content-Length") or "").strip()
    except Exception:
        pass
    known_len = int(clen) if clen.isdigit() else -1
    # 防盗链拦截形态（200 + "OK\n"，v1.14）：掐掉上游连接按 502 上抛——
    # 3 字节当分片喂给解复用器只会换来解析异常+重试循环（观感=卡顿）
    if (known_len == -1 or known_len < 64) and _is_tiny_junk(u, ctype, len(first)):
        try:
            _release_relay_response(state["resp"])
        except Exception:
            pass
        return 502, "text/plain; charset=utf-8", {}, iter(
            [("relay rejected: tiny body (%dB)" % len(first)).encode("utf-8")]), False
    buf = bytearray() if (not rng and known_len >= 0 and known_len <= 8 * 1024 * 1024) else None
    upstream_te = ""
    try:
        upstream_te = (state["resp"].headers.get("Transfer-Encoding") or "").strip().lower()
    except Exception:
        pass
    resume_etag = _strong_etag(state["resp"])
    resume_enabled = (
        bool(first)
        and not rng
        and status == 200
        and 0 < known_len <= 8 * 1024 * 1024
        and not upstream_te
        and _identity_nonchunked(state["resp"])
        and _supports_byte_ranges(state["resp"])
        and bool(resume_etag)
        and _media_content_type(ctype)
    )
    resume_headers = dict(state.get("request_headers") or {})
    strict_known_body = (
        not rng and status == 200 and not upstream_te and known_len >= 0
    )

    def gen():
        resp = state["resp"]
        first = state["first"]
        fully_read = False
        sent = 0
        resume_attempts = 0
        last_read_error = None

        def track(piece):
            nonlocal sent
            if not piece:
                return piece
            sent += len(piece)
            if buf is not None and len(buf) < known_len:
                buf.extend(piece[:known_len - len(buf)])
            return piece

        def open_resume(start):
            headers = dict(resume_headers)
            headers["Range"] = "bytes=%d-" % start
            headers["If-Range"] = resume_etag
            resumed, _final = _open_upstream(
                u, headers, read_timeout=5, preload=False)
            try:
                if resumed.status != 206:
                    raise RuntimeError(
                        "relay resume expected 206, got %s" % resumed.status)
                if not _identity_nonchunked(resumed):
                    raise RuntimeError("relay resume was not identity/non-chunked")
                if _strong_etag(resumed) != resume_etag:
                    raise RuntimeError("relay resume ETag changed")
                parsed = _content_range(resumed.headers.get("Content-Range"))
                if parsed is None:
                    raise RuntimeError("relay resume missing Content-Range")
                resume_start, resume_end, total = parsed
                if (resume_start != start or total != known_len
                        or resume_end < resume_start or resume_end >= total):
                    raise RuntimeError("relay resume Content-Range mismatch")
                resume_len = (resumed.headers.get("Content-Length") or "").strip()
                if (not resume_len.isdigit()
                        or int(resume_len) != resume_end - resume_start + 1):
                    raise RuntimeError("relay resume Content-Length mismatch")
            except Exception:
                _release_relay_response(resumed)
                raise
            return resumed

        def resume_from(resp_after_error):
            nonlocal resp, resume_attempts
            if resume_attempts >= 2:
                return None
            resume_attempts += 1
            resp = None
            _release_relay_response(resp_after_error)
            resp = open_resume(sent)
            return resp

        def raise_short():
            if last_read_error is not None:
                raise RuntimeError("relay stream incomplete after resume") from last_read_error
            raise http.client.IncompleteRead(b"", max(0, known_len - sent))

        # 上游读失败必须向上抛（不得吞）：吞掉会导致响应 body 短于已发的
        # Content-Length 但连接干净收尾 → 客户端把截断分片当完整数据喂解码器
        # → hls.js worker 卡死（视频冻结、无报错、不再请求分片，实测复现）。
        # do_GET 捕获后按短包处理（掐断连接 → 客户端立刻网络错误 → hls.js 重试）
        try:
            # Prime the finally block before handing ownership to the handler:
            # a seek may disconnect even before response headers can be written.
            yield None
            if first:
                yield track(first)
            while True:
                try:
                    b = _read_relay_chunk(resp, chunk)
                except Exception as exc:
                    last_read_error = exc
                    partial = _incomplete_read_partial(exc)
                    if partial:
                        remaining = known_len - sent if known_len >= 0 else len(partial)
                        if len(partial) > remaining:
                            raise RuntimeError("relay stream exceeded Content-Length") from exc
                        yield track(partial)
                    if known_len >= 0 and sent >= known_len:
                        fully_read = True
                        break
                    if resume_enabled and resume_from(resp) is not None:
                        continue
                    if strict_known_body:
                        raise_short()
                    raise
                if not b:
                    if known_len >= 0 and sent >= known_len:
                        fully_read = True
                        break
                    if resume_enabled and resume_from(resp) is not None:
                        continue
                    if strict_known_body:
                        raise_short()
                    fully_read = True
                    break
                yield track(b)
        finally:
            try:
                _release_relay_response(resp, fully_read=fully_read)
            except Exception:
                pass
            # 仅完整收完的分片入缓存（中断/部分数据不得入缓存，防毒化）
            if (fully_read and status == 200 and not extra.get("Content-Range")
                    and buf is not None and known_len >= 0 and len(buf) == known_len):
                with _relay_lock:
                    RELAY_CACHE[_relay_cache_key(u)] = (time.time(), 200, ctype or "application/octet-stream", bytes(buf), False)
                    _trim_relay()

    # extra（Content-Range/Accept-Ranges）已在打开上游连接时构建
    if rng:
        # Range/206：保留上游段长（seek 语义需要 Content-Range + Content-Length）
        if known_len >= 0:
            extra["Content-Length"] = str(known_len)
    elif "chunked" in upstream_te:
        # 上游自身 chunked（腾讯系直播 CDN 在中间盒/代理链路上的形态）：
        # urllib3 按 chunked 解码读取、无视其同时声明的 Content-Length——该 CL
        # 可能虚高（实测稳定短交付 ~10 字节后干净收尾）。旧版透传此 CL，客户端
        # （OkHttp/fetch）body 结束时判缺字节硬报 unexpected end of stream →
        # ERROR_CODE_IO_NETWORK_CONNECTION_FAILED；重试重抓同一上游必复发
        # （同线重试×3 → 错误态 30s 自动重试 → fresh 重解析仍同一 CDN 同一形态）
        # = 球迹直播「看一会就看不了、点重试/换源都没用」根因（2026-09-19 实测）。
        # 改以 chunked 下发行：0-chunk 收尾即「完整」，到达多少算多少——TS 尾部
        # 残缺不足一个 188B 包，解复用器直接丢弃，无感知；上游真异常（连接重置/
        # 超时/读错误）仍由生成器上抛 → do_GET 不写终止块 → 客户端网络错误 →
        # 既有重试链（语义与旧短包掐断一致，但虚报 CL 形态不再误伤）。
        extra["Transfer-Encoding"] = "chunked"
    elif known_len >= 0:
        # 有明确 Content-Length 的上游：透传长度，保持 HTTP 分帧一致；
        # 真短读由 urllib3 上抛 → do_GET 短包掐断 → 客户端重试
        extra["Content-Length"] = str(known_len)
    stream = gen()
    next(stream)  # Consume the ownership marker; only media bytes reach clients.
    return status, ctype, extra, stream, False


# ===== VLC 兜底专用：直播 HLS → 连续 MPEG-TS 直出（/api/relayts/<matchId>?src=bb） =====
# 背景（2026-08-30 v1.8 实测）：libVLC 3.x 的 adaptive(HLS) 模块对上游时间戳跳变存在
# assert 崩溃缺陷（FakeESOut::applyTimestampContinuity 断言失败 → SIGABRT 杀死整个 app）。
# 把 HLS 在服务端拼接成无终止原生 TS 流，VLC 走久经广播考验的 ts demuxer 完全绕开
# adaptive 模块；同时签名轮换（~分钟级）由本生成器内部 fresh 重解析自愈，播放器
# 全程不换地址（VLC 无热切换，换地址=重开黑屏）。
# 注意：全部抓取复用 relay_url/relay_stream（既有 SSRF 防线与分片缓存），不新增抓取路径。
_RELAYTS_U_RE = re.compile(r'^/api/relay\?u=([^&\s]+)')


def _relayts_playlist_items(text):
    """解析 relay_url 改写后的媒体清单 → [(分片绝对URL, EXTINF时长秒)] 有序列表。
    滑动窗口清单长度恒定，消费方必须按「分片 URL」定位游标（按索引会永远取空）。"""
    out = []
    dur = 5.0
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF:"):
            try:
                dur = float(s[8:].split(",")[0].strip() or 5.0)
            except Exception:
                dur = 5.0
            continue
        if s.startswith("#"):
            continue
        mm = _RELAYTS_U_RE.match(s)
        if mm:
            try:
                out.append((unquote(mm.group(1)), max(dur, 0.5)))
            except Exception:
                pass
            dur = 5.0
    return out


def relayts_generator(match_id, src):
    """生成器：yield TS 字节块。断流自动 fresh 重解析；45s 无新分片重解析防签名静默失效；
    直链（非 m3u8）经 relay_stream 透传。客户端断开 → GeneratorExit 结束（每连接独立游标）。"""
    url = None
    referer = SOURCE_URL
    try:
        resolved, _cached = resolve_88zhibo_stream(match_id, src, fresh=True)
        url = resolved.get("url")
        referer = resolved.get("referer") or SOURCE_URL
    except Exception:
        pass
    if not url:
        return

    def _refresh_url():
        """fresh 重解析当前源签名；变化则返回新地址（调用方重置游标追新边缘）。"""
        try:
            resolved, _cached = resolve_88zhibo_stream(match_id, src, fresh=True)
            nu = resolved.get("url")
            if nu and nu != url:
                return nu, resolved.get("referer") or SOURCE_URL
        except Exception:
            pass
        return None

    # 非清单直链（mp4/flv 等少见形态）：流式透传
    if "m3u8" not in url.lower():
        try:
            status, ctype, extra, it, _m = relay_stream(url, referer)
            if status != 200:
                return
            for b in it:
                yield b
        except GeneratorExit:
            raise
        except Exception:
            return
        return
    watermark = None      # 最后已写出的分片 URL（滑动窗口清单长度恒定，按索引记水位会永远取空）
    idle = 0.0
    backoff = 1.0
    # 供给节流（2026-08-30 实测教训：不限速时追边突发 ~20Mbps，VLC 缓冲溢出停读 →
    # 生成器跑过上游删除窗撞 404 → 跳边时间轴震荡 → 卡顿判定循环重开）：
    # 供给领先挂钟最多 LEAD 秒（≈ VLC network-caching 1.5s + 余量），永远实时节奏
    LEAD = 10.0
    t0 = time.time()
    served_media = 0.0
    # 实测（2026-08-30）：football.online-video.info 的每个 auth_key 映射独立 CDN
    # 会话，存在「空窗死会话」（HTTP 200 但清单永远 0 分片）——连续几轮空窗必须
    # fresh 换 key 重挂活会话；预算上限防把上游解析打成限流（v1.6 的 429 教训）
    empty_polls = 0
    empty_refresh_budget = 8
    while True:
        try:
            st, _ct, body, _err, _m = relay_url(url, referer)
            if st != 200:
                raise IOError("playlist HTTP %d" % st)
            text = body.decode("utf-8", errors="replace")
        except GeneratorExit:
            raise
        except Exception:
            # 清单抓失败（签名轮换/抖动）→ fresh 重解析换地址，从新直播边缘续播
            refreshed = _refresh_url()
            if refreshed:
                url, referer = refreshed
                watermark = None
            time.sleep(min(backoff, 5.0))
            backoff = min(backoff * 2, 5.0)
            continue
        backoff = 1.0
        items = _relayts_playlist_items(text)
        if watermark is None:
            # 首次：从直播边缘起（留 2 片预卷），不从窗口头追历史
            todo = items[-2:] if len(items) >= 2 else items[:]
        else:
            idx = next((i for i, (sg, _d) in enumerate(items) if sg == watermark), -1)
            # 找不到 = 窗口已滚过（消费太慢）→ 弃落后内容追新边缘
            todo = items[idx + 1:] if idx >= 0 else (items[-2:] if len(items) >= 2 else items[:])
        if not todo:
            idle += 1.5
            empty_polls += 1
            need_refresh = idle >= 45.0 or (empty_polls >= 3 and empty_refresh_budget > 0)
            if need_refresh:
                idle = 0.0
                empty_polls = 0
                if empty_refresh_budget > 0:
                    empty_refresh_budget -= 1
                refreshed = _refresh_url()
                if refreshed:
                    url, referer = refreshed
                    watermark = None
            else:
                time.sleep(1.5)
            continue
        idle = 0.0
        empty_polls = 0
        for sg, sdur in todo:
            try:
                st2, _ct2, seg, _e2, _m2 = relay_url(sg, referer)
                if st2 == 200 and seg:
                    yield seg
                    watermark = sg
                    served_media += sdur
                    # 实时节流：领先挂钟超过 LEAD 就等一等（不吃满 VLC 缓冲）
                    while served_media - (time.time() - t0) > LEAD:
                        time.sleep(0.4)
            except GeneratorExit:
                raise
            except Exception:
                # 单片抓失败：跳过该片继续（直播少量缺片可接受，卡死不可接受）
                continue


# ===== 球队图标查询（TheSportsDB + 本地缓存） =====
# 本地队标缓存（运行时内存缓存 + 持久化 JSON，避免重启丢失）
TEAM_ICON_CACHE_FILE = Path(__file__).parent / "team_icon_cache.json"
TEAM_ICON_CACHE = {}
_tdb_last_call = 0
# football-logos.cc autocomplete index (4,800+ entries, fetched once per process).
FOOTBALL_LOGOS_INDEX = None
# football-data SVG 队徽内存缓存（本地代理返回，避免浏览器直连被限流）
SVG_ICON_CACHE = {}
SVG_ICON_LOCK = threading.Lock()
SVG_ICON_DIR = Path(__file__).parent / "svg_icon_cache"

# 每个 URL 一把抓取锁：并发请求同一队标时只让第一个真正打上游，其余等它的结果。
# 缺它时，足球页一次滚出十几个队 → 十几个 urlopen 同时打 crests.football-data.org
# → 上游限流，一部分失败；而失败既不落盘也不记失败缓存，下个请求照样再失败一遍
# （实测并发 16 个队标有 6 个返回 68 字节 1x1 占位图，串行则 0 失败）。
SVG_ICON_FETCH_LOCKS = {}
SVG_ICON_FETCH_LOCKS_GUARD = threading.Lock()
SVG_ICON_FETCH_ATTEMPTS = 3
SVG_ICON_FETCH_BACKOFF = 0.35
# 单次抓取超时。实测同一个 SVG 本机 0.9s、设备（移动网络到境外 CDN）要 5~6s；
# 原先写死 6s 时，真机 48 队并发仍有 3 个（利物浦/布伦特福德/巴黎圣日耳曼）
# 三连超时后返回 68 字节占位图。放宽到 20s 让慢网也能拿到，代价只是失败路径等更久，
# 而成功路径不受影响（真机绝大多数队标 1~2s 就回）。
SVG_ICON_FETCH_TIMEOUT = 20
# 对上游的总并发闸门。足球页一次滚出十几个队标就会同时打 crests.football-data.org；
# 48 个并发时设备侧带宽被打满，连把单次超时放宽到 20s 也救不回来——大文件
# （利物浦 299KB、巴黎圣日耳曼 274KB）会三连超时，而它们**单独**请求只要 3.1s。
# 加超时治不了并发挤占，必须限流：同一时刻最多 4 个请求打上游，其余排队。
SVG_ICON_FETCH_SEMAPHORE = threading.Semaphore(4)


def _svg_fetch_lock(url):
    """取（并按需创建）该 URL 的抓取锁。"""
    with SVG_ICON_FETCH_LOCKS_GUARD:
        lock = SVG_ICON_FETCH_LOCKS.get(url)
        if lock is None:
            lock = threading.Lock()
            SVG_ICON_FETCH_LOCKS[url] = lock
        return lock


TRANS_ICON_DIR = Path(__file__).parent / "transparent_icon_cache"


def fetch_icon_bytes(url):
    """抓取外部队标 PNG 字节（内存 + 磁盘缓存，磁盘长期复用）。失败返回 None。"""
    import hashlib
    fname = hashlib.md5(url.encode("utf-8")).hexdigest() + ".png"
    fpath = TRANS_ICON_DIR / fname
    if fpath.exists():
        try:
            return fpath.read_bytes()
        except Exception:
            pass
    try:
        req = urlreq.Request(url, headers={"User-Agent": UA})
        with urlreq.urlopen(req, timeout=6) as resp:
            data = resp.read()
        if not data:
            return None
        try:
            TRANS_ICON_DIR.mkdir(exist_ok=True)
            fpath.write_bytes(data)
        except Exception:
            pass
        return data
    except Exception:
        return None


def transparentize_png(data):
    """把方形队标 PNG/JPEG 的实色背景（边缘连通区域）透明化。
    纯透明/圆形徽章直接原样返回；处理后若透明占比 >96% 视为误删，回退原图。"""
    if not data:
        return data
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(data)).convert("RGBA")
        w, h = im.size
        if w < 8 or h < 8:
            return data
        px = im.load()
        opaque = [px[x, y][3] for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
        if any(a < 200 for a in opaque):
            return data  # 四角已有透明（圆形徽章等），无需处理
        anchor = tuple(sum(px[x, y][i] for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))) // 4
                       for i in range(3))
        tol = 70
        from collections import deque
        q = deque()
        visited = set()
        for x in range(w):
            q.append((x, 0)); q.append((x, h - 1))
        for y in range(h):
            q.append((0, y)); q.append((w - 1, y))
        gone = 0
        while q:
            x, y = q.popleft()
            if (x, y) in visited or not (0 <= x < w and 0 <= y < h):
                continue
            visited.add((x, y))
            p = px[x, y]
            if p[3] < 160:
                continue
            if abs(p[0] - anchor[0]) + abs(p[1] - anchor[1]) + abs(p[2] - anchor[2]) >= tol:
                continue
            px[x, y] = (p[0], p[1], p[2], 0)
            gone += 1
            q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
        # 安全阀：透明化过量说明把队徽本体误删了，回退原图
        if gone > w * h * 0.96:
            return data
        buf = _io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return data


def fetch_svg_icon(url):
    """抓取 football-data SVG 队徽内容（内存 + 磁盘缓存，磁盘长期复用）。
    返回 (bytes, ok)；失败返回 (None, False)。"""
    # 磁盘缓存：URL → 文件名（md5）
    import hashlib
    fname = hashlib.md5(url.encode("utf-8")).hexdigest() + ".svg"
    fpath = SVG_ICON_DIR / fname
    with SVG_ICON_LOCK:
        if url in SVG_ICON_CACHE:
            return SVG_ICON_CACHE[url], True
    if fpath.exists():
        try:
            data = fpath.read_bytes()
            with SVG_ICON_LOCK:
                SVG_ICON_CACHE[url] = data
            return data, True
        except Exception:
            pass

    # 慢路径要加锁：并发下只让一个请求打上游，其余等结果复用。
    # double-check 在锁内再查一次——等锁期间前一个线程可能已经写好缓存了。
    with _svg_fetch_lock(url):
        with SVG_ICON_LOCK:
            if url in SVG_ICON_CACHE:
                return SVG_ICON_CACHE[url], True
        if fpath.exists():
            try:
                data = fpath.read_bytes()
                with SVG_ICON_LOCK:
                    SVG_ICON_CACHE[url] = data
                return data, True
            except Exception:
                pass

        # 重试：上游 CDN 对突发并发会限流，单次 urlopen 失败率实测约 38%。
        # 退避后重试即可稳定拿到；全失败才返回 False 让调用方给占位图。
        last_error = None
        for attempt in range(SVG_ICON_FETCH_ATTEMPTS):
            if attempt:
                time.sleep(SVG_ICON_FETCH_BACKOFF * attempt)
            try:
                req = urlreq.Request(url, headers={"User-Agent": UA})
                with SVG_ICON_FETCH_SEMAPHORE:
                    with urlreq.urlopen(req, timeout=SVG_ICON_FETCH_TIMEOUT) as resp:
                        data = resp.read()
                if not data:
                    last_error = "empty body"
                    continue
                try:
                    SVG_ICON_DIR.mkdir(exist_ok=True)
                    fpath.write_bytes(data)
                except Exception:
                    pass
                with SVG_ICON_LOCK:
                    SVG_ICON_CACHE[url] = data
                    if len(SVG_ICON_CACHE) > 300:
                        for k in list(SVG_ICON_CACHE)[:150]:
                            SVG_ICON_CACHE.pop(k, None)
                return data, True
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error is not None:
            try:
                print("svg 队标抓取失败 %s: %s" % (url, str(last_error)[:120]))
            except Exception:
                pass
        return None, False


def load_team_icon_cache():
    """启动时加载本地队标缓存"""
    global TEAM_ICON_CACHE
    try:
        if TEAM_ICON_CACHE_FILE.exists():
            with open(TEAM_ICON_CACHE_FILE, "r", encoding="utf-8") as f:
                TEAM_ICON_CACHE = json.load(f)
    except Exception:
        TEAM_ICON_CACHE = {}


def save_team_icon_cache():
    """持久化队标缓存到 JSON（后台线程，不阻塞主流程）"""
    try:
        with open(TEAM_ICON_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(TEAM_ICON_CACHE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _football_logo_key(value):
    """Normalize display names so e.g. `Liverpool` matches `Liverpool FC`."""
    value = (value or "").replace("_", " ").lower()
    # v1.32（2026-09-19）：通用后缀剥离兼容「CJK+FC 连写」（奥克兰FC/FC东京/巴黎FC）。
    # 旧 \b 词界在 CJK 与拉丁字母相邻时不生效（\w 含 CJK）：「奥克兰fc」剥不掉 fc，
    # [^a-z0-9] 清洗后残留裸 "fc" 当查询键——前缀匹配命中一切 FC* 俱乐部，
    # 最短键 FCV Dender EH 恒胜，5 支球队错挂比利时队徽（实测 FC东京/安道尔FC/
    # 巴黎FC/邓迪FC/奥克兰FC 全中）。改为「前无拉丁字符、后无拉丁字符」断言。
    value = re.sub(r"(^|[^a-z0-9])(football club|futbol club|fc|cf|ac|sc)(?![a-z0-9])", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _football_logos_search_team(team_name):
    """Resolve a team against football-logos.cc's public autocomplete index."""
    global FOOTBALL_LOGOS_INDEX
    english_name = team_backdrop.TEAM_EN.get(team_name, team_name).replace("_", " ")
    query_key = _football_logo_key(english_name)
    if not query_key:
        return None

    if FOOTBALL_LOGOS_INDEX is None:
        try:
            req = urlreq.Request(
                "https://football-logos.cc/ac-v2.json",
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            with urlreq.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
            FOOTBALL_LOGOS_INDEX = payload if isinstance(payload, list) else []
        except Exception:
            return None

    matches = []
    for item in FOOTBALL_LOGOS_INDEX:
        if not isinstance(item, dict):
            continue
        candidate_key = _football_logo_key(item.get("name"))
        category = item.get("categoryId")
        logo_id = item.get("id")
        logo_hash = item.get("h")
        if not (candidate_key and category and logo_id and logo_hash):
            continue
        if candidate_key == query_key:
            score = 0
        elif candidate_key.startswith(query_key):
            score = 1
        elif query_key in candidate_key:
            score = 2
        else:
            continue
        matches.append((score, len(candidate_key), category, logo_id, logo_hash))

    if not matches:
        return None
    _, _, category, logo_id, logo_hash = min(matches)
    return (
        "https://assets.football-logos.cc/logos/"
        f"{category}/64x64/{logo_id}.{logo_hash}.png"
    )


def _tdb_search_team(team_name):
    """串行查询 TheSportsDB（带退避防 429）。返回 {badge, id_api_football} 或 None。

    中文队名先通过 team_backdrop.TEAM_EN 转英文；TheSportsDB 的 strBadge 为 512×512 HD，
    优先使用，strTeamBadge 仅作为兜底。
    """
    global _tdb_last_call
    import time as _time
    elapsed = _time.time() - _tdb_last_call
    if elapsed < 0.6:
        _time.sleep(0.6 - elapsed)
    try:
        # 中文队名转英文搜索，空格替换为下划线以匹配 TDB 搜索习惯
        en = team_backdrop.TEAM_EN.get(team_name, team_name)
        url = "https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t=" + quote(en.replace(" ", "_"))
        req = urlreq.Request(url, headers={"User-Agent": UA})
        # 3s 超时：外网 TDB 慢/挂起时快速失败并缓存 unknown，
        # 避免 15s 超时的请求长时间占用浏览器连接池，堵住本地队标图片加载
        with urlreq.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            _tdb_last_call = _time.time()
            teams = data.get("teams", [])
            if not teams:
                return None
            t = teams[0]
            # strBadge 实测为 512×512 HD；strTeamBadge 实测常为 null，仅兜底
            badge = t.get("strBadge") or t.get("strTeamBadge")
            id_api = t.get("idAPIfootball")
            return {"badge": badge, "id_api_football": id_api}
    except Exception:
        return None




# 足球队徽 SVG 映射（football-data.org crests，官方免费静态资源，SVG 无背景色块）。
# 覆盖主流五大联赛豪门/强队；未映射的队回退 api-sports PNG（透明底）。
FB_CREST_IDS = {
    # 英超
    "阿森纳": 57, "曼城": 65, "曼彻斯特城": 65, "利物浦": 64, "曼联": 66, "曼彻斯特联": 66,
    "切尔西": 61, "热刺": 73, "托特纳姆热刺": 73, "纽卡斯尔联": 67, "阿斯顿维拉": 58,
    "布莱顿": 397, "西汉姆联": 346, "埃弗顿": 62, "狼队": 76, "水晶宫": 354, "富勒姆": 63,
    "莱斯特城": 338, "布伦特福德": 402, "伯恩茅斯": 1044, "诺丁汉森林": 351,
    "南安普顿": 340, "伊普斯维奇": 349,
    # 西甲
    "皇家马德里": 86, "巴塞罗那": 81, "马德里竞技": 78, "马竞": 78, "塞维利亚": 559, "瓦伦西亚": 95,
    "比利亚雷亚尔": 94, "皇家社会": 77, "毕尔巴鄂竞技": 80,
    # 意甲
    "国际米兰": 108, "尤文图斯": 109, "拉齐奥": 110, "AC米兰": 98, "佛罗伦萨": 99,
    "亚特兰大": 102,
    # 德甲
    "拜仁慕尼黑": 5, "拜仁": 5, "多特蒙德": 4, "勒沃库森": 3, "莱比锡红牛": 721,
    "法兰克福": 19, "斯图加特": 10, "门兴格拉德巴赫": 18, "沃尔夫斯堡": 12,
    # 法甲
    "巴黎圣日耳曼": 524,
}


# 球队简称/别名 → 标准名（数据源常用简称，映射表只收录全名，历史上导致大量队标缺失）
TEAM_ALIAS = {
    "巴萨": "巴塞罗那", "巴萨罗那": "巴塞罗那", "皇马": "皇家马德里", "国米": "国际米兰",
    "巴黎": "巴黎圣日耳曼", "多特": "多特蒙德", "申花": "上海申花", "上港": "上海海港",
    "国安": "北京国安", "泰山": "山东泰山", "纽卡": "纽卡斯尔联", "西汉姆": "西汉姆联",
    "毕尔巴鄂": "毕尔巴鄂竞技", "皇社": "皇家社会", "拉科": "拉科鲁尼亚", "莱斯特": "莱斯特城",
    "柏林联": "柏林联合",
    "萨巴赫": "沙巴巴库", "林茨": "LASK", "雅典AEK": "AEK雅典",
    "博多格林特": "博德闪耀",
    "米兰": "AC米兰", "药厂": "勒沃库森",
    "红牛": "莱比锡红牛", "大巴黎": "巴黎圣日耳曼", "恒大": "广州恒大",
}
# 数据源脏名前缀：如"英超第18轮南安普顿""欧冠1/4决赛皇马"——剥离赛事/轮次前缀后再解析
_COMPETITION_PREFIX = re.compile(
    r"^(欧冠|欧联|欧协|世俱杯|欧超杯|英超|英冠|西甲|意甲|德甲|法甲|中超|日职|韩职|美职|墨超|"
    r"阿甲|巴甲|足总杯|国王杯|联赛杯|超级杯|社区盾|友谊赛)?(第?\d+轮|1/\d+决赛|半决赛|四分之一决赛|决赛)?"
)


def _normalize_team_name(name):
    n = (name or "").strip()
    stripped = _COMPETITION_PREFIX.sub("", n, count=1)
    if stripped:
        n = stripped
    return TEAM_ALIAS.get(n, n)


def resolve_team_icon(team_name):
    """解析球队队标 URL。
    优先级: 0) 别名/脏名归一化  1) football-data SVG  2) 本地已知映射
    3) 本地缓存  4) football-logos.cc  5) TheSportsDB 查询  6) 默认空
    返回 dict: {url, source, id}
    """
    team_name = _normalize_team_name(team_name)
    # 0. football-data SVG 队徽（无背景色块，主流欧洲联赛全覆盖）
    fd_id = FB_CREST_IDS.get(team_name)
    if fd_id:
        return {"url": "https://crests.football-data.org/%d.svg" % fd_id,
                "source": "fbrest_svg", "id": fd_id}
    # 1. 本地已知映射（与前端 TEAM_ICONS 同步的子集，高可信）
    KNOWN_MAP = {
        "曼城": 50,
        "曼彻斯特城": 50,
        "利物浦": 40,
        "阿森纳": 42,
        "曼联": 33,
        "曼彻斯特联": 33,
        "切尔西": 49,
        "热刺": 47,
        "托特纳姆热刺": 47,
        "纽卡斯尔联": 34,
        "阿斯顿维拉": 66,
        "布莱顿": 51,
        "西汉姆联": 48,
        "埃弗顿": 45,
        "莱斯特城": 46,
        "狼队": 39,
        "水晶宫": 52,
        "富勒姆": 63,
        "皇家马德里": 541,
        "巴塞罗那": 529,
        "马德里竞技": 530,
        "塞维利亚": 536,
        "比利亚雷亚尔": 533,
        "瓦伦西亚": 532,
        "毕尔巴鄂竞技": 531,
        "皇家社会": 548,
        "皇家贝蒂斯": 543,
        "国际米兰": 505,
        "AC米兰": 489,
        "米兰": 489,
        "尤文图斯": 496,
        "罗马": 497,
        "那不勒斯": 492,
        "亚特兰大": 499,
        "拉齐奥": 487,
        "佛罗伦萨": 502,
        "都灵": 503,
        "博洛尼亚": 500,
        "拜仁慕尼黑": 157,
        "拜仁": 157,
        "多特蒙德": 165,
        "勒沃库森": 168,
        "莱比锡红牛": 173,
        "法兰克福": 169,
        "斯图加特": 172,
        "门兴格拉德巴赫": 163,
        "巴黎圣日耳曼": 85,
        "马赛": 81,
        "里昂": 80,
        "摩纳哥": 91,
        "里尔": 79,
        "尼斯": 84,
        "本菲卡": 211,
        "波尔图": 212,
        "里斯本竞技": 211,
        "阿贾克斯": 94,
        "埃因霍温": 197,
        "费耶诺德": 209,
        "凯尔特人": 236,
        "流浪者": 235,
        "布鲁日": 112,
        "加拉塔萨雷": 645,
        "费内巴切": 611,
        "圣洛伦索": 1191,
        "阿根廷独立队": 453,
        "河床": 435,
        "米亚尔比": 2240,
        "布拉迪斯拉发": 186,
        "贝尔格莱德红星": 1221,
        "博德闪耀": 327,
        "奥林匹亚科斯": 151,
        "萨格勒布迪纳摩": 1124,
        "布拉格斯巴达": 1228,
        "沙姆洛克流浪": 652,
        "莱万特": 539,
        "阿尔梅里亚": 723,
        "米内罗竞技": 120,
        "桑托斯": 128,
        "尤文图德": 124,
        "利雅得胜利": 2938,
        "帕丘卡": "https://r2.thesportsdb.com/images/media/team/badge/k9duyw1747334895.png",
        "美洲狮": 1909,
        "老虎大学": 1903,
        "阿特拿斯": 2283,
        "华雷斯": 1900,
        "辛辛那提": 2242,
        "夏洛特FC": 18310,
        "明尼苏达联": 1612,
        "温哥华白浪": 1603,
        "亚特兰大联": 1605,
        "皇家盐湖城": 1602,
        "哥伦布机员": 1613,
        "洛里昂": 97,
        "勒阿弗尔": 111,
        "伊普斯维奇": 57,
        "奈梅亨": 413,
        "圣吉罗斯": 1138,
        "济州SK": "https://r2.thesportsdb.com/images/media/team/badge/hna7ae1736207131.png",
        "托尔": 2116,
        "贝雷达比历克": 2407,
        "库里科": 1742,
        "奴伯伦斯": 2337,
        "塔尔卡流浪者": 1729,
        "瑞模贝雷": 130,
        "维多利亚": 129,
        "巴拉纳竞技": 126,
        "图库曼竞技": 455,
        "飓风队": 447,
        "萨斯菲尔德": 438,
        "塔勒瑞斯": 456,
        "普拉滕斯竞技": 1064,
        "科尔多瓦中央SDE": 452,
        "上海海港": 836,
        "北京国安": 830,
        "山东泰山": 844,
        "上海申花": 833,
        "长春亚泰": 834,
        "天津津门虎": 837,
        "上海海港B队": 836,
        "长春喜都": 834,
        "巴吞联": 2769,
        "康塞普西翁": 3646,
        "阿拉特阿美尼亚": 582,
        "佩利根": 2322,
        "奥达里加": 2406,
        "地拉拿迪纳摩": 2206,
        "埃格纳蒂亚": 2361,
        "拉恩": 2320,
        "考诺萨基列斯": 3872,
        "比尔舒华夏普尔": 2397,
        "阿拉木图凯拉特": 2523,
        "索非亚列夫斯基": 2410,
        "纽波特郡": 1367,
        "韩职明星队": 5600,
        "帕尔梅拉斯": 121,
        "弗鲁米嫩塞": 124,
        "格雷米奥": 130,
        "博卡青年": 437,
        "拉普拉塔大学生": 450,
        "迈阿密国际": 9568,
        "奥兰多城": 1606,
        "利雅得新月": 2932,
        "吉达国民": 2929,
        "吉达联合": 1043,
        "达曼": 1579,
        "蓉城": 5648,
        "浙江": 848,
        "武汉三镇": 5695,
        "河南": 840,
        "奥萨苏纳": 727,
        "马洛卡": 798,
        "贝尔格拉诺": 440,
        "图卢兹": 96,
        "图尔库国际": 1164,
        "扎布热矿工": 345,
        "托卢卡": 2281,
        "莱昂": 2289,
        "洛杉矶FC": 1616,
        "达拉斯FC": 1597,
        "西雅图海湾人": 1595,
        "纳什威尔": 1599,
        "蒙特瑞": 1600,
        "蒙特雷湾": 1608,
        "埃尔帕索机车": 1609,
        "塔尔萨钻机工": 1611,
        "古比斯": 1165,
        "格拉茨风暴": 637,
        "费伦茨瓦罗斯": 1614,
        "瓦杜兹": 1615,
        "帕纳辛纳科斯": 617,
        "科金博": 2330,
        "米拉索": 1618,
        "泰格雷": 1619,
        "圣地亚哥漫游者": 1620,
        "圣菲利浦联": 1621,
        "圣马洛科斯": 1622,
        "拉卡莱拉联": 1623,
        "利马切颜色": 1624,
        "赫塔菲": 544,
        "拉科鲁尼亚": 546,
        "谢尔伯恩": 3854,
        "哈茨": 237,
        "希伯尼安": 249,
        "布拉加": 217,
        "安德莱赫特": 88,
        "哥本哈根": 131,
        "中日德兰": 133,
        "贝西克塔斯": 644,
        "塞萨洛尼基": 153,
        "基辅迪纳摩": 384,
        "萨尔茨堡": 564,
        "特温特": 415,
        # 2026-08-19 补：TDB 英文名查得的高清徽章（中文查询 TDB 失败）
        "柏林联合": "https://r2.thesportsdb.com/images/media/team/badge/q0o5001599679795.png",
        "巴拉多利德": "https://r2.thesportsdb.com/images/media/team/badge/guqal21757526050.png",
        "深圳新鹏城": "https://r2.thesportsdb.com/images/media/team/badge/jsv5901716654183.png",
        "普埃布拉": "https://r2.thesportsdb.com/images/media/team/badge/h0jgg51593451845.png",

        # 同日补 5 支「CJK+FC 后缀」队（_football_logo_key 残留 fc 曾错挂 Dender，matcher 已修）
        "FC东京": "https://assets.football-logos.cc/logos/japan/64x64/fc-tokyo.3d5d763a.png",
        "奥克兰FC": "https://assets.football-logos.cc/logos/new-zealand/64x64/auckland-fc.0814647e.png",
        "安道尔FC": "https://assets.football-logos.cc/logos/spain/64x64/fc-andorra.2d86d24c.png",
        "巴黎FC": "https://assets.football-logos.cc/logos/france/64x64/paris-fc.d3c4901c.png",
        "邓迪FC": "https://assets.football-logos.cc/logos/scotland/64x64/dundee.83e57059.png",

        # 2026-09-19 v1.32 补：站点赛事全量扫出的 163 支缺标队（football-logos.cc
        # 64px + TDB 512px 徽章，逐队人工甄别——贪心匹配曾把 悉尼国王→Sydney FC、
        # 墨尔本联→Melbourne City、卢加诺/阿特拉斯→阿根廷同名队，均已修正；
        # U23 用足协徽；篮球队按 TDB sport=Basketball 校验）
        "SBV精英队": "https://assets.football-logos.cc/logos/netherlands/64x64/excelsior-rotterdam.9bd13c24.png",
        "不莱梅": "https://assets.football-logos.cc/logos/germany/64x64/werder-bremen.35e99fc4.png",
        "东京绿茵": "https://assets.football-logos.cc/logos/japan/64x64/tokyo-verdy.29cfa7c6.png",
        "东南墨尔本凤凰": "https://r2.thesportsdb.com/images/media/team/badge/73xo0d1755702709.png",
        "中国U23": "https://assets.football-logos.cc/logos/china/64x64/china-national-team.d9b8835b.png",
        "中国香港女篮": "https://r2.thesportsdb.com/images/media/team/badge/bxuaz71704648483.png",
        "乌德勒支": "https://assets.football-logos.cc/logos/netherlands/64x64/fc-utrecht.e6fcb34c.png",
        "乔治罗尼亚": "https://assets.football-logos.cc/logos/poland/64x64/jagiellonia.e3b77a0c.png",
        "什切青": "https://assets.football-logos.cc/logos/poland/64x64/pogon.4e231a97.png",
        "代格福什": "https://assets.football-logos.cc/logos/sweden/64x64/degerfors.6ff855a4.png",
        "伊拉瓦拉飞鹰": "https://r2.thesportsdb.com/images/media/team/badge/juj7y91725980615.png",
        "伊朗U23": "https://assets.football-logos.cc/logos/iran/64x64/iran-national-team.b40d4914.png",
        "休达": "https://assets.football-logos.cc/logos/spain/64x64/ceuta.20a6c69a.png",
        "伯恩利": "https://assets.football-logos.cc/logos/england/64x64/burnley.e3bc6326.png",
        "佛山南狮": "https://r2.thesportsdb.com/images/media/team/badge/gxrbpw1726937225.png",
        "克拉科维亚": "https://assets.football-logos.cc/logos/poland/64x64/cracovia.bb734224.png",
        "克雷莫纳": "https://assets.football-logos.cc/logos/italy/64x64/cremonese.84709685.png",
        "内卡萨": "https://assets.football-logos.cc/logos/mexico/64x64/necaxa.5ac11b37.png",
        "冈山绿雉": "https://assets.football-logos.cc/logos/japan/64x64/fagiano-okayama.71498843.png",
        "凯勒雷斯": "https://assets.football-logos.cc/logos/italy/64x64/carrarese.7124f579.png",
        "利勒斯特罗姆": "https://assets.football-logos.cc/logos/norway/64x64/lillestrom.045ea2a1.png",
        "利斯特雷": "https://assets.football-logos.cc/logos/argentina/64x64/deportivo-riestra.b1e69a93.png",
        "加济安泰普大都会": "https://assets.football-logos.cc/logos/turkey/64x64/gaziantep.8fe98f9b.png",
        "加的斯": "https://assets.football-logos.cc/logos/spain/64x64/cadiz.fdf257af.png",
        "勒芒": "https://assets.football-logos.cc/logos/france/64x64/le-mans.11abbc97.png",
        "千叶市原": "https://assets.football-logos.cc/logos/japan/64x64/jef-united-chiba.2f334567.png",
        "华沙普洛克": "https://assets.football-logos.cc/logos/poland/64x64/wisla-plock.0a7afe9c.png",
        "华沙莱吉亚": "https://assets.football-logos.cc/logos/poland/64x64/legia-warszawa.c29b3ec2.png",
        "南蒂罗尔": "https://r2.thesportsdb.com/images/media/team/badge/8fico41579725098.png",
        "博尔顿": "https://assets.football-logos.cc/logos/england/64x64/bolton.274c8db2.png",
        "卡坦萨罗": "https://assets.football-logos.cc/logos/italy/64x64/catanzaro.1ac7deb6.png",
        "卡尔斯鲁厄": "https://assets.football-logos.cc/logos/germany/64x64/karlsruher.06772e9f.png",
        "卡斯特利翁": "https://assets.football-logos.cc/logos/spain/64x64/castellon.eb62f4ca.png",
        "卢加诺": "https://assets.football-logos.cc/logos/switzerland/64x64/lugano.3d05b3b6.png",
        "卢塞恩": "https://assets.football-logos.cc/logos/switzerland/64x64/luzern.48fd812f.png",
        "卢宾扎格勒比": "https://assets.football-logos.cc/logos/poland/64x64/zaglebie-lubin.85bc2192.png",
        "吉尔吉斯斯坦U23": "https://assets.football-logos.cc/logos/kyrgyzstan/64x64/kyrgyzstan-national-team.55ad948d.png",
        "吉马良斯": "https://assets.football-logos.cc/logos/portugal/64x64/vitoria-de-guimaraes.94af668d.png",
        "哈萨克斯坦女篮": "https://r2.thesportsdb.com/images/media/team/badge/we4msa1704648627.png",
        "哥德堡盖斯": "https://assets.football-logos.cc/logos/sweden/64x64/gais.abe24254.png",
        "哥罗纳": "https://assets.football-logos.cc/logos/poland/64x64/korona-kielce.7d8f2f29.png",
        "图恩": "https://assets.football-logos.cc/logos/switzerland/64x64/thun.6356590c.png",
        "圣加仑": "https://assets.football-logos.cc/logos/switzerland/64x64/st-gallen.deb64fa5.png",
        "圣图尔登": "https://assets.football-logos.cc/logos/belgium/64x64/sint-truidense.a2bb0b43.png",
        "圣埃蒂安": "https://assets.football-logos.cc/logos/france/64x64/as-saint-etienne.144deb42.png",
        "圣塔菲联队": "https://assets.football-logos.cc/logos/argentina/64x64/union.8593d85c.png",
        "圣米伦": "https://assets.football-logos.cc/logos/scotland/64x64/st-mirren.7dc9c309.png",
        "圣约翰斯通": "https://assets.football-logos.cc/logos/scotland/64x64/st-johnstone.a400a34f.png",
        "圣路易斯竞技": "https://assets.football-logos.cc/logos/mexico/64x64/atletico-de-san-luis.d57a75db.png",
        "坎布尔": "https://assets.football-logos.cc/logos/netherlands/64x64/sc-cambuur.7f9c2f80.png",
        "埃尔夫斯堡": "https://assets.football-logos.cc/logos/sweden/64x64/elfsborg.8683830b.png",
        "埃尔祖鲁姆体育": "https://assets.football-logos.cc/logos/turkey/64x64/erzurumspor.837b9b69.png",
        "埃弗斯堡": "https://assets.football-logos.cc/logos/germany/64x64/sv-elversberg.803d172e.png",
        "埃瓦尔": "https://assets.football-logos.cc/logos/spain/64x64/eibar.334990c3.png",
        "埃登斯": "https://assets.football-logos.cc/logos/spain/64x64/eldense.70f4e1e3.png",
        "基斯迪辛特": "https://assets.football-logos.cc/logos/norway/64x64/kristiansund.286e3bd4.png",
        "基马诺克": "https://assets.football-logos.cc/logos/scotland/64x64/kilmarnock.ebb7b56f.png",
        "塞尔维特": "https://assets.football-logos.cc/logos/switzerland/64x64/servette.0be8884e.png",
        "墨尔本联": "https://r2.thesportsdb.com/images/media/team/badge/adpuke1755702629.png",
        "大连可为": "https://r2.thesportsdb.com/images/media/team/badge/tu9jt11773419457.png",
        "奥尔格里特": "https://assets.football-logos.cc/logos/sweden/64x64/orgryte.aae88c18.png",
        "奥德赫维里": "https://assets.football-logos.cc/logos/belgium/64x64/oud-heverlee-leuven.1fa58bc5.png",
        "奥斯纳布鲁克": "https://assets.football-logos.cc/logos/germany/64x64/osnabruck.fa51589b.png",
        "奥胡斯": "https://assets.football-logos.cc/logos/denmark/64x64/agf.dbceb779.png",
        "威尼斯": "https://assets.football-logos.cc/logos/italy/64x64/venezia.65aa8dbe.png",
        "威廉二世": "https://assets.football-logos.cc/logos/netherlands/64x64/willem-ii.fc83ba30.png",
        "安特卫普": "https://assets.football-logos.cc/logos/belgium/64x64/antwerp.d58478c8.png",
        "巴勒莫": "https://assets.football-logos.cc/logos/italy/64x64/palermo.056287c3.png",
        "巴塞尔": "https://assets.football-logos.cc/logos/switzerland/64x64/basel.ccd56f6b.png",
        "布伦瑞克": "https://assets.football-logos.cc/logos/germany/64x64/eintracht-braunschweig.9301aeb7.png",
        "布洛涅": "https://assets.football-logos.cc/logos/france/64x64/boulogne.b28e2dcf.png",
        "布雷斯特": "https://assets.football-logos.cc/logos/france/64x64/brest.0586bbdf.png",
        "希洪竞技": "https://assets.football-logos.cc/logos/spain/64x64/sporting-gijon.23aa5612.png",
        "帕多瓦": "https://assets.football-logos.cc/logos/italy/64x64/padova.9e965adb.png",
        "年青人": "https://assets.football-logos.cc/logos/switzerland/64x64/young-boys.4aadc488.png",
        "弗洛西诺尼": "https://assets.football-logos.cc/logos/italy/64x64/frosinone.2ec7ffe1.png",
        "德累斯顿": "https://assets.football-logos.cc/logos/germany/64x64/dynamo-dresden.f936408a.png",
        "恩特拉": "https://assets.football-logos.cc/logos/italy/64x64/virtus-entella.c0da688c.png",
        "悉尼国王": "https://r2.thesportsdb.com/images/media/team/badge/gsfe5x1550073324.png",
        "拉普拉塔体操": "https://assets.football-logos.cc/logos/argentina/64x64/gimnasia-lp.2aafca49.png",
        "拉路维尔": "https://assets.football-logos.cc/logos/belgium/64x64/raal-la-louviere.d549f5c0.png",
        "斯达": "https://assets.football-logos.cc/logos/norway/64x64/start.a8354aef.png",
        "新西兰破坏者": "https://r2.thesportsdb.com/images/media/team/badge/83088b1725978587.png",
        "无锡吴钩": "https://r2.thesportsdb.com/images/media/team/badge/ylvkag1726938322.png",
        "日本U23": "https://assets.football-logos.cc/logos/japan/64x64/japan-national-team.873d5cb6.png",
        "日本女篮": "https://r2.thesportsdb.com/images/media/team/badge/0gckkv1651190201.png",
        "昂热": "https://assets.football-logos.cc/logos/france/64x64/angers.9aec6d75.png",
        "曼托瓦": "https://assets.football-logos.cc/logos/italy/64x64/mantova-1911.cf180a97.png",
        "朴茨茅斯": "https://assets.football-logos.cc/logos/england/64x64/portsmouth.7ed5c164.png",
        "杭州临平昊越": "https://r2.thesportsdb.com/images/media/team/badge/qwvc4f1743181934.png",
        "林肯城": "https://assets.football-logos.cc/logos/england/64x64/lincoln-city.5153edd1.png",
        "柏太阳神": "https://assets.football-logos.cc/logos/japan/64x64/kashiwa-reysol.03ad59c5.png",
        "查尔顿": "https://assets.football-logos.cc/logos/england/64x64/charlton.09ab0c5f.png",
        "根克勒比利吉": "https://assets.football-logos.cc/logos/turkey/64x64/genclerbirligi.141ca9af.png",
        "格拉斯哥流浪者": "https://assets.football-logos.cc/logos/scotland/64x64/rangers.5c081147.png",
        "桑德捷斯基": "https://assets.football-logos.cc/logos/denmark/64x64/sonderjyske.0a90b467.png",
        "桑德菲杰": "https://assets.football-logos.cc/logos/norway/64x64/sandefjord.b68bee9b.png",
        "桑普多利亚": "https://assets.football-logos.cc/logos/italy/64x64/sampdoria.0420ea9a.png",
        "梅斯": "https://assets.football-logos.cc/logos/france/64x64/fc-metz.06656ce1.png",
        "梅赫伦": "https://assets.football-logos.cc/logos/belgium/64x64/mechelen.91c1be60.png",
        "欧登塞": "https://assets.football-logos.cc/logos/denmark/64x64/odense-boldklub.38b78f7d.png",
        "比勒菲尔德": "https://assets.football-logos.cc/logos/germany/64x64/arminia-bielefeld.2ae199a3.png",
        "比萨": "https://assets.football-logos.cc/logos/italy/64x64/pisa.53a7734c.png",
        "水户蜀葵": "https://assets.football-logos.cc/logos/japan/64x64/mito-hollyhock.ca36d9ae.png",
        "汉坎": "https://assets.football-logos.cc/logos/norway/64x64/hamkam.27b8f3c1.png",
        "沙勒罗瓦": "https://assets.football-logos.cc/logos/belgium/64x64/charleroi.9175aeba.png",
        "沙巴度尔": "https://assets.football-logos.cc/logos/spain/64x64/sabadell.bf421e53.png",
        "泰国U23": "https://assets.football-logos.cc/logos/thailand/64x64/thailand-national-team.da0c6edd.png",
        "洛桑": "https://assets.football-logos.cc/logos/switzerland/64x64/lausanne-sport.94621913.png",
        "洛默尔": "https://assets.football-logos.cc/logos/belgium/64x64/lommel.37a41719.png",
        "海牙": "https://assets.football-logos.cc/logos/netherlands/64x64/ado-den-haag.6271bca9.png",
        "特内里费": "https://assets.football-logos.cc/logos/spain/64x64/tenerife.ab50f66a.png",
        "特尔斯达": "https://assets.football-logos.cc/logos/netherlands/64x64/telstar.7c7e5c48.png",
        "特罗姆瑟": "https://assets.football-logos.cc/logos/norway/64x64/tromso.4c0ba4af.png",
        "特鲁瓦": "https://assets.football-logos.cc/logos/france/64x64/troyes.9a1bf8b3.png",
        "珀斯野猫": "https://r2.thesportsdb.com/images/media/team/badge/7gkvnd1755701286.png",
        "班菲尔德": "https://assets.football-logos.cc/logos/argentina/64x64/banfield.f4431b3c.png",
        "琴斯托霍瓦": "https://assets.football-logos.cc/logos/poland/64x64/rakow.ab7a1a7f.png",
        "瓦勒海姆": "https://assets.football-logos.cc/logos/belgium/64x64/zulte-waregem.bb2b6da2.png",
        "甘冈": "https://assets.football-logos.cc/logos/france/64x64/guingamp.aea1049a.png",
        "皇家奥维耶多": "https://assets.football-logos.cc/logos/spain/64x64/oviedo.165a9747.png",
        "皮亚斯特": "https://assets.football-logos.cc/logos/poland/64x64/piast.46717f60.png",
        "石家庄功夫": "https://r2.thesportsdb.com/images/media/team/badge/a4q6fx1739511291.png",
        "福图纳锡塔德": "https://assets.football-logos.cc/logos/netherlands/64x64/fortuna-sittard.1fb659f0.png",
        "福尔柯克": "https://assets.football-logos.cc/logos/scotland/64x64/falkirk.129c885c.png",
        "科特布斯": "https://assets.football-logos.cc/logos/germany/64x64/energie-cottbus.b65e177a.png",
        "科里蒂巴": "https://assets.football-logos.cc/logos/brazil/64x64/coritiba.8eab11d7.png",
        "科鲁姆": "https://assets.football-logos.cc/logos/turkey/64x64/corum.55157c14.png",
        "红星": "https://assets.football-logos.cc/logos/france/64x64/red-star-fc.c11dc246.png",
        "维京": "https://assets.football-logos.cc/logos/norway/64x64/viking.057a6e98.png",
        "维塞乌": "https://assets.football-logos.cc/logos/portugal/64x64/academico-de-viseu.a81d9464.png",
        "维琴察": "https://assets.football-logos.cc/logos/italy/64x64/vicenza.68adffb9.png",
        "罗森博格": "https://assets.football-logos.cc/logos/norway/64x64/rosenborg.4096b5e6.png",
        "腓特烈斯塔": "https://assets.football-logos.cc/logos/norway/64x64/fredrikstad.250253c1.png",
        "色格拉布鲁日": "https://assets.football-logos.cc/logos/belgium/64x64/cercle-brugge.fa894bb6.png",
        "艾华卡": "https://assets.football-logos.cc/logos/portugal/64x64/alverca.d0393a93.png",
        "苏黎世": "https://assets.football-logos.cc/logos/switzerland/64x64/zurich.965ff93d.png",
        "草蜢": "https://assets.football-logos.cc/logos/switzerland/64x64/grasshoppers.cbac245e.png",
        "荷尔斯泰因基尔": "https://assets.football-logos.cc/logos/germany/64x64/holstein-kiel.af752e8c.png",
        "莫托路宾": "https://assets.football-logos.cc/logos/poland/64x64/motor-lublin.fea7660a.png",
        "菲律宾女篮": "https://r2.thesportsdb.com/images/media/team/badge/rfu7w31704638282.png",
        "葡萄牙体育": "https://assets.football-logos.cc/logos/portugal/64x64/sporting-cp.b4809cb3.png",
        "西悉尼流浪者": "https://assets.football-logos.cc/logos/australia/64x64/western-sydney-wanderers.6da3345f.png",
        "贝内文托": "https://assets.football-logos.cc/logos/italy/64x64/benevento.cf40995f.png",
        "赣州瑞狮": "https://r2.thesportsdb.com/images/media/team/badge/2b2tb11729973180.png",
        "邓迪联队": "https://assets.football-logos.cc/logos/scotland/64x64/dundee-united.77b91f6b.png",
        "里奥阿维": "https://assets.football-logos.cc/logos/portugal/64x64/rio-ave.7132586c.png",
        "锡永": "https://assets.football-logos.cc/logos/switzerland/64x64/sion.941932f3.png",
        "长崎成功丸": "https://assets.football-logos.cc/logos/japan/64x64/v-varen-nagasaki.cf3f6cc7.png",
        "阿伯丁": "https://assets.football-logos.cc/logos/scotland/64x64/aberdeen.4cadc403.png",
        "阿兰亚士邦": "https://assets.football-logos.cc/logos/turkey/64x64/alanyaspor.1a4bb5e8.png",
        "阿尔克马尔": "https://assets.football-logos.cc/logos/netherlands/64x64/az-alkmaar.a2a2c71b.png",
        "阿斯科利": "https://assets.football-logos.cc/logos/italy/64x64/ascoli.6ea376d9.png",
        "阿特拉斯": "https://assets.football-logos.cc/logos/mexico/64x64/atlas.2a39988b.png",
        "阿维利诺": "https://assets.football-logos.cc/logos/italy/64x64/us-avellino-1912.1c1452fc.png",
        "阿雷佐": "https://assets.football-logos.cc/logos/italy/64x64/arezzo.64a289cd.png",
        "阿马多拉": "https://assets.football-logos.cc/logos/portugal/64x64/estrela-da-amadora.2205119b.png",
        "雷克瑟姆": "https://assets.football-logos.cc/logos/england/64x64/wrexham.5652ff90.png",
        "霍森斯": "https://assets.football-logos.cc/logos/denmark/64x64/horsens.5b3eb944.png",
        "韦斯特鲁": "https://assets.football-logos.cc/logos/belgium/64x64/westerlo.df6706b5.png",
        "马梅洛迪日落": "https://assets.football-logos.cc/logos/south-africa/64x64/mamelodi-sundowns.435ac696.png",
        "马瑟韦尔": "https://assets.football-logos.cc/logos/scotland/64x64/motherwell.a8fd7032.png",
        "鹿特丹斯巴达": "https://assets.football-logos.cc/logos/netherlands/64x64/sparta-rotterdam.8f93fe3c.png",
        "阿尔萨德": "https://r2.thesportsdb.com/images/media/team/badge/908a011774579337.png",
        "席贝": "https://r2.thesportsdb.com/images/media/team/badge/xlskhy1693027251.png",
        "侯赛因": "https://r2.thesportsdb.com/images/media/team/badge/ijbwwm1753204799.png",
        "沙巴巴库": "https://r2.thesportsdb.com/images/media/team/badge/o2ttq41614284640.png",
        "布拉格斯拉维亚": "https://r2.thesportsdb.com/images/media/team/badge/l7kl4n1759252139.png",
        "LASK": "https://r2.thesportsdb.com/images/media/team/badge/oox26l1683556395.png",
        "顿涅茨克矿工": "https://r2.thesportsdb.com/images/media/team/badge/sqrxsr1421791799.png",
        "AEK雅典": "https://r2.thesportsdb.com/images/media/team/badge/4nogst1602773624.png",
    }
    if team_name in KNOWN_MAP:
        val = KNOWN_MAP[team_name]
        if isinstance(val, int):
            return {"url": "https://media.api-sports.io/football/teams/%d.png" % val,
                    "source": "known", "id": val}
        else:
            return {"url": val, "source": "known_url", "id": None}

    # 2. 本地缓存：成功结果 30 天 TTL；查询失败 unknown 仅 10 分钟 TTL。
    #    默认队标需后台持续下载真实队标 → unknown 短缓存，过期后自动重查 TDB，
    #    查到真实队徽即返回（前端替换显示），避免永久 unknown 再无机会更新。
    cache = TEAM_ICON_CACHE.get(team_name)
    if cache:
        ttl = 30 * 86400 if cache.get("url") else 600
        if int(_time.time()) - int(cache.get("ts", 0)) < ttl:
            return {"url": cache.get("url"), "source": "cache", "id": cache.get("id")}

    # 3. football-logos.cc 在线补全（非商业项目使用）；成功结果进入既有磁盘缓存。
    football_logos_url = _football_logos_search_team(team_name)
    if football_logos_url:
        TEAM_ICON_CACHE[team_name] = {
            "url": football_logos_url,
            "id": None,
            "ts": int(_time.time()),
        }
        save_team_icon_cache()
        return {"url": football_logos_url, "source": "football_logos_cc", "id": None}

    # 4. TheSportsDB 查询
    tdb = _tdb_search_team(team_name)
    if tdb:
        badge = tdb.get("badge")
        tid = tdb.get("id_api_football")
        # 优先 TheSportsDB 的 512×512 HD badge（r2.thesportsdb.com 可达），
        # 无 badge 时才回退到 api-sports 150px 图标
        if badge:
            TEAM_ICON_CACHE[team_name] = {"url": badge, "id": tid, "ts": int(_time.time())}
            save_team_icon_cache()
            return {"url": badge, "source": "tdb_badge_hd", "id": tid}
        if tid:
            url = "https://media.api-sports.io/football/teams/%d.png" % tid
            TEAM_ICON_CACHE[team_name] = {"url": url, "id": tid, "ts": int(_time.time())}
            save_team_icon_cache()
            return {"url": url, "source": "tdb_api", "id": tid}

    # 查询失败也缓存 unknown（30 天 TTL）：避免每次刷新都重复慢查询 TDB，
    # 串行 0.6s/个的查询会占满浏览器连接池，把本地已下载队标的图片请求堵住
    TEAM_ICON_CACHE[team_name] = {"url": None, "id": None, "ts": int(_time.time())}
    save_team_icon_cache()
    return {"url": None, "source": "unknown", "id": None}


# 启动时加载缓存
load_team_icon_cache()


class Handler(SimpleHTTPRequestHandler):
    # HTTP/1.1 keep-alive：hls.js 分片请求高频（每分片一个请求），
    # 连接复用显著减少 TCP 握手开销（直播卡顿优化）。所有响应均带 Content-Length，安全。
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        # 网页版：前端在 www/ 子目录（proxy.py 在 pc-web/ 根）。
        # OTV_WEB_ROOT 与 web_entrypoint 共用同一个根，缓存表/健康字段也读它，
        # 避免「服务的目录」和「读 manifest 的目录」各说各话。
        super().__init__(*a, directory=str(_web_static_root()), **kw)

    def do_POST(self):
        parsed = urlparse(self.path)
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = b'{"ok":false,"error":"not found"}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _apk_update_manifest(self, parsed):
        """代理后台发布的公开 APK 清单，不把 APK 二进制经过网页函数。"""
        match = re.fullmatch(r"/apk-update/(tv|mobile)\.json", parsed.path)
        if not match:
            return False
        key = f"apk-update/{match.group(1)}.json"
        try:
            req = urlreq.Request(
                f"{APK_UPDATE_OBJECT_BASE}/{quote(key, safe='/')}",
                headers={"Accept": "application/json", "User-Agent": UA},
            )
            with urlreq.urlopen(req, timeout=8) as resp:
                body = resp.read(512 * 1024)
                status = resp.status
        except urlreq.HTTPError as exc:
            body = exc.read(4096)
            status = exc.code
        except Exception as exc:
            body = json.dumps({"error": "apk_manifest_unavailable", "detail": str(exc)[:120]}).encode("utf-8")
            status = 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        vlc_session = (qs.get("vlc_session") or [""])[0]
        vlc_token = _VLC_HLS_SESSION.set(vlc_session if re.fullmatch(r"[0-9a-f]{32}", vlc_session) else None)
        try:
            self._do_get(parsed)
        finally:
            _VLC_HLS_SESSION.reset(vlc_token)

    def _do_get(self, parsed):
        self._static_response = False
        if self._apk_update_manifest(parsed):
            return
        path = parsed.path
        football_api = (
            path in {"/api/matches", "/api/relay", "/api/team-icon", "/api/team-icon-img", "/api/health"}
            or re.fullmatch(r"/api/(?:stream|relayts)/\d+", path)
        )
        if path.startswith("/api/") and not football_api:
            body = b'{"ok":false,"error":"not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/matches":
            qs = parse_qs(parsed.query)
            wait_for_fresh = (qs.get("fresh") or ["0"])[0].lower() in ("1", "true", "yes")
            data, cached = build_api(wait_for_fresh=wait_for_fresh)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Cache", "HIT" if cached else "MISS")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        match = re.fullmatch(r"/api/stream/(\d+)", parsed.path)
        if match:
            qs = parse_qs(parsed.query)
            source = (qs.get("src") or ["a2"])[0]
            fresh = (qs.get("fresh") or ["0"])[0].lower() in ("1", "true", "yes")
            if source == "all":
                data, cached = resolve_88zhibo_streams(match.group(1), fresh=fresh)
            else:
                data, cached = resolve_88zhibo_stream(match.group(1), source, fresh=fresh)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Cache", "HIT" if cached else "MISS")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        match = re.fullmatch(r"/api/relayts/(\d+)", parsed.path)
        if match:
            qs = parse_qs(parsed.query)
            source = (qs.get("src") or ["a2"])[0]
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                for chunk in relayts_generator(match.group(1), source):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, GeneratorExit):
                return
            except Exception as exc:
                self.log_message("relayts error: %s" % str(exc)[:120])
            return
        if parsed.path == "/api/relay":
            qs = parse_qs(parsed.query)
            u = (qs.get("u") or [""])[0]
            ref = (qs.get("ref") or [None])[0]
            rng = self.headers.get("Range")
            is_m3u8_url = "m3u8" in u.lower()
            # ---- 媒体/直链（非 m3u8）：流式转发（Range 透传 seek，不全量缓冲）----
            # 命中短缓存（小分片）直接返回；大文件/带 Range 走 relay_stream 边收边发
            # HLS clients (notably FFmpeg / some native players) can send Range on
            # child playlists too. A playlist must still go through relay_url so it
            # gets rewritten and returned with a complete manifest body; treating
            # that Range as a media request made players parse the short m3u8 body
            # as a TS segment and fail before the first frame.
            if not is_m3u8_url:
                cache_key = _relay_cache_key(u)
                with _relay_lock:
                    hit = RELAY_CACHE.get(cache_key)
                    if hit and time.time() - hit[0] >= RELAY_CACHE_TTL:
                        hit = None

                # RELAY_CACHE stores only complete, status-200 non-manifest bodies.
                # Serve valid single byte ranges from those bodies; malformed or
                # multi-range requests continue through the upstream Range path.
                if hit and hit[1] == 200 and not hit[4]:
                    cached_body = hit[3]
                    status = 200
                    body = cached_body
                    range_headers = {}
                    can_serve_cache = not rng
                    if rng:
                        # The compact cache tuple has no validators to compare
                        # with If-Range, so let the origin evaluate that request.
                        parsed_range = (None if self.headers.get("If-Range") else
                                        _cached_byte_range(rng, len(cached_body)))
                        if parsed_range is not None:
                            status, start, end = parsed_range
                            can_serve_cache = True
                            range_headers["Accept-Ranges"] = "bytes"
                            if status == 416:
                                body = b""
                                range_headers["Content-Range"] = "bytes */%d" % len(cached_body)
                            else:
                                body = cached_body[start:end + 1]
                                range_headers["Content-Range"] = (
                                    "bytes %d-%d/%d" % (start, end, len(cached_body)))

                    if can_serve_cache:
                        # 注意：_prefetch_after 绝不能在 _relay_lock 内调用——
                        # Lock 不可重入，其内部的缓存查询会同线程死锁，
                        # 拖死整个中继（实测复现：全站分片请求超时）
                        self.send_response(status)
                        self.send_header("Content-Type", hit[2])
                        self.send_header("Cache-Control", "max-age=10")
                        self.send_header("Content-Length", str(len(body)))
                        for name, value in range_headers.items():
                            self.send_header(name, value)
                        self.end_headers()
                        try:
                            if body:
                                self.wfile.write(body)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        if status != 416:
                            _prefetch_after(u, ref)  # 锁外触发：继续预取后继片
                        return
                try:
                    status, ctype, extra, it, _m = relay_stream(u, ref, rng)
                except Exception as e:
                    status, ctype, extra, it = 500, "text/plain; charset=utf-8", {}, iter(
                        ("relay stream crash: %s" % str(e)[:200]).encode("utf-8"))
                sent = 0
                promised = 0
                try:
                    promised = int(extra.get("Content-Length") or 0)
                except Exception:
                    promised = 0
                # 非 Range 媒体由 relay_stream 标记 chunked 下发行（见其中注释：
                # 上游虚报 CL 短交付时按完整 body 干净收尾，不再硬报缺字节）。
                # HTTP/1.0 客户端不支持 chunked → 退化为「无 CL + 关闭分帧」。
                use_chunked = (
                    (extra.get("Transfer-Encoding") or "").lower() == "chunked"
                    and self.request_version >= "HTTP/1.1"
                )
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "max-age=10")
                    for k, v in extra.items():
                        if k.lower() == "transfer-encoding" and not use_chunked:
                            continue   # 1.0 客户端：不发 chunked 头，走关闭分帧
                        self.send_header(k, v)
                    if not use_chunked and "Content-Length" not in extra:
                        # 真流式（长度未知）：HTTP/1.1 下必须显式关闭连接分帧，
                        # 否则客户端不知道 body 何时结束 → 分片请求挂起（卡顿推手）
                        self.send_header("Connection", "close")
                        self.close_connection = True
                    # 统一 end_headers()：带 CORS（Android WebView file:// 必需）
                    self.end_headers()
                    for chunk in it:
                        if use_chunked:
                            self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii"))
                            self.wfile.write(chunk)
                            self.wfile.write(b"\r\n")
                        else:
                            self.wfile.write(chunk)
                        sent += len(chunk)
                    if use_chunked:
                        # 终止块 = 完整 body：上游短交付（虚报 CL 的中间盒形态）到
                        # 此为止按完整接收（TS 尾包残缺解复用器可弃），不再硬失败
                        self.wfile.write(b"0\r\n\r\n")
                        _prefetch_after(u, ref)
                    # 短包防护：上游中断导致 body < 已承诺 Content-Length 时，
                    # 必须掐断连接（close FIN）——keep-alive 下干净收尾会让客户端
                    # 把截断分片当完整数据解复用 → 解码器/hls worker 卡死无报错。
                    # 掐断后 Chromium 立刻报 ERR_CONTENT_LENGTH_MISMATCH → hls.js
                    # 走网络错误重试（可命中短缓存），秒级恢复而非冻结。
                    elif promised and sent < promised:
                        self.close_connection = True
                    else:
                        _prefetch_after(u, ref)  # 完整转发 → 预取后继片（多连接对抗限速）
                    return
                except (BrokenPipeError, ConnectionResetError):
                    return  # 客户端断开（seek/切源），停止转发
                except Exception:
                    # 生成器中途失败（上游超时/重置/读错误）：不写 chunked 终止块
                    # 直接掐断连接 → 客户端网络错误 → 既有重试链（同旧短包语义）
                    self.close_connection = True
                    return
                finally:
                    close_stream = getattr(it, "close", None)
                    if close_stream is not None:
                        close_stream()
            # ---- m3u8 播放列表：整包重写（分片/密钥指回本代理）----
            try:
                status, ctype, body, _err, is_m3u8 = relay_url(u, ref)
            except Exception as e:
                status, ctype, body, is_m3u8 = 500, "text/plain; charset=utf-8", ("relay crash: %s" % str(e)[:200]).encode("utf-8"), False
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            # m3u8 播放列表不缓存（需实时刷新），.ts 分片短缓存10s减少重复请求
            if is_m3u8:
                self.send_header("Cache-Control", "no-store")
            else:
                self.send_header("Cache-Control", "max-age=10")
            # ⚠️ 不在此处发 CORS 头：end_headers() 已统一发送。
            #    重复发送会得到两个 `Access-Control-Allow-Origin: *`，
            #    Chromium 规范禁止重复 CORS 头 → Android WebView(file:// origin=null)
            #    会直接拦截 relay 响应，中继播放全挂（桌面同源无 CORS 检查不暴露）。
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/team-icon":
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            result = resolve_team_icon(name)
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/team-icon-img":
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            result = resolve_team_icon(name)
            url = result.get("url")
            if url:
                # football-data SVG：后端抓取本地返回（SVG 无背景色 + 磁盘缓存复用）
                if result.get("source") == "fbrest_svg":
                    data, ok = fetch_svg_icon(url)
                    if ok and data:
                        self.send_response(200)
                        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                        self.send_header("Cache-Control", "max-age=86400")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    # SVG 抓取失败 → 1x1 透明图，前端 onerror 首字兜底
                    self._send_pixel_png()
                    return
                else:
                    # 非 SVG（api-sports/TDB PNG）：后端抓取 → 背景透明化 → 本地返回；
                    # 抓取失败才 302 原图（浏览器兜底）
                    raw = fetch_icon_bytes(url)
                    out = transparentize_png(raw) if raw else None
                    if out:
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Cache-Control", "max-age=86400")
                        self.send_header("Content-Length", str(len(out)))
                        self.end_headers()
                        self.wfile.write(out)
                        return
                    self.send_response(302)
                    self.send_header("Location", url)
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    return
            self._send_pixel_png()
            return
        if parsed.path == "/api/health":
            # assets 字段（2026-09-28）：当前内容寻址构建 + 归档保留的构建列表。
            # 归档降级（例如跳过损坏的历史档）必须在这里看得见，否则
            # compose healthcheck 与后台监控都无从发现「上一版本已丢失」。
            payload = {"ok": True, "time": time.strftime("%H:%M:%S"),
                       "assets": _web_asset_health()}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._static_response = True
        return super().do_GET()




    def _send_pixel_png(self):
        # 1x1 透明 PNG：队徽/海报找不到时兜底，避免 img onerror 触发。
        # ⚠️ v1.32 根修：旧 base64 实为 RGBA(255,255,0,127) 半透明黄色——App 端
        # coil 加载成功即认为「图标就绪」并隐藏首字兜底，1px 黄点被 Fit 拉伸成
        # 整格「黄色方块」（老板报障）。现已替换为真 RGBA(0,0,0,0)，并由
        # tests/test_placeholder_pixel_contract.py 锁死透明性。
        # no-store：不让浏览器缓存兜底图——否则 TDB 后续查到真实队标后，
        # 浏览器仍显示旧的透明像素（缓存 1 天）。每次都重新请求即可拿到新队标。
        pixel = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(pixel)))
        self.end_headers()
        self.wfile.write(pixel)

    def send_response(self, code, message=None):
        # 每个响应开始时重置 Cache-Control 追踪（keep-alive 下同一连接复用 handler）
        self._cc_sent = False
        self._response_code = code
        super().send_response(code, message)

    def send_header(self, keyword, value):
        # 记录本响应是否已发过 Cache-Control（修复：此前 end_headers 无条件补发
        # no-store，把分片 max-age=10 / 队标 max-age=86400 变成冲突双头，
        # Chromium 按最严格处理 → 所有 API 缓存头实际全部失效，重复请求全打上游）
        if keyword.lower() == "cache-control":
            self._cc_sent = True
        super().send_header(keyword, value)

    def end_headers(self):
        """静态文件默认禁用缓存（开发期改前端后刷新立即可见）；API 分支已自定
        Cache-Control（分片 max-age=10 / 队标 max-age=86400 / 清单 no-store）则
        尊重分支设置，不再重复发送。
        v1.22（2026-09-05 需求⑫ 网页版加载提速）：静态分支按扩展名给缓存——
        海报/图标等不可变资源 24h，app.js/style.css 5min，html 仍 no-store
        （发布即生效）。
        2026-09-28：构建产物改成内容寻址后，判定改以 www/manifest.json 的
        cacheControl 为准（进程内按 mtime 缓存），miss 才退回下面的扩展名规则。
        否则 assets/app.<sha16>.js 没有 ?v= 会被误判成 5min 短缓存，
        而旧式 ?v= 引用反而拿 immutable，缓存强度接反。"""
        if not getattr(self, "_cc_sent", False):
            cc = "no-store"
            if getattr(self, "_static_response", False):
                try:
                    low_all = self.path.lower()
                    low = low_all.split("?", 1)[0]
                    status = getattr(self, "_response_code", 200)
                    # Never let an error response be cached as an immutable asset:
                    # a 404 for a hashed name would otherwise be pinned for a year.
                    if status < 400 and not low.endswith(".html"):
                        table = _web_cache_control_table()
                        declared = table.get(low) if table else None
                        if declared is not None:
                            cc = declared
                        else:
                            versioned = "v=" in low_all
                            # A content-hashed filename is immutable by construction:
                            # the bytes behind the name never change.
                            content_hashed = _STATIC_CONTENT_HASH_RE.search(low) is not None
                            if re.search(r"\.(jpe?g|png|webp|gif|ico|webmanifest)$", low):
                                # v1.25：带 ?v= 版本号的资源视为不可变（发布即换版本号，
                                # 浏览器可长存；未版本化的维持 24h）
                                cc = "public, max-age=31536000, immutable" if (
                                    versioned or content_hashed) else "public, max-age=86400"
                            elif re.search(r"\.(js|css)$", low):
                                cc = "public, max-age=31536000, immutable" if (
                                    versioned or content_hashed) else "public, max-age=300"
                except Exception:
                    pass
            self.send_header("Cache-Control", cc)
        # CORS：原生客户端的本机资源请求可能来自 file://，
        # fetch 到 http://127.0.0.1:8090 属跨源，必须允许。
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Untrusted request lines must not inject terminal control sequences.
        message = re.sub(r"[\x00-\x1f\x7f]", "?", fmt % args)
        print("[%s] %s" % (time.strftime("%H:%M:%S"), message))




def main(port=None, bind=None):
    """启动代理服务器（阻塞）。安卓 Chaquopy 通过 main() 显式调用；
    直接运行 `python proxy.py [port]` 由 __main__ 块调用；
    云端容器不传参 —— 端口/绑定地址读环境变量
    PORT（容器平台注入）与 OTV_BIND（默认 127.0.0.1，容器设 0.0.0.0）。
    安卓侧 proxy_runner 传 bind="0.0.0.0"（投屏：电视需经局域网访问本机中继流）。"""
    if port is None:
        try:
            port = int(os.environ.get("PORT") or 8090)
        except ValueError:
            port = 8090
    if bind is None:
        bind = os.environ.get("OTV_BIND") or "127.0.0.1"
    # 多线程：直播分片/播放列表是并发请求，单线程服务器会互相阻塞。
    server = ThreadingHTTPServer((bind, port), Handler)
    print("球迹直播 数据代理服务器 http://%s:%d  (数据源 %s)" % (bind, port, _active_source["url"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
