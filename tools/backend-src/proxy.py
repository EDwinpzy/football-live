#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
球迹直播 数据代理服务器
- 静态文件服务（designs/ 各套前端）
- GET /api/matches : 抓取 88zhibo.tv → 解析为 JSON（缓存 60s）
- GET /api/health  : 健康检查
用法: python proxy.py [port]   (默认 8090)
"""
import os
import atexit
import re
import json
import html as _html
from html.parser import HTMLParser
import hashlib
import hhkan  # 影视源（hhkan0.com 抓取）
import hhkan_snapshot  # 好好看静态快照（云函数出口受风控时容灾，绝不引入外部卡片）
import team_backdrop  # 球队 16:9 海报（TheSportsDB）
import scraper  # 视频刮削（片名 → 完整元数据）
import douban_catalog
import media_index
import vod_api
import vod_sources
import metadata_client
import vod_resolver
import time
import time as _time  # 模块级别名：resolve_team_icon 等用 _time，与 _tdb_search_team 内局部导入一致
import threading
import contextvars
import concurrent.futures
import http.client
import base64
import copy
from urllib.parse import urljoin, quote, unquote, urlparse, urlsplit, parse_qs
import urllib.request as urlreq
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
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
FOOTBALL_PROXY_URL = "http://127.0.0.1:17890"
_UPSTREAM_PROXY_URL = contextvars.ContextVar("otv_upstream_proxy_url", default=None)
_PROXY_CLIENT_LOCK = threading.Lock()
_PROXY_CLIENTS = {}
_PROXY_OPENERS = {}
WWW_ROOT = Path(__file__).parent / "www"


def _active_upstream_proxy():
    return _UPSTREAM_PROXY_URL.get()


def _relay_cache_key(url):
    proxy_url = _active_upstream_proxy()
    return (url, proxy_url) if proxy_url else url


def _http_client_for(proxy_url):
    if not HAS_URLLIB3:
        return None
    if not proxy_url:
        return _http
    with _PROXY_CLIENT_LOCK:
        client = _PROXY_CLIENTS.get(proxy_url)
        if client is None:
            client = urllib3.ProxyManager(
                proxy_url, maxsize=16, retries=0,
                timeout=urllib3.Timeout(connect=5, read=15),
            )
            _PROXY_CLIENTS[proxy_url] = client
        return client

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
# 足球直播列表源。88zhibo 首页同时混排篮球，parse_matches 会过滤非足球赛事。
SOURCE_MIRRORS = [SOURCE_URL]
_active_source = {"i": 0, "url": SOURCE_MIRRORS[0]}
# 云端兜底（2026-09-10「足球无比赛」根因修复）：家宽出口被 yoozb 系拉黑
# （实测长沙移动 IP 对 yoozb.live/5cj.tv 全 403，镜像回退无效）时，比赛列表与
# 直播流解析回退到云端 otv-web（同一份 proxy.py 部署，腾讯云出口正常）。
# 云端自身在 scf_bootstrap 置 OTV_CLOUD_RELAY=0 防自环；设备/本地默认开启。
CLOUD_RELAY_BASE = os.environ.get(
    "OTV_CLOUD_BASE",
    "https://appletv-d5ge1bth794873f76.service.tcloudbase.com/web")
CLOUD_RELAY_ENABLED = os.environ.get("OTV_CLOUD_RELAY", "1") != "0"
"""影视片源云端兜底开关（2026-09-24）：自建 VPS 出口连不上片源站 API
（hongniuzy2 / bfzyapi / jyzyapi / hhkan 实测 curl 000），网页版影视详情页因此只剩
「暂无片源」。整站通用中继（比赛/直播）在自建栈里是明确关掉的迁移契约，所以这里给
片源单开一条：默认跟随通用中继（设备/本地默认开），自建部署用
OTV_VOD_CLOUD_FALLBACK=1 显式打开，CloudBase 退场时只需改这一个开关。"""
_VOD_FALLBACK_ENV = os.environ.get("OTV_VOD_CLOUD_FALLBACK")
CLOUD_VOD_FALLBACK_ENABLED = (
    CLOUD_RELAY_ENABLED if _VOD_FALLBACK_ENV is None
    else _VOD_FALLBACK_ENV.strip().lower() not in ("", "0", "false", "no"))
"""片源静态索引（2026-09-24，去 CloudBase 依赖的主方案）：自建 VPS 出口到不了片源站
（Cloudflare/海外 IP 全部连接超时，实测 18 个采集站全 000），而用户本机网络可以。
所以由本机 `tools/build_vod_line_index.py` 解析好线路、生成 JSON 索引推到 VPS 对象桶，
VPS 在本地匹配为空时直接读这份索引。索引地址为空即关闭该链路。"""
VOD_LINE_INDEX_URL = os.environ.get("OTV_VOD_LINE_INDEX_URL", "").strip()
VOD_LINE_INDEX_TTL = float(os.environ.get("OTV_VOD_LINE_INDEX_TTL", "300") or 300)
VOD_LINE_INDEX_MAX_AGE_DAYS = float(os.environ.get("OTV_VOD_LINE_INDEX_MAX_AGE_DAYS", "30") or 30)
# APK 应用内更新清单由后台写入 CloudBase 公开桶；网页函数只负责以稳定的
# /web/apk-update/{tv,mobile}.json 地址代理它，避免每次发 APK 都重新部署网页函数。
APK_UPDATE_OBJECT_BASE = os.environ.get(
    "OTV_APK_UPDATE_OBJECT_BASE",
    "https://appletv-d5ge1bth794873f76.api.tcloudbasegateway.com/v1/storages/object/hotupdate",
).rstrip("/")
_HHKAN_CHANNELS = {1: "电影", 2: "电视剧", 3: "动漫", 4: "综艺", 6: "短剧"}
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

# 测试可替换；生产环境首次访问影视 API 时再初始化，避免启动阶段触网。
VOD_SERVICE = None
_VOD_SERVICE_LOCK = threading.Lock()
_VOD_REMOTE_CONFIG_URL = os.environ.get("OTV_VOD_SOURCE_CONFIG_URL", "").strip()
_VOD_REMOTE_CONFIG_LOCK = threading.Lock()
_VOD_REMOTE_CONFIG_STATE = {"last_attempt": 0.0, "registry": None}
_VOD_REMOTE_CONFIG_TTL = 30.0
_STATIC_CONTENT_HASH_RE = re.compile(r"(?:^|[/._-])[0-9a-f]{8,64}(?=$|[/._-])", re.I)


def _shutdown_vod_service():
    service = VOD_SERVICE
    registry = getattr(service, "registry", None)
    close = getattr(registry, "close", None)
    if callable(close):
        close()


atexit.register(_shutdown_vod_service)


def _refresh_remote_vod_registry():
    """Load the latest published My TV source config from the private business API."""
    if not _VOD_REMOTE_CONFIG_URL:
        return None
    now = time.monotonic()
    registry = _VOD_REMOTE_CONFIG_STATE["registry"]
    if now - _VOD_REMOTE_CONFIG_STATE["last_attempt"] < _VOD_REMOTE_CONFIG_TTL:
        return registry
    with _VOD_REMOTE_CONFIG_LOCK:
        now = time.monotonic()
        if now - _VOD_REMOTE_CONFIG_STATE["last_attempt"] < _VOD_REMOTE_CONFIG_TTL:
            return _VOD_REMOTE_CONFIG_STATE["registry"]
        _VOD_REMOTE_CONFIG_STATE["last_attempt"] = now
        try:
            target = urlparse(_VOD_REMOTE_CONFIG_URL)
            if (target.scheme != "http" or target.hostname != "business-api" or
                    target.port != 8082 or target.path != "/internal/vod-sources" or
                    target.username or target.password or target.query or target.fragment):
                return _VOD_REMOTE_CONFIG_STATE["registry"]
            req = urlreq.Request(_VOD_REMOTE_CONFIG_URL, headers={"Accept": "application/json"})
            with urlreq.urlopen(req, timeout=4) as response:
                payload = json.loads(response.read(512 * 1024).decode("utf-8"))
            config = payload.get("config") if isinstance(payload, dict) else None
            version = payload.get("version") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or payload.get("ret") != 0 or
                    not isinstance(config, dict) or not isinstance(config.get("sources"), list)):
                return _VOD_REMOTE_CONFIG_STATE["registry"]
            version = int(version or config.get("version") or 0)
            if version <= 0:
                return _VOD_REMOTE_CONFIG_STATE["registry"]
            config = dict(config)
            config["version"] = version
            registry = vod_sources.SourceRegistry.from_dict(config)
            _VOD_REMOTE_CONFIG_STATE["registry"] = registry
            return registry
        except Exception:
            return _VOD_REMOTE_CONFIG_STATE["registry"]


def _vod_data_dir(preferred=None):
    """影视缓存/索引目录：代码目录可写就用它，不可写（云函数只读挂载）就落到临时目录。"""
    preferred = Path(preferred) if preferred is not None else Path(__file__).parent / ".cache"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "otv-data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


_IME_HOTWORD_SERVICE = None
_IME_HOTWORD_LOCK = threading.Lock()


def _get_ime_hotword_service():
    global _IME_HOTWORD_SERVICE
    if _IME_HOTWORD_SERVICE is None:
        with _IME_HOTWORD_LOCK:
            if _IME_HOTWORD_SERVICE is None:
                from ime_hotwords import HotwordService
                cache_root = Path(os.environ.get("OTV_DATA_DIR") or _vod_data_dir())
                _IME_HOTWORD_SERVICE = HotwordService(
                    _get_vod_service().catalog, cache_root / "ime_hotwords.json")
    return _IME_HOTWORD_SERVICE


def _get_vod_service():
    global VOD_SERVICE
    remote_registry = _refresh_remote_vod_registry()
    if VOD_SERVICE is None:
        with _VOD_SERVICE_LOCK:
            if VOD_SERVICE is None:
                cache_root = Path(os.environ.get("OTV_DATA_DIR") or _vod_data_dir())
                local_index = media_index.MediaIndex(cache_root / "media" / "mytv.db")
                catalog = douban_catalog.DoubanCatalog(
                    cache=douban_catalog.JsonDiskCache(cache_root / "douban"), media_index=local_index)
                registry = remote_registry or vod_sources.SourceRegistry.load_current()
                registry.history = vod_sources.LineHistory(cache_root / "vod_line_history.json")
                registry.stats = vod_sources.SourceStats(cache_root / "vod_source_stats.json")
                resolver = vod_resolver.VodResolver(
                    metadata_client.MetadataClient(),
                    vod_resolver.StaticLineIndex(
                        _vod_index_payload,
                        max_age_seconds=VOD_LINE_INDEX_MAX_AGE_DAYS * 86400),
                    registry,
                )
                VOD_SERVICE = vod_api.VodService(
                    catalog, registry, fallback_sources=_vod_fallback_sources(),
                    resolver=resolver)
                # 内置豆瓣快照预热搜索索引：云端出口到不了豆瓣，冷实例即便是
                # 访客直接搜索（没打开过影视首页）也必须先有结果。
                catalog.warm_index()
    # 管理端发布的配置由 App 原子写入运行目录；请求到来时热替换，无需重启 App。
    current = remote_registry or vod_sources.SourceRegistry.load_current()
    previous = None
    discarded = None
    with _VOD_SERVICE_LOCK:
        service = VOD_SERVICE
        if hasattr(service, "registry") and (
                (remote_registry is not None and current.version != service.registry.version) or
                current.version > service.registry.version):
            previous = service.registry
            current.history = previous.history
            current.stats = previous.stats
            resolver = getattr(service, "resolver", None)
            if resolver is not None:
                # Lock order is service lock -> resolver cache lock. Resolver code
                # never calls back into _get_vod_service while holding its cache lock.
                resolver.replace_registry(current)
            service.registry = current
            service._resolve_sources = current.resolve_subject
        elif hasattr(service, "registry") and current is not service.registry:
            # load_current/remote refresh may have produced a distinct object
            # with the already-active version. It was not adopted, so retire
            # its executor after leaving the service lock.
            discarded = current

    # Closing a registry can acquire its own pool lock; do it after both service
    # pointers and the resolver snapshot have been committed and the service lock
    # has been released.
    for registry in (previous, discarded):
        close = getattr(registry, "close", None)
        if callable(close):
            close()
    return VOD_SERVICE


def _safe_int(v, default, lo=None, hi=None):
    """安全解析整型参数：非法值/越界回落默认值，避免裸 int() 抛异常打崩请求线程。"""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return default
    return n



def _cloud_get_json(path, timeout=12, allow_when_relay_off=False):
    """云端 otv-web 兜底请求（同一份 proxy.py 的云端部署）：返回解析后的 dict。
    未启用/网络失败/响应异常一律抛错，由调用方回落原有失败路径。"""
    if not CLOUD_RELAY_ENABLED and not allow_when_relay_off:
        raise RuntimeError("cloud relay disabled")
    req = urlreq.Request(CLOUD_RELAY_BASE + path, headers={"User-Agent": UA})
    with urlreq.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    if not isinstance(data, dict):
        raise RuntimeError("cloud relay non-dict response")
    return data


def _cloud_vod_sources(subject, douban_id):
    """影视片源兜底（2026-09-24）：自建 VPS 出口到不了片源站（hongniuzy2 / bfzyapi /
    jyzyapi / hhkan 实测 curl 000 超时），网页版详情页只能显示「暂无片源」，而线路的
    播放地址（hn.bfvvs.com 等）在 VPS 上可取。这里复用足球同款云端兜底：本地匹配为
    空时问云端 `/vod/detail/{id}?refresh=1` 要一次线路。任何失败都返回空列表，绝不
    把「本地匹配成功」的既有路径带进网络等待。"""
    if not CLOUD_VOD_FALLBACK_ENABLED:
        return []
    ref = str(douban_id or "").removeprefix("douban:")
    if not ref:
        return []
    try:
        data = _cloud_get_json("/vod/detail/" + ref + "?refresh=1", timeout=25,
                              allow_when_relay_off=True)
    except Exception:
        return []
    sources = data.get("sources") if isinstance(data, dict) else None
    return sources if isinstance(sources, list) else []


_vod_index_cache = {"ts": 0.0, "loaded": False, "data": None}
_vod_index_lock = threading.Lock()


def _vod_index_payload():
    """拉取并缓存片源静态索引；失败同样进缓存，避免每次请求都去捶远端。"""
    if not VOD_LINE_INDEX_URL:
        return None
    now = time.time()
    with _vod_index_lock:
        if _vod_index_cache["loaded"] and now - _vod_index_cache["ts"] < VOD_LINE_INDEX_TTL:
            return _vod_index_cache["data"]
    data = None
    try:
        req = urlreq.Request(VOD_LINE_INDEX_URL, headers={"User-Agent": UA})
        with urlreq.urlopen(req, timeout=15) as resp:
            parsed = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        data = None
    with _vod_index_lock:
        _vod_index_cache.update(ts=now, loaded=True, data=data)
    return data


def _index_vod_sources(subject, douban_id):
    """从本机解析好、推上来的静态索引里取线路（先按豆瓣 id，再按标题+年份回落）。"""
    payload = _vod_index_payload()
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if not isinstance(entries, dict) or not entries:
        return []
    ref = str(douban_id or "").removeprefix("douban:")
    entry = entries.get(ref)

    def entry_key(candidate):
        return (vod_sources.normalize_title(candidate.get("title")), str(candidate.get("year") or ""))

    if not isinstance(entry, dict) and subject.get("title"):
        want = (vod_sources.normalize_title(subject.get("title")), str(subject.get("year") or ""))
        for candidate in entries.values():
            if isinstance(candidate, dict) and entry_key(candidate) == want:
                entry = candidate
                break
    if not isinstance(entry, dict):
        return []
    try:
        resolved_at = float(entry.get("resolved_at") or 0)
    except (TypeError, ValueError):
        resolved_at = 0.0
    if not resolved_at or time.time() - resolved_at > VOD_LINE_INDEX_MAX_AGE_DAYS * 86400:
        return []
    lines = entry.get("lines")
    return [line for line in lines if isinstance(line, dict)] if isinstance(lines, list) else []


def _vod_fallback_sources():
    """片源兜底顺序：静态索引优先（无外部依赖），云端回源作为可选最后一环。"""
    providers = []
    if VOD_LINE_INDEX_URL:
        providers.append(_index_vod_sources)
    if CLOUD_VOD_FALLBACK_ENABLED:
        providers.append(_cloud_vod_sources)
    return tuple(providers)


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
            proxy_url = _active_upstream_proxy()
            open_source = _url_opener(proxy_url).open if proxy_url else urlreq.urlopen
            with open_source(req, timeout=20) as resp:
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
_88Z_SOURCE_LABELS = {
    "a2": "线路A2",
    "migu": "线路migu",
    "q2": "线路Q2",
    "q3": "线路Q3",
    "c4": "线路C4",
}
_88Z_NON_FOOTBALL_RE = re.compile(
    r"篮球|男篮|女篮|NBA|CBA|WNBA|NBL|排球|羽毛球|乒乓球|网球|"
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
        sgb = hhkan.fetch_plain(LIVE7M_URL)
        csxl = hhkan.fetch_plain(LIVE7M_LIVE_URL)
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


# ===== 直播流地址解析（服务端执行混淆解密） =====
import subprocess
import tempfile

DECRYPT_JS = str(Path(__file__).parent / "decrypt_stream.js")
STREAM_CACHE = {}
STREAM_CACHE_MAX = 500  # 上限：超出清最旧一半（防长期运行无限增长）
_stream_lock = threading.Lock()
# 播放器域名轮换：每次从频道页/详情页响应中动态发现，不硬编码
# （实测 2026-08-04: qqlive100.html 内嵌 http://wtmdjxkq.com/qqliveHD100.php）
PLAYER_HOST_CACHE = {"ts": 0, "host": None}

# 安卓模式：解密服务地址（Kotlin 侧监听 127.0.0.1:8091，QuickJS 引擎替代 Node）
RHINO_DECRYPT_URL = "http://127.0.0.1:8091/decrypt"

# 安卓自动检测：Chaquopy 提供 sys.getandroidapilevel()（仅安卓存在），
# 比 sys.platform 更可靠（实测 Chaquopy 3.11 的 sys.platform 可能不是 "android"）。
# 无需手动设环境变量；桌面调试保持 node 分支。
import sys as _sys
ANDROID_MODE = (os.environ.get("ANDROID_MODE") == "1") or hasattr(_sys, "getandroidapilevel")


def decrypt_page(page, match_id, src):
    """执行播放器混淆解密，返回 {"url": ..., "html": ...}。

    安卓模式（ANDROID_MODE=1）：HTTP 桥接 QuickJS 解密服务（:8091），
    替代 node 子进程 —— 安卓无 node。
    桌面模式：保持原 node 调用（decrypt_stream.js），调试不受影响；
    node 不可用/输出不可解析（云端 Web 函数无 node）→ decrypt_python 兜底
    （quickjs 移植，v1.22：wheel 随部署包 vendor/ 内置，PYTHONPATH 注入）。
    """
    if ANDROID_MODE:
        req = urlreq.Request(
            RHINO_DECRYPT_URL,
            data=json.dumps({"html": page, "matchId": str(match_id)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlreq.urlopen(req, timeout=70) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    # 桌面/云端: node 执行混淆解密；不可用 → quickjs 移植兜底
    tmp = os.path.join(tempfile.gettempdir(), "ballbar_%s_%s.html" % (src, match_id))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(page)
        try:
            proc = subprocess.run(
                ["node", DECRYPT_JS, tmp, str(match_id)],
                capture_output=True, text=True, timeout=25,
                creationflags=0x08000000 if os.name == "nt" else 0,  # 隐藏窗口
            )
            try:
                return json.loads(proc.stdout.strip().splitlines()[-1])
            except Exception:
                pass  # 输出不可解析 → 落 quickjs 兜底
        except (FileNotFoundError, OSError):
            pass  # 无 node（云端容器）→ 落 quickjs 兜底
        # quickjs 兜底（decrypt_python：decrypt_stream.js 的 Python 移植，引擎同源 C 实现）
        try:
            import decrypt_python
            return decrypt_python.decrypt_html(page, str(match_id))
        except Exception:
            return {}
    finally:
        # 清理临时文件（长期运行避免 /tmp 累积）
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def discover_player_host():
    """从 qqlive 频道页发现当前播放器域名（5 分钟缓存）。
    返回形如 http://wtmdjxkq.com 的基础地址；失败回退 None（由调用方兜底）。
    v1.23（2026-09-06 信号源补全）：旧探测页 qqlive100.html 已从站点比赛列表
    下架（m.html 现按场次给 qqlive15~33/666 动态组合，编号随时轮换），固定探测
    100 迟早 404。改为候选列表依次探测：qqlive666（CCTV5+，几乎每场都在）→
    qqlive15（低编号常驻）→ qqlive100（历史页，当前仍 200 留作兜底）。
    """
    now = time.time()
    if PLAYER_HOST_CACHE["host"] and now - PLAYER_HOST_CACHE["ts"] < 300:
        return PLAYER_HOST_CACHE["host"]
    host = None
    for probe in ("qqlive666", "qqlive15", "qqlive100"):
        try:
            page = fetch_url("http://www.yoozb.live/tv/%s.html" % probe,
                             referer="http://www.yoozb.live/")
            mm = re.search(r'http://([^/"\']+)\.php', page)
            if mm:
                host = "http://" + mm.group(1)
                break
        except Exception:
            continue
    if host:
        PLAYER_HOST_CACHE.update(ts=now, host=host)
    return host


def fetch_url(url, referer=None, timeout=20):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    if HAS_URLLIB3:
        resp = _http_client_for(_active_upstream_proxy()).request(
            "GET", url, headers=headers,
            timeout=urllib3.Timeout(connect=5, read=timeout),
        )
        return resp.data.decode("utf-8", errors="ignore")
    else:
        req = urlreq.Request(url, headers=headers)
        proxy_url = _active_upstream_proxy()
        opener = _url_opener(proxy_url) if proxy_url else None
        with (opener.open(req, timeout=timeout) if opener else urlreq.urlopen(req, timeout=timeout)) as resp:
            return resp.read().decode("utf-8", errors="ignore")


# ==================== 豆瓣刮削 ====================
# 用 suggest API 搜标题（可匿名访问）→ 拿 id + 高清海报（s_ratio_poster 转 l 大图）；
# 再用移动版条目页（m.douban.com）拿评分 + 简介（meta description 内嵌，绕过 PC 反爬）。
# 结果带磁盘/内存缓存，避免每次请求都打豆瓣（反爬限流）。
_DOUBAN_CACHE = {}
_DOUBAN_CACHE_TTL = 3600 * 24  # 24h
_DOUBAN_MOB_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")


def _douban_search(q):
    """豆瓣 suggest 搜索：返回 [{id,title,year,poster}]，poster 为高清（l 大图）。"""
    url = "https://movie.douban.com/j/subject_suggest?q=" + quote(q)
    try:
        headers = {"User-Agent": UA, "Referer": "https://movie.douban.com/", "Accept": "*/*"}
        if HAS_URLLIB3:
            resp = _http.request("GET", url, headers=headers,
                                 timeout=urllib3.Timeout(connect=5, read=12))
            raw = resp.data
        else:
            req = urlreq.Request(url, headers=headers)
            with urlreq.urlopen(req, timeout=12) as resp:
                raw = resp.read()
        arr = json.loads(raw.decode("utf-8", errors="ignore"))
        out = []
        for it in arr:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            pid = str(it.get("id", "")).strip()
            poster = (it.get("img") or "").strip()
            # s_ratio_poster → l：豆瓣高清大图（1080×1594 实测）
            poster_hd = poster.replace("s_ratio_poster", "l") if poster else ""
            out.append({
                "id": pid,
                "title": it.get("title", "").strip(),
                "year": str(it.get("year", "")).strip(),
                "sub_title": (it.get("sub_title") or "").strip(),
                "poster": poster_hd or poster,
            })
        return out
    except Exception:
        return []


def _douban_detail(sid):
    """豆瓣移动版条目页：解析 meta description 内的评分 + 简介。"""
    url = "https://m.douban.com/movie/subject/%s/" % sid
    try:
        headers = {"User-Agent": _DOUBAN_MOB_UA, "Accept": "text/html,application/xhtml+xml"}
        if HAS_URLLIB3:
            resp = _http.request("GET", url, headers=headers,
                                 timeout=urllib3.Timeout(connect=5, read=12))
            html = resp.data.decode("utf-8", errors="ignore")
        else:
            req = urlreq.Request(url, headers=headers)
            with urlreq.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        rating = ""
        desc = ""
        m = re.search(r'<meta name="description" content="([^"]+)"', html)
        if m:
            text = m.group(1)
            r = re.search(r"豆瓣评分[:：]\s*([\d.]+)", text)
            if r:
                rating = r.group(1)
            i = re.search(r"简介[:：]\s*(.+)", text)
            if i:
                desc = i.group(1).strip()
        og = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        title = og.group(1).replace(" - 电影", "").strip() if og else ""
        return {"id": sid, "title": title, "rating": rating, "desc": desc}
    except Exception:
        return {"id": sid, "title": "", "rating": "", "desc": ""}


def douban_lookup(q):
    """按标题查豆瓣：搜索 + 详情，返回 {title, year, rating, desc, poster, id}。"""
    now = time.time()
    key = "q:" + q.strip().lower()
    hit = _DOUBAN_CACHE.get(key)
    if hit and now - hit[0] < _DOUBAN_CACHE_TTL:
        return hit[1]
    results = _douban_search(q)
    data = {"found": False, "title": "", "year": "", "rating": "", "desc": "", "poster": "", "id": ""}
    if results:
        # 优先标题完全匹配，其次第一个结果
        exact = next((x for x in results if x["title"] == q.strip()), None)
        best = exact or results[0]
        data.update({"found": True, "title": best["title"], "year": best["year"],
                     "poster": best["poster"], "id": best["id"]})
        if best["id"]:
            detail = _douban_detail(best["id"])
            data["rating"] = detail.get("rating", "")
            data["desc"] = detail.get("desc", "")
    _DOUBAN_CACHE[key] = (now, data)
    return data


def _stream_via_cloud(match_id, src, fresh):
    """直播流解析云端兜底：本网被 yoozb 系拉黑（403 错误页 → 找不到播放器 /
    连接直接失败）时由云端 otv-web 代解析。成功返回含 url 的 payload，
    失败返回 None，由调用方回落原错误路径（「未开播」语义不在此兜底）。"""
    try:
        data = _cloud_get_json("/api/stream/%s?src=%s%s" % (
            match_id, quote(src), "&fresh=1" if fresh else ""))
        if data.get("url"):
            data["cloud"] = True
            return data
    except Exception:
        pass
    return None


STREAM_SUCCESS_TTL = 60


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


def _88z_source_links(match_id):
    if not str(match_id).isdigit():
        raise ValueError("invalid_match_id")
    endpoint = "https://www.88zhibo.tv/live/%s/source" % match_id
    raw = fetch_url(endpoint, referer=SOURCE_URL, timeout=20)
    return _decode_88zhibo_source_links(raw)


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


def _resolve_88zhibo_direct(match_id, src, source_links=None):
    """解析源站源清单中的一个线路；line IDs 对应本次清单的原始下标。"""
    links = source_links if source_links is not None else _88z_source_links(match_id)
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
    key = "%s-%s-%s" % (src, match_id, "vpn" if _active_upstream_proxy() else "direct")
    with _stream_lock:
        cached = STREAM_CACHE.get(key)
        if not fresh and cached and cached["ts"] > 0 and now - cached["ts"] < STREAM_SUCCESS_TTL:
            return cached["data"], True
    try:
        stream_url, embed_referer, label = _resolve_88zhibo_direct(
            str(match_id), src, source_links=source_links,
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


def resolve_88zhibo_streams(match_id, fresh=False):
    """按源站本场清单枚举线路，只返回 HLS 解析并通过上游探测的线路。"""
    if not str(match_id).isdigit():
        return {"url": None, "sources": [], "error": "invalid_match_id"}, False
    try:
        links = _88z_source_links(str(match_id))
    except Exception as exc:
        return {"url": None, "sources": [], "error": str(exc)[:160] or "88zhibo 线路列表读取失败"}, False

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

    available = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                contextvars.copy_context().run,
                resolve_88zhibo_stream,
                str(match_id), item["src"], fresh, links,
            ): item
            for item in entries
        }
        for future in concurrent.futures.as_completed(futures):
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

    # Future completion order depends on CDN latency. Restore deterministic order.
    order = {item["src"]: position for position, item in enumerate(entries)}
    available.sort(key=lambda item: order.get(item.get("source"), len(order)))
    first = available[0] if available else None
    return {
        "url": first.get("url") if first else None,
        "player": "m3u8" if first else None,
        "source": first.get("source") if first else None,
        "name": first.get("name") if first else None,
        "referer": first.get("referer") if first else None,
        "sources": available,
        "error": None if available else "当前没有可播放的线路",
    }, False


def resolve_stream(match_id, src="bb", fresh=False):
    """返回 (payload, cached) — 通过 node 解密播放器提取流地址
    src: plu / bb → 每场专属频道页 → ballbar.php/plu.php 混淆播放器
         qqlive100..103 → 高清线路公共播放器 qqliveHD{编号}.php
    fresh: True = 跳过成功结果的 60s 缓存强制重新解析（播放中静默续签 /
         失败重试用——上游签名 URL 有效期仅 17~50 分钟，缓存会返回已过期地址）
    """
    now = time.time()
    key = "%s-%s" % (src, match_id)
    # 调试专用魔术 ID（2026-08-30 v1.8 解码兜底链验证用）：999001 → 本地测试源
    # （宿主机 ffmpeg testsrc2 无限流，High@3.2 720p45 复刻故障特征）。
    # 真实比赛 id 均为站点数字段，不会撞此值；域名常量非 IP 字面量。
    if match_id == "999001":
        return {"url": "http://testsrc.local:8099/live.m3u8", "player": "m3u8"}, False
    with _stream_lock:
        c = STREAM_CACHE.get(key)
        # ts<=0 表示「未开播」结果，永不缓存（前端重试时每次重新解析拿最新信号）
        # fresh=1 同样绕过成功缓存；写入照旧（缓存里总是留最新）
        if not fresh and c and c["ts"] > 0 and now - c["ts"] < STREAM_SUCCESS_TTL:
            return c["data"], True
        payload = {"url": None, "error": None}
    try:
        if src.startswith("qqlive"):
            # 高清线路：固定播放器，不依赖比赛 id；播放器域名动态发现
            player_url = "%s/qqliveHD%s.php" % (
                discover_player_host() or "http://wtmdjxkq.com",
                src.replace("qqlive", ""))
            page = fetch_url(player_url, referer="http://www.yoozb.live/")
        else:
            # 1. 抓频道页 → 找 ballbar 播放器地址（优先，其次任意 php 播放器）
            page = fetch_url("http://www.yoozb.live/tv/%s-%s.html" % (src, match_id))
            mm = re.search(r'src="(http://[^"]*ballbar\.php[^"]*)"', page)
            if not mm:
                mm = re.search(r'src="(http://[^"]+\.php[^"]*)"', page)
            if not mm:
                cloud = _stream_via_cloud(match_id, src, fresh)
                if cloud is not None:
                    with _stream_lock:
                        _trim_cache(STREAM_CACHE, STREAM_CACHE_MAX)
                        STREAM_CACHE[key] = {"ts": now, "data": cloud}
                    return cloud, False
                payload["error"] = "未找到播放器地址"
                with _stream_lock:
                    _trim_cache(STREAM_CACHE, STREAM_CACHE_MAX)
                    STREAM_CACHE[key] = {"ts": 0, "data": payload}
                return payload, False
            # 2. 抓播放器页（必须带正确 Referer）
            page = fetch_url(mm.group(1), referer="http://www.yoozb.live/")
        # 3. 执行混淆解密（安卓走 QuickJS HTTP 桥，桌面走 node）
        data = decrypt_page(page, match_id, src)
        payload["url"] = data.get("url") or None
        # URL 合法性校验：decrypt_stream.js 已过滤非 http 结果，
        # 此处二次防御 -- "error:xxx" 等非 http 字符串不能当流地址。
        if payload["url"] and not payload["url"].startswith(("http://", "https://")):
            payload["url"] = None
        # v1.22（2026-09-05 需求⑦）：qqlive 高清线是「固定频道播放器」（qqliveHD{n}.php
        # 不带比赛 id），解出的流地址常是 CDN 上已轮换/下架的旧 ls id（实测 404）。
        # v1.32（2026-09-19）：交付前活性探测扩展到 bb/plu 全源——原「bb/plu 是比赛
        # 专属频道页，不探测」在遇到「挂起型死地址」时让死链直达播放器，用户白等
        # ~20s 起播看门狗（当日奥克兰FC 友谊赛 plu 线 lmpull.va02.com 实测，见
        # _probe_alive 注释）。_probe_alive 短超时判死后按「未开播」语义返回
        # （ts=0 不缓存）——前端 30s 自动重试时重新解析，站点来流后自愈；
        # 探测自身异常 fail-open（保留地址交播放器裁决）。
        if payload["url"]:
            try:
                if not _probe_alive(payload["url"], None):
                    payload["url"] = None
            except Exception:
                pass
        with _stream_lock:
            if payload["url"]:
                payload["player"] = "m3u8" if "m3u8" in payload["url"].lower() else "direct"
                # 成功解析：CDN txSecret 有效期短，300s 缓存会让 URL 过期，
                # 且不同源可能轮换 CDN。成功也仅短缓存，避免拿到失效 URL。
                _trim_cache(STREAM_CACHE, STREAM_CACHE_MAX)
                STREAM_CACHE[key] = {"ts": now, "data": payload}
            else:
                payload["error"] = "信号尚未开播（开赛后自动可用）"
                # 未开播：**不设置过期时间戳**（返回 ts=0 表示永不缓存），
                # 前端 30s 自动重试时每次都重新解析，拿到最新信号。
                _trim_cache(STREAM_CACHE, STREAM_CACHE_MAX)
                STREAM_CACHE[key] = {"ts": 0, "data": payload}
        return payload, False
    except Exception as e:
        cloud = _stream_via_cloud(match_id, src, fresh)
        if cloud is not None:
            with _stream_lock:
                _trim_cache(STREAM_CACHE, STREAM_CACHE_MAX)
                STREAM_CACHE[key] = {"ts": now, "data": cloud}
            return cloud, False
        payload["error"] = "解析失败: %s" % str(e)[:100]
        with _stream_lock:
            _trim_cache(STREAM_CACHE, STREAM_CACHE_MAX)
            STREAM_CACHE[key] = {"ts": 0, "data": payload}
        return payload, False


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
# TTL=120s：VOD 分片不可变可长存（配合预取加速，见下）；直播滑动窗口旧分片自然淘汰
#（LRU trim 兜底），预取只取清单中已存在的后继片不会预取到不存在的分片。
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
# v1.14（2026-09-01 点播卡顿根治）：4→6——点播也走本中继后（App 端 ExoPlayer HLS
# 为单线程顺序拉片，免费 CDN 单连接供需比实测 0.86，90s 缓冲 ~11-20 分钟放空即
# 「点播 20 分钟后频繁卡顿」），供给并行度是唯一解；6 连接 × 单连接限速仍留余量。
# v1.22（2026-09-05 需求⑥ 足球直播卡顿）：6→8——直播分片供给波动的余量翻倍，
# 字节预算（48MB）不变仍兜底内存。
PREFETCH_AHEAD = 8
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
                # v1.14：预取复用 relay_url 主路径——自带 SSRF 防线（入口 host 校验）、
                # 双 Referer 策略、防盗链垃圾体守卫，≤8MB 响应自动写入 RELAY_CACHE。
                # 此前预取直接引用 _http：内嵌后端（Chaquopy）只有标准库、无 urllib3 →
                # NameError 被 except 吞掉 → **设备上预取从未生效**（供给=单连接 ~1×，
                # 点播缓冲爬不上去 = 「20 分钟后频繁卡顿」的另一半根因；桌面调试有
                # urllib3 所以从未暴露）。
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
        proxy_url = _active_upstream_proxy()
        with _MANIFEST_LOCK:
            for manifest_key, (lst, ts) in MANIFEST_SEGS.items():
                manifest_proxy = manifest_key[1] if isinstance(manifest_key, tuple) else None
                if manifest_proxy != proxy_url:
                    continue
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
    """记录媒体清单的分片顺序表，并预热头 2 片（加速首帧）。"""
    try:
        with _MANIFEST_LOCK:
            MANIFEST_SEGS[_relay_cache_key(u)] = (raw_segs, time.time())
            if len(MANIFEST_SEGS) > MANIFEST_SEGS_MAX:
                oldest = min(MANIFEST_SEGS.items(), key=lambda kv: kv[1][1])[0]
                MANIFEST_SEGS.pop(oldest, None)
        _prefetch_segments(raw_segs[:2], ref)
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
    """单策略出站请求头（relay_stream/预取用）。ref="none" → 不带 Referer：
    点播 hhkan CDN 由 App 直连起家（无 Referer），异站 Referer 反而可能被拒。"""
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if ref != "none":
        h["Referer"] = ref or "http://www.yoozb.live/"
    return h


def _relay_strategies(ref):
    """双策略出站请求头（relay_url 整包拉取用，按序回退）。
    - ref="none"（点播）：先无 Referer（App 直连同语义），失败再带默认 Referer 兜底；
    - ref="douban"（豆瓣图床）：必须带豆瓣 Referer（第三方 Referer 回 HTML），失败退无 Referer；
    - 其他（直播 yoozb 系）：先带 Referer（腾讯云 VCLOUD 必须），后无 Referer。"""
    if ref == "douban":
        return [
            {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8", "Referer": DOUBAN_IMG_REFERER},
            {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"},
        ]
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
    """点播 CDN 防盗链拦截形态识别：HTTP 200 + 正文仅 3 字节 "OK\\n"（142.248.x 系
    签名校验拒绝时实测）。合法小文件（EXT-X-KEY 的 16B AES 密钥/字幕等）不带 .ts
    后缀且非 mp2t，不受影响；真分片 <64B 物理不可能（1.5s × 最低码率也 ≥ 数 KB）。"""
    if size >= 64:
        return False
    if u.split("?", 1)[0].lower().endswith(".ts"):
        return True
    return "mp2t" in (ctype or "").lower()


class _NoRedirectHandler(urlreq.HTTPRedirectHandler):
    """urlreq 分支禁用自动重定向（改由 _open_upstream 手动逐跳跟随 + 校验）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urlreq.build_opener(_NoRedirectHandler)
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _url_opener(proxy_url, no_redirect=False):
    handlers = [urlreq.ProxyHandler({"http": proxy_url, "https": proxy_url})]
    if no_redirect:
        handlers.append(_NoRedirectHandler())
    with _PROXY_CLIENT_LOCK:
        opener = _PROXY_OPENERS.get((proxy_url, no_redirect))
        if opener is None:
            opener = urlreq.build_opener(*handlers)
            _PROXY_OPENERS[(proxy_url, no_redirect)] = opener
        return opener


def _open_upstream(u, headers, read_timeout, preload=True, connect_timeout=5):
    """打开上游 GET 连接，返回 (resp, final_url)。

    2026-09-04 v1.21 网页版：urllib3 2.x 自动重定向会丢 query —— IPTV 源站
    「82 端口 → 81 端口带 tm/key 签名」跳转变成无限循环（实测 too many redirects，
    curl -L 一跳即 200）。此处 redirect=False + 手动 ≤3 跳跟随：
    - 每跳目标经 _blocked_internal_url 校验（重定向到内网地址直接拒绝，SSRF）；
    - 301/302/303 保持 GET+原头（HLS 清单跳转无 POST 场景）；
    - 返回的 resp 与原两分支同型（urllib3: preload 后 .data 可用；
      urlreq: .read() 取体），status 属性两分支一致。"""
    cur, hops = u, 0
    proxy_url = _active_upstream_proxy()
    while True:
        if HAS_URLLIB3:
            resp = _http_client_for(proxy_url).request(
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
                opener = _url_opener(proxy_url, no_redirect=True) if proxy_url else _no_redirect_opener
                resp = opener.open(req, timeout=read_timeout)
                return resp, cur
            except urlreq.HTTPError as e:
                if e.code not in _REDIRECT_CODES:
                    raise
                loc = e.headers.get("Location") or ""
                e.close()
        if not loc:
            raise RuntimeError("redirect without Location: %s" % cur)
        cur = urljoin(cur, loc)
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

    # 出站策略见 _relay_strategies（v1.14：ref=none 点播无 Referer 优先）
    strategies = _relay_strategies(ref)
    last_err = None
    data = ctype = final = None
    for headers in strategies:
        try:
            resp, final = _open_upstream(u, headers, read_timeout=12, preload=True)
            status = resp.status
            if status >= 400:
                # 403 = 未开播（腾讯云 VCLOUD 约定），无需再用另一种 Referer 策略重试，
                # 直接返回 502 让前端进入「信号尚未开播」自动重试。
                raise RuntimeError("HTTP %d from %s" % (status, u))
            data = resp.data if HAS_URLLIB3 else resp.read()
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

        # v1.21：重写基准用重定向后的最终地址（final）——IPTV 源站 82→81 签名跳转后
        # 分片绝对路径落在 81 端口，用原始 u 做 urljoin 会拼出错误主机端口
        base = final or u

        def rw(uri):
            full = urljoin(base, uri)
            q = quote(full, safe="").replace("+", "%2B")
            # v1.14：点播链路（ref=none）把 none 传导给分片/子清单请求。
            # 88zhibo m3u8.html 来源则必须保留原 embed Referer；其他旧线路沿用当前清单 URL。
            relay_ref = ref if _is_88z_player_embed_referer(ref) else u
            rq = "none" if ref == "none" else quote(relay_ref, safe="").replace("+", "%2B")
            # v1.21 相对形式「relay?...」：以清单 URL（…/api/relay?u=…）所在目录解析
            # → 同目录 /api/relay；绝对路径 /api/relay 在「网关子路径部署」（网页版
            # https://…/web/）下会被浏览器解析到网关根而 404。设备端（根部署）
            # 解析结果与原绝对路径完全一致（ExoPlayer/hls.js 均按规范相对解析）。
            football_vpn = "&football_vpn=1" if _active_upstream_proxy() else ""
            return "relay?u=" + q + "&ref=" + rq + football_vpn

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
            _remember_manifest(u, raw_segs, ref)
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
            if len(data) <= 8 * 1024 * 1024:
                RELAY_CACHE[cache_key] = (time.time(), 200, ctype or "application/octet-stream", data, is_m3u8)
                _trim_relay()
    return 200, ctype or "application/octet-stream", data, None, is_m3u8


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
    # 只试一种策略，分片整线必挂。策略序与 _relay_strategies 同源（点播无 Referer 优先）。
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
                if HAS_URLLIB3:
                    resp.release_conn()
                else:
                    resp.close()
                last_err = "HTTP %d from %s" % (resp.status, u)
                resp = None
                continue   # 换下一 Referer 策略再试
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
        first = resp.read(64 * 1024)
        state["resp"], state["first"] = resp, first
        for h in ("Content-Range", "Accept-Ranges"):
            v = resp.headers.get(h)
            if v:
                extra[h] = v
        status, detect = resp.status, ctype
    except Exception as e:
        return 502, "text/plain; charset=utf-8", {}, iter(
            ("relay stream error: %s" % str(e)[:200]).encode("utf-8")), False

    if "mpegurl" in detect.lower():
        # 罕见：无 .m3u8 后缀的清单 → 关掉流，整包重写（manifest 体积小）
        try:
            if HAS_URLLIB3:
                state["resp"].release_conn()
            else:
                state["resp"].close()
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
            if HAS_URLLIB3:
                state["resp"].release_conn()
            else:
                state["resp"].close()
        except Exception:
            pass
        return 502, "text/plain; charset=utf-8", {}, iter(
            [("relay rejected: tiny body (%dB)" % len(first)).encode("utf-8")]), False
    buf = bytearray() if (not rng and known_len >= 0 and known_len <= 8 * 1024 * 1024) else None

    def gen():
        resp = state["resp"]
        first = state["first"]
        # 上游读失败必须向上抛（不得吞）：吞掉会导致响应 body 短于已发的
        # Content-Length 但连接干净收尾 → 客户端把截断分片当完整数据喂解码器
        # → hls.js worker 卡死（视频冻结、无报错、不再请求分片，实测复现）。
        # do_GET 捕获后按短包处理（掐断连接 → 客户端立刻网络错误 → hls.js 重试）
        try:
            if first:
                if buf is not None:
                    buf.extend(first)
                yield first
            while True:
                b = resp.read(chunk)
                if not b:
                    break
                if buf is not None and len(buf) <= 8 * 1024 * 1024:
                    buf.extend(b)
                yield b
        finally:
            try:
                if HAS_URLLIB3:
                    resp.release_conn()
                else:
                    resp.close()
            except Exception:
                pass
            # 仅完整收完的分片入缓存（中断/部分数据不得入缓存，防毒化）
            if buf is not None and known_len >= 0 and len(buf) == known_len:
                with _relay_lock:
                    RELAY_CACHE[_relay_cache_key(u)] = (time.time(), 200, ctype or "application/octet-stream", bytes(buf), False)
                    _trim_relay()

    # extra（Content-Range/Accept-Ranges）已在打开上游连接时构建
    upstream_te = ""
    try:
        upstream_te = (state["resp"].headers.get("Transfer-Encoding") or "").lower()
    except Exception:
        pass
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
        # = 足球直播「看一会就看不了、点重试/换源都没用」根因（2026-09-19 实测）。
        # 改以 chunked 下发行：0-chunk 收尾即「完整」，到达多少算多少——TS 尾部
        # 残缺不足一个 188B 包，解复用器直接丢弃，无感知；上游真异常（连接重置/
        # 超时/读错误）仍由生成器上抛 → do_GET 不写终止块 → 客户端网络错误 →
        # 既有重试链（语义与旧短包掐断一致，但虚报 CL 形态不再误伤）。
        extra["Transfer-Encoding"] = "chunked"
    elif known_len >= 0:
        # 普通 CL 上游（点播 CDN 等）：透传 Content-Length，行为与旧版一致；
        # 真短读由 urllib3 上抛 → do_GET 短包掐断 → 客户端重试
        extra["Content-Length"] = str(known_len)
    return status, ctype, extra, gen(), False


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


def _tdb_search_player(player_name):
    """查询 TheSportsDB 球员头像（strThumb 512×512）。带 0.6s 节流防 429。
    返回头像 URL 或 None。"""
    import time as _time
    global _tdb_last_call
    elapsed = _time.time() - _tdb_last_call
    if elapsed < 0.6:
        _time.sleep(0.6 - elapsed)
    try:
        url = "https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p=" + quote(player_name)
        req = urlreq.Request(url, headers={"User-Agent": UA})
        with urlreq.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            _tdb_last_call = _time.time()
            players = data.get("player", [])
            if not players:
                return None
            # 优先 strThumb（头像），其次 strCutout（透明抠像）
            for p in players:
                thumb = p.get("strThumb") or p.get("strCutout") or ""
                if thumb:
                    return thumb
            return None
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


# 足球新闻抓取缓存（60s TTL，避免每次刷新都抓源站）
NEWS_CACHE = {"ts": 0, "data": None}
# 搜索结果缓存：k/page → (ts, items)（10 分钟 TTL，见 _hhkan_api /hhkan/search）
_SEARCH_CACHE = {}
# v1.22 /hhkan/* 目录类页缓存（2026-09-05 需求⑫ 网页版加载提速）：{key: (ts, obj)}
_HHKAN_PAGE_CACHE = {}
_HHKAN_PAGE_CACHE_TTL = 90.0
# 2026-09-11 需求⑯：筛选项内容基本静态（类型/地区/语言/年份清单），单独放宽到 1h——
# 「全部」页每次进页/切类不必重新实时抓筛选（冷抓实测 0.7s+，弱网更久）
_HHKAN_PAGE_CACHE_TTL_FILTERS = 3600.0
_HHKAN_PAGE_CACHE_MAX = 80
class _LazyHhkanSnapshot:
    """22.9MB hhkan_snapshot.json 的全量 json.loads 从 import 期推迟到首个 /hhkan/* 请求。

    冷启动优化（2026-09-19 方案 C1）：旧实现模块级同步 parse，卡在绑端口之前，
    手机端 import proxy 要多等数秒、还多占几十 MB 内存（dict 膨胀）——而 hero 简介等
    豆瓣路径根本不读这份快照。这里首次访问才真正加载（锁防并发重复 parse），
    所有 `_HHKAN_SNAPSHOT.xxx` 调用点零改动；绑端口后由 _startup_warm 后台预载。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._real = None

    def load(self):
        if self._real is None:
            with self._lock:
                if self._real is None:
                    self._real = hhkan_snapshot.SnapshotCatalog.load_default()
        return self._real

    def preload(self):
        threading.Thread(target=self.load, daemon=True).start()

    def __getattr__(self, name):
        return getattr(self.load(), name)


_HHKAN_SNAPSHOT = _LazyHhkanSnapshot()
# 好好看主站在数据中心出口上被风控时，避免每个接口都重复等待多轮超时。
# unknown/probing 只允许一个请求探测；失败后 open 5 分钟，期间直接读好好看快照；
# 到期自动进入 half-open（由下一个请求单次探测），成功后恢复 healthy。
_HHKAN_ORIGIN_CIRCUIT_TTL = 300.0
# 2026-09-11 需求⑯：探针死线——_fetch 重试链最长 10 次×25s 超时（镜像轮换+挑战），
# 触发探针的那个请求会整段等完（弱网下数十秒 = 「筛选条件加载很慢」的组成部分）。
# 探针经单飞执行器执行并限时：超时立刻熔断走快照（快照实测 5-8ms），后台线程自然结束。
_HHKAN_PROBE_TIMEOUT = 12.0
_HHKAN_PROBE_EXEC = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="hhkan-probe")
_HHKAN_SNAPSHOT_FIRST = os.environ.get("OTV_HHKAN_SNAPSHOT_FIRST") == "1"
# 快照改懒加载后，import 期不再读 _HHKAN_SNAPSHOT.available（会触发 22MB parse）；
# SNAPSHOT_FIRST 的「开局即熔断走快照」语义推迟到首个实时调用时引导。
_HHKAN_ORIGIN_STATE = {"status": "unknown", "until": 0.0}
_HHKAN_ORIGIN_BOOTSTRAPPED = False
_HHKAN_ORIGIN_COND = threading.Condition()


def _hhkan_origin_bootstrap():
    global _HHKAN_ORIGIN_BOOTSTRAPPED
    if _HHKAN_ORIGIN_BOOTSTRAPPED:
        return
    with _HHKAN_ORIGIN_COND:
        if not _HHKAN_ORIGIN_BOOTSTRAPPED:
            if _HHKAN_SNAPSHOT_FIRST and _HHKAN_SNAPSHOT.available:
                _HHKAN_ORIGIN_STATE.update({
                    "status": "open",
                    "until": time.time() + _HHKAN_ORIGIN_CIRCUIT_TTL,
                })
            _HHKAN_ORIGIN_BOOTSTRAPPED = True


class _HhkanOriginCircuitOpen(RuntimeError):
    pass


def _hhkan_origin_call(fn):
    """执行一次好好看实时调用；首探针单飞，失败后快速熔断到静态快照。"""
    _hhkan_origin_bootstrap()
    if not _HHKAN_SNAPSHOT.available:
        return fn()
    wait_deadline = time.time() + 8.0
    while True:
        with _HHKAN_ORIGIN_COND:
            now = time.time()
            status = _HHKAN_ORIGIN_STATE["status"]
            if status == "open" and now < _HHKAN_ORIGIN_STATE["until"]:
                raise _HhkanOriginCircuitOpen("好好看实时源暂时熔断")
            if status == "open":
                _HHKAN_ORIGIN_STATE.update({"status": "unknown", "until": 0.0})
                status = "unknown"
            if status == "probing":
                left = wait_deadline - time.time()
                if left <= 0:
                    raise _HhkanOriginCircuitOpen("好好看实时探针仍在执行")
                _HHKAN_ORIGIN_COND.wait(timeout=left)
                continue
            if status == "unknown":
                _HHKAN_ORIGIN_STATE["status"] = "probing"
        break

    try:
        # 2026-09-11 需求⑯：探针限时执行（超时→熔断走快照；后台线程自然收尾丢弃）。
        # healthy 期同样经执行器（无感），保证 show 并行取页时 4 路并发上限稳定。
        fut = _HHKAN_PROBE_EXEC.submit(fn)
        try:
            value = fut.result(timeout=_HHKAN_PROBE_TIMEOUT)
        except concurrent.futures.TimeoutError:
            with _HHKAN_ORIGIN_COND:
                _HHKAN_ORIGIN_STATE.update({
                    "status": "open",
                    "until": time.time() + _HHKAN_ORIGIN_CIRCUIT_TTL,
                })
                _HHKAN_ORIGIN_COND.notify_all()
            raise _HhkanOriginCircuitOpen(
                "好好看实时探针超时 %.0fs（本轮走快照）" % _HHKAN_PROBE_TIMEOUT)
    except _HhkanOriginCircuitOpen:
        raise
    except Exception:
        with _HHKAN_ORIGIN_COND:
            _HHKAN_ORIGIN_STATE.update({
                "status": "open",
                "until": time.time() + _HHKAN_ORIGIN_CIRCUIT_TTL,
            })
            _HHKAN_ORIGIN_COND.notify_all()
        raise
    else:
        with _HHKAN_ORIGIN_COND:
            _HHKAN_ORIGIN_STATE.update({"status": "healthy", "until": 0.0})
            _HHKAN_ORIGIN_COND.notify_all()
        return value
# 搜索节流（2026-08-29）：app 逐键前缀搜索曾把源站打到 429 限流（后续请求全空）。
# 未命中缓存的搜索串行执行 + 相邻两次上游请求至少间隔 1.5s，从后端侧兜底保护源站。
_SEARCH_LOCK = threading.Lock()
_SEARCH_MIN_GAP = 2.5
_last_search_ts = [0.0]


def _search_throttled_fetch(k, page):
    """节流后的上游搜索（缓存未命中路径调用）：最小间隔 + 解析条目列表。"""
    token = hhkan.get_search_token()
    path = "/search?t=%s&k=%s&p=%s" % (quote(token), quote(k), page)
    with _SEARCH_LOCK:
        gap = _time.time() - _last_search_ts[0]
        if gap < _SEARCH_MIN_GAP:
            time.sleep(_SEARCH_MIN_GAP - gap)
        _last_search_ts[0] = _time.time()
    html = hhkan.get_page(path)
    items = []
    for m in re.finditer(
            r'<a href="/detail/(\d+)\.html" class="search-result-item">.*?'
            r'data-original="([^"]+)".*?<div class="title">([^<]*)</div>',
            html, re.S):
        items.append({"id": int(m.group(1)),
                      "cover": hhkan.img_url(m.group(2)),
                      "title": m.group(3).strip()})
    return items

# ---- 影视图片磁盘缓存（需求 影视#1 海报加载提速）----
# 经 /hhkan/proxy 中继的海报/封面落到本地磁盘，二次加载零上游请求。
# 缓存目录 = 运行目录下 img_cache/（app 内置后端运行目录即解压目录，应用私有可写）；
# 简易 LRU：文件数超上限按 mtime 淘汰最旧一批。
# 缓存键 = URL 的 sha256 十六进制摘要；isalnum 校验保证不含路径分隔符。
import hashlib as _hashlib

_IMG_CACHE = Path(os.getcwd()) / "img_cache"
IMG_CACHE_MAX_FILES = 2400          # ~100KB/张 × 2400 ≈ 240MB
IMG_CACHE_EVICT_BATCH = 400


def _img_cache_paths(url: str):
    key = _hashlib.sha256(url.encode("utf-8")).hexdigest()
    if not key.isalnum():
        raise ValueError("invalid cache key")
    return _IMG_CACHE / (key + ".bin"), _IMG_CACHE / (key + ".type")


def _img_cache_load(url: str):
    """命中返回 (bytes, content_type)；未命中/损坏返回 None。"""
    try:
        p, tp = _img_cache_paths(url)
        if not (p.is_file() and tp.is_file()):
            return None
        data = p.read_bytes()
        ctype = tp.read_text(encoding="ascii").strip() or "image/jpeg"
        # 老版本曾将豆瓣抓取失败时的 1×1 PNG 当作成功海报缓存。遇到旧条目时
        # 删除两份文件并重新抓取，不能继续把占位图命中为正常图片。
        if _is_cached_poster_placeholder(data):
            _img_cache_remove(url)
            return None
        os.utime(str(p), None)  # LRU 触点
        return data, ctype
    except Exception:
        return None


def _img_cache_remove(url: str):
    try:
        p, tp = _img_cache_paths(url)
        p.unlink(missing_ok=True)
        tp.unlink(missing_ok=True)
    except Exception:
        pass


def _img_cache_store(url: str, data: bytes, ctype: str):
    try:
        # 缓存层本身也拒绝旧版/未来调用方传入失败占位图，防止它重新变成
        # 永久成功缓存。
        if _is_cached_poster_placeholder(data):
            return
        _IMG_CACHE.mkdir(parents=True, exist_ok=True)
        p, tp = _img_cache_paths(url)
        p.write_bytes(data)
        tp.write_text(ctype[:64], encoding="ascii")
        # 简易容量控制：超限按 mtime 淘汰最旧一批
        entries = [e for e in _IMG_CACHE.iterdir() if e.name.endswith(".bin")]
        if len(entries) > IMG_CACHE_MAX_FILES:
            entries.sort(key=lambda e: e.stat().st_mtime)
            for old in entries[:IMG_CACHE_EVICT_BATCH]:
                try:
                    old.unlink(missing_ok=True)
                    old.with_suffix(".type").unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        pass  # 缓存写失败不影响响应

# ---- 图片长连接抓取池（2026-09-11 需求⑯ 海报提速）----
# urllib 每张图新建 DNS+TCP+TLS（冷图实测 1.1~1.4s/张，握手占大头；ThreadingHTTPServer
# 每 OkHttp 连接一个服务线程，线程生命周期与客户端 keep-alive 对齐）→ 线程本地
# http.client 连接复用，同主机后续请求实测 0.3~0.8s。失效连接（对端关闭）自动重建一次。
# vres.* 图片失败时按 hhkan.IMG_DOMAINS 轮换镜像（缓存键仍记原始 URL）。
_IMG_FETCH_TIMEOUT = 20
_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|bmp|avif)$", re.I)
_img_fetch_local = threading.local()


def _img_fetch_pooled(url: str, referer: str):
    """长连接 GET 图片 → (bytes, content_type)；连接失败丢弃重建一次。"""
    sp = urlparse(url)
    conns = getattr(_img_fetch_local, "conns", None)
    if conns is None:
        conns = {}
        _img_fetch_local.conns = conns
    key = (sp.scheme, sp.hostname, sp.port)

    def _once():
        conn = conns.get(key)
        if conn is None:
            conn = (http.client.HTTPSConnection if sp.scheme == "https"
                    else http.client.HTTPConnection)(
                sp.hostname, sp.port or (443 if sp.scheme == "https" else 80),
                timeout=_IMG_FETCH_TIMEOUT)
            conns[key] = conn
        path = sp.path or "/"
        if sp.query:
            path += "?" + sp.query
        headers = {"User-Agent": hhkan.UA, "Accept": "image/*,*/*;q=0.8"}
        if referer:                      # referer=None：某些图床/防盗链反而拒绝带 Referer
            headers["Referer"] = referer
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status not in (200, 206):
            raise RuntimeError("img HTTP %d" % resp.status)
        return data, (resp.getheader("Content-Type") or "application/octet-stream")

    try:
        return _once()
    except Exception:
        conns.pop(key, None)   # 连接疑似失效：丢弃重建一次
        return _once()


# 豆瓣图床：img1..img9.doubanio.com 是同一份存储的不同前端（路径完全通用），但会按
# Referer 做防盗链——第三方 Referer（含 hhkan 的）回 418/HTML，客户端 Coil 拿到非图
# 直接解码失败，表现为「海报整片不出图」。所以出站一律带豆瓣自己的 Referer，并在
# 各前端之间轮换：哪台可达用哪台。
DOUBAN_IMG_RE = re.compile(r"(?i)^https?://img\d+\.doubanio\.com/")
DOUBAN_IMG_HOSTS = ("img9.doubanio.com", "img1.doubanio.com", "img2.doubanio.com", "img3.doubanio.com")
DOUBAN_IMG_REFERER = "https://movie.douban.com/"
# asset.doubanio.com（默认海报/剧照）没有 imgN 镜像可轮换，且同样按 Referer 防盗链：
# 旧规则只认 img\d+.doubanio.com，导致默认海报带 hhkan Referer 抓到 HTML 直接判失败
# （线上日志：url=asset.doubanio.com/.../movie_default_large.png err=ctype=text/html）。
DOUBAN_IMG_ANY_RE = re.compile(r"(?i)^https?://([a-z0-9-]+\.)*douban(io)?\.com/")
# 保留旧版本失败占位图字节以便识别并清理设备上的历史磁盘缓存。新请求不再把这张
# 1×1 PNG 当成功图片返回；HTTP 失败会让 Coil 保持卡片底图并在之后重新请求。
PLACEHOLDER_POSTER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mPQ1jcDAAEZAJGsvJXRAAAAAElFTkSuQmCC")


def _is_cached_poster_placeholder(data: bytes) -> bool:
    return data == PLACEHOLDER_POSTER_PNG


def _img_fetch_with_mirrors(url: str):
    """vres.* 按 IMG_DOMAINS、豆瓣图床按 DOUBAN_IMG_HOSTS 轮换镜像抓取；
    其余域只试原地址。返回 (bytes, ctype)；失败返回 None，由调用方决定重试或返回错误。

    Referer 兜底：豆瓣系图床一律先带豆瓣 Referer（第三方 Referer 会被判防盗链回 HTML），
    再依次退到「不带 Referer」「hhkan Referer」；判定标准始终是 Content-Type 真的是图片，
    而不是 HTTP 200。失败返回 None，由代理返回 no-store 错误，避免 Coil 把占位图片当成功缓存。"""
    from urllib.parse import urlunsplit
    sp = urlparse(url)
    candidates = [url]
    is_douban = bool(DOUBAN_IMG_ANY_RE.match(url))
    if re.match(r"(?i)^https?://vres\.", url):
        for base in hhkan.IMG_DOMAINS:
            host = urlparse(base).netloc
            if host and host != sp.netloc:
                candidates.append(urlunsplit((sp.scheme, host, sp.path, sp.query, "")))
    elif DOUBAN_IMG_RE.match(url):
        for host in DOUBAN_IMG_HOSTS:
            if host != sp.netloc:
                candidates.append(urlunsplit((sp.scheme, host, sp.path, sp.query, "")))
    if is_douban:
        referers = [DOUBAN_IMG_REFERER, None, hhkan.BASE + "/"]
    else:
        referers = [hhkan.BASE + "/", None]
    last_err = ""
    last_referer = None
    for last_referer in referers:
        for cand in candidates:
            try:
                data, ctype = _img_fetch_pooled(cand, last_referer)
                if ctype and ctype.startswith("image/"):
                    return data, ctype
                last_err = "ctype=%s" % ctype
            except Exception as e:
                last_err = str(e)[:140]
                continue
    # 诊断留痕：失败原因只在这里可见（调用方只回 502，不写日志）。
    print("[img] 抓取失败 url=%s referer=%s err=%s" % (url[:96], last_referer, last_err or "no-candidate"),
          flush=True)
    return None


# ---- 数据接口响应级封面预取（2026-09-11 需求⑯）----
# 目录/筛选接口响应时，把前 N 张封面交给后台线程暖磁盘缓存——客户端 Coil 的图片请求
# 到达时大概率已命中（磁盘缓存命中实测 ~5ms vs 冷抓 1.1s+），海报墙「卡片先出、海报
# 迟迟不出」直接消解。环境变量 OTV_IMG_PREFETCH=0 可关（云端函数冻结线程无用时可关）。
_IMG_PREFETCH_N = 24
_img_prefetch_on = os.environ.get("OTV_IMG_PREFETCH", "1") != "0"
_img_prefetch_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="img-prefetch")
_img_prefetch_seen = set()
_img_prefetch_lock = threading.Lock()


def _img_prefetch_one(url: str):
    try:
        if _img_cache_load(url) is not None:
            return
        got = _img_fetch_with_mirrors(url)
        if got is not None and not _is_cached_poster_placeholder(got[0]):
            _img_cache_store(url, got[0], got[1])
    except Exception:
        pass  # 预取失败无副作用（客户端请求时走通用路径）


def prefetch_response_covers(obj):
    """respond() 缓存后调用：提取 items/sections/carousel 的图片 URL 前 24 张预取。"""
    if not _img_prefetch_on:
        return
    urls = []

    def _collect(items):
        for it in items or []:
            if len(urls) >= _IMG_PREFETCH_N:
                return
            c = (it or {}).get("cover") or ""
            if c and c.startswith("http") and _IMG_EXT_RE.search(urlparse(c).path):
                urls.append(c)

    try:
        for c in obj.get("carousel") or []:
            b = (c or {}).get("backdrop") or ""
            if b and b.startswith("http") and _IMG_EXT_RE.search(urlparse(b).path):
                urls.append(b)
        for sec in obj.get("sections") or []:
            _collect((sec or {}).get("items"))
        _collect(obj.get("items"))
    except Exception:
        return
    if not urls:
        return
    with _img_prefetch_lock:
        fresh = [u for u in urls if u not in _img_prefetch_seen]
        if len(_img_prefetch_seen) > 4000:
            _img_prefetch_seen.clear()
        _img_prefetch_seen.update(fresh)
    for u in fresh[:_IMG_PREFETCH_N]:
        _img_prefetch_pool.submit(_img_prefetch_one, u)

# 新闻素材图兜底用的队名清单（TEAM_EN 已覆盖 400+ 主流队，含中超）
NEWS_TEAM_NAMES = sorted(team_backdrop.TEAM_EN.keys(), key=len, reverse=True)


def _fetch_football_news(limit=12):
    """抓取直播吧足球资讯频道新闻（标题+链接+文章封面图）。
    返回 {"list": [{"title", "url", "img"}]}；失败返回空 list。
    ⚠️ 用 urllib.request 而非 fetch_url：urllib3 跟随 zhibo8 重定向会 "too many redirects"。
    封面图用线程池并发抓取（每条 ≤1.5s 超时），保证全部条目带素材图。
    """
    import time as _nt
    from concurrent.futures import ThreadPoolExecutor, as_completed
    now = _nt.time()
    if NEWS_CACHE["data"] and now - NEWS_CACHE["ts"] < 60:
        return {"list": NEWS_CACHE["data"][:limit]}
    items = []
    try:
        req = urlreq.Request("https://news.zhibo8.cc/zuqiu/",
                             headers={"User-Agent": UA, "Referer": "https://www.zhibo8.cc/"})
        with urlreq.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        pat = re.compile(r'<a[^>]*href="(//news\.zhibo8\.com/[^"]+)"[^>]*>\s*([^<]{8,60})\s*</a>')
        seen = set()
        for href, title in pat.findall(html):
            title = title.strip()
            if not title or title in seen:
                continue
            seen.add(title)
            if not any(k in title for k in ['球', '队', '赛', '杯', '联赛', '中超', '英超', '欧冠', '西甲', '意甲', '德甲', '法甲', 'U1', '女足']):
                continue
            items.append({"title": title, "url": "https:" + href, "img": ""})
            if len(items) >= limit:
                break

        def _grab(it):
            try:
                areq = urlreq.Request(it["url"], headers={"User-Agent": UA,
                                                          "Referer": "https://news.zhibo8.cc/zuqiu/"})
                with urlreq.urlopen(areq, timeout=8) as aresp:
                    ah = aresp.read().decode("utf-8", errors="ignore")
                # 封面图（正文首图）
                im = re.search(r'<img[^>]*src="(https?://[^"]*\.(?:jpg|png|jpeg|webp)[^"]*)"', ah)
                if im:
                    it["img"] = im.group(1)
                # 正文图片列表（content 区内）
                ci = ah.find('class="content"')
                seg = ah[ci:ci + 6000] if ci >= 0 else ah
                imgs = [u for u in re.findall(r'<img[^>]*src="([^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', seg, re.I)]
                imgs = [("https:" + u) if u.startswith("//") else u for u in imgs]
                imgs = [u for u in imgs if 'ico/video' not in u and 'bbsimg' not in u
                        and 'wenhuajingying' not in u and 'policy_icon' not in u and 'logo' not in u]
                if imgs:
                    it["content"] = imgs[:8]
                    if not it.get("img"):
                        it["img"] = imgs[0]
                # 视频 URL（video-url 属性）
                vm = re.search(r'video-url="(https?://[^"]+)"', seg)
                if vm:
                    it["video"] = vm.group(1).replace("&amp;", "&")
                # 时间
                tm = re.search(r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})', ah)
                if tm:
                    it["time"] = tm.group(1)
                # 正文文字（content 区内段落，过滤导航/版权/脚本噪音 + 网站尾部截断）
                body = re.sub(r'<script.*?</script>', '', seg, flags=re.S)
                body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
                body = re.sub(r'<[^>]+>', '\n', body)
                body = body.replace('&nbsp;', ' ')
                lines = [l.strip() for l in body.split('\n') if l.strip() and len(l.strip()) > 4]
                noise = ['版权', '举报', '合作网站', '手机直播吧', '网址导航', '友情链接', '联系我们',
                         '用户协议', '隐私政策', '报错反馈', '投诉反馈', 'var ', 'function', 'window.',
                         'document.', 'class=', 'style=', '广告', 'iframe', 'shouji.zhibo8']
                # 网站尾部（ICP/公安备案、电信许可证、创卫标语等）出现后，其后全部丢弃
                tail = re.compile(r'(ICP备|公网安备|B2-|网文[（(]|卫生城市|文明城市|'
                                  r'增值电信|Copyright|copyright|版权所有|All Rights)')
                cut = next((i for i, l in enumerate(lines) if tail.search(l)), len(lines))
                paras = [l for l in lines[:cut] if not any(k in l for k in noise)]
                if paras:
                    it["text"] = paras[:12]
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=min(6, max(1, len(items)))) as ex:
            list(ex.map(_grab, items))

        # 纯文字战报没有封面图：从标题里提队名，返回 team 字段供前端用队徽兜底
        for it in items:
            if it.get("img"):
                continue
            hit = None
            for tm in NEWS_TEAM_NAMES:
                if tm and tm in it["title"]:
                    hit = tm
                    break
            if hit:
                it["team"] = hit
    except Exception:
        items = []
    NEWS_CACHE.update(ts=now, data=items)
    return {"list": items}


# ===== 足球比赛集锦/录像（hhkan 搜索聚合，缓存 10 分钟） =====
VOD_FB_CACHE = {"ts": 0, "data": None}
VOD_FB_LEAGUES = ["英超", "西甲", "意甲", "德甲", "法甲", "中超", "欧冠",
                  "英冠", "葡超", "荷甲", "土超", "亚冠", "足总杯", "欧联"]


# 球队 → 所属联赛（基于 FB_CREST_IDS 分组注释；用于校正 hhkan 标错的 league，
# 例：狼队/埃弗顿是英超队，不应被"西甲"搜索词标成西甲）
_TEAM_LEAGUE = {}
try:
    _fb_body = re.search(
        r'FB_CREST_IDS\s*=\s*(\{.*?\n\})',
        Path(__file__).read_text(encoding="utf-8"),
        re.S,
    ).group(1)
    _cur = None
    for _ln in _fb_body.splitlines():
        if _ln.lstrip().startswith("#"):
            _cur = _ln.split("#")[-1].strip()
        else:
            for _t in re.findall(r'"([^"]*[\u4e00-\u9fa5][^"]*)"\s*:', _ln):
                if _cur and _t:
                    _TEAM_LEAGUE[_t] = _cur
except Exception:
    _TEAM_LEAGUE = {}


def _correct_league(item):
    """用集锦两队名校正 league：两队同属某联赛即用该联赛，否则保留原 league。"""
    home = item.get("home") or ""
    away = item.get("away") or ""
    hl = _TEAM_LEAGUE.get(home)
    al = _TEAM_LEAGUE.get(away)
    if hl and hl == al:
        item["league"] = hl



def _is_football_match_title(t):
    """hhkan 搜索结果里筛出真实比赛集锦/录像标题（含赛季/轮次/vs/日期特征）。"""
    if not t:
        return False
    if re.search(r"\d{3,4}赛季", t):
        return True
    if re.search(r"\bvs\b|VS", t, re.I):
        return True
    if re.search(r"第\d+轮", t):
        return True
    if re.search(r"\d+月\d+日", t):
        return True
    return False


def _team_from_video_title(t):
    """从集锦标题拆出两队名（用 crest 队名库匹配，避免把赛季/轮次当作队名）。
    返回 (home, away) 或 (None, None)。"""
    if not t:
        return None, None
    # 复用 crest 中文队名库（按长度倒序匹配，避免子串误配）
    names = sorted(set(team_backdrop.TEAM_EN.keys()) | set(FB_CREST_IDS.keys()),
                   key=len, reverse=True)
    found = []
    for nm in names:
        if nm and nm in t and nm not in found:
            found.append(nm)
    if len(found) < 2:
        return None, None
    # 按在标题中的出现位置排序，取前两个不同队
    found.sort(key=lambda nm: t.index(nm))
    return found[0], found[1]


def _attach_video_score(item, results):
    """给集锦 item 附加两队名（一直解析）+ 比分（两队同场命中才填，否则为空）。
    results: [{home,away,h,a}] 已结束比赛比分池。"""
    home, away = _team_from_video_title(item.get("title", ""))
    if not home or not away:
        return
    # 两队名一定展示（来自标题，即使无比分也像比赛卡）
    item["home"], item["away"] = home, away
    item["h"], item["a"] = "", ""
    for r in results or []:
        if (r.get("home") and r.get("away") and
            (home in r["home"] or r["home"] in home) and
                (away in r["away"] or r["away"] in away)):
            item["h"], item["a"] = r["h"], r["a"]
            return


def _fetch_football_videos(limit=12):
    """遍历主流联赛关键词搜索 hhkan，聚合并过滤出真实比赛集锦/录像。
    返回 {"list": [{id, title, cover, league}]}；失败返回空 list。
    hhkan 收录量有限（几十条），但均为真实可播 m3u8（前端经 /api/relay 中继播放）。
    各联赛关键词并行搜索（ThreadPoolExecutor），首次全量抓取压到几秒内。
    """
    import time as _vt
    from concurrent.futures import ThreadPoolExecutor
    now = _vt.time()
    if VOD_FB_CACHE["data"] and now - VOD_FB_CACHE["ts"] < 600:
        return {"list": VOD_FB_CACHE["data"][:limit]}

    def _search(league):
        out = {}
        try:
            token = hhkan.get_search_token()
            html = hhkan.get_page("/search?t=%s&k=%s&p=1" % (quote(token), quote(league)))
            for m in re.finditer(
                r'<a href="/detail/(\d+)\.html" class="search-result-item">.*?'
                r'data-original="([^"]+)".*?<div class="title">([^<]*)</div>',
                html, re.S):
                vid = int(m.group(1))
                title = m.group(3).strip()
                if not _is_football_match_title(title) or vid in out:
                    continue
                out[vid] = {"id": vid, "title": title,
                            "cover": hhkan.img_url(m.group(2)), "league": league}
        except Exception:
            pass
        return out

    pool = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(_search, VOD_FB_LEAGUES):
            pool.update(res)
    items = list(pool.values())
    # 附加真实比分：用集锦标题拆出两队，到 zhibo8 已结束比赛比分池匹配（匹配不到 h/a 留空）
    try:
        results = _fetch_zhibo8_results(limit=60).get("list", [])
        for it in items:
            _attach_video_score(it, results)
    except Exception:
        pass
    # 校正标错的联赛（狼队/埃弗顿应归英超，不被"西甲"搜索词误标）
    for it in items:
        _correct_league(it)
    VOD_FB_CACHE.update(ts=now, data=items)
    return {"list": items[:limit]}


# ===== 已结束比赛（带真实比分，来自 zhibo8 战报标题） =====
FB_RESULTS_CACHE = {"ts": 0, "data": None}


def _parse_zhibo8_score_title(title, name_list):
    """从 zhibo8 战报标题解析出 (home, away, h, a) 或 None。
    标题格式：'武汉三镇2比3上海海港' / '厦门飞鹭2-0江西庐山' / '南京城市主场1-1绝平定南赣联'。
    name_list 按长度倒序，优先匹配长队名避免子串误配。"""
    if not title:
        return None
    # 找比分（X比Y / X-Y / X:Y，X、Y ≤ 3 位数）
    m = re.search(r"(\d{1,3})\s*(?:比|[-:])\s*(\d{1,3})", title)
    if not m:
        return None
    h, a = int(m.group(1)), int(m.group(2))
    # 用已知队名匹配标题里两队
    found = []
    for tm in name_list:
        if tm and tm in title:
            found.append(tm)
    if len(found) < 2:
        return None
    # 去重 + 按出现位置排序，取两个不同队
    seen, ordered = set(), []
    for tm in found:
        if tm not in seen:
            seen.add(tm); ordered.append(tm)
    if len(ordered) < 2:
        return None
    home, away = ordered[0], ordered[1]
    return (home, away, h, a)


def _fetch_zhibo8_results(limit=12):
    """从直播吧足球新闻提取已结束比赛（标题带比分）。
    返回 {"list": [{home, away, h, a, title, url}]}；失败返回空 list。
    """
    import time as _rt
    now = _rt.time()
    if FB_RESULTS_CACHE["data"] and now - FB_RESULTS_CACHE["ts"] < 600:
        return {"list": FB_RESULTS_CACHE["data"][:limit]}
    items = []
    try:
        req = urlreq.Request("https://news.zhibo8.cc/zuqiu/",
                             headers={"User-Agent": UA, "Referer": "https://www.zhibo8.cc/"})
        with urlreq.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        pat = re.compile(r'<a[^>]*href="(//news\.zhibo8\.com/[^"]+)"[^>]*>\s*([^<]{8,60})\s*</a>')
        # 队名清单：TEAM_EN 的 key（覆盖主流）+ 比分标题常含的队名
        name_list = sorted(set(team_backdrop.TEAM_EN.keys()) | set(FB_CREST_IDS.keys()),
                           key=len, reverse=True)
        seen = set()
        for href, title in pat.findall(html):
            title = title.strip()
            if not title or title in seen:
                continue
            seen.add(title)
            parsed = _parse_zhibo8_score_title(title, name_list)
            if not parsed:
                continue
            home, away, h, a = parsed
            if not any(k in title for k in ['球', '队', '赛', '杯', '联赛', '中超', '英超', '西甲', '意甲', '德甲', '法甲', '女足', '欧冠']):
                continue
            items.append({"home": home, "away": away, "h": h, "a": a,
                          "title": title, "url": "https:" + href})
            if len(items) >= limit:
                break
    except Exception:
        items = []
    FB_RESULTS_CACHE.update(ts=now, data=items)
    return {"list": items}


# 新闻视频解析（腾讯云点播 tcplayer 尽力提取，命中率有限）
NEWS_VIDEO_CACHE = {}


def _resolve_news_video(tcplayer_url):
    """从 tcplayer 播放器页尽力解析视频 m3u8。
    腾讯云点播需签名接口，无签名时返回空；此函数尽力而为。"""
    if not (tcplayer_url.startswith("http://") or tcplayer_url.startswith("https://")):
        return ""
    if _blocked_internal_url(tcplayer_url):
        return ""
    if tcplayer_url in NEWS_VIDEO_CACHE:
        return NEWS_VIDEO_CACHE[tcplayer_url]
    url = ""
    try:
        req = urlreq.Request(tcplayer_url, headers={"User-Agent": UA,
                                                    "Referer": "https://news.zhibo8.com/"})
        with urlreq.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'https?://[^"\']*\.m3u8[^"\']*', html)
        if m:
            url = m.group(0)
    except Exception:
        url = ""
    NEWS_VIDEO_CACHE[tcplayer_url] = url
    return url


# 新闻图片代理缓存（duoduocdn 防盗链需 Referer，代理后浏览器可直读）
NEWS_IMG_CACHE = {}


def _proxy_news_image(url):
    """代理抓取新闻图片（带 zhibo8 Referer，防盗链）。返回 (data, mime) 或 (None, None)。"""
    if not (url.startswith("http://") or url.startswith("https://")):
        return None, None
    if _blocked_internal_url(url):
        return None, None
    if url in NEWS_IMG_CACHE:
        return NEWS_IMG_CACHE[url]
    data, mime = None, None
    try:
        req = urlreq.Request(url, headers={"User-Agent": UA,
                                           "Referer": "https://news.zhibo8.com/"})
        with urlreq.urlopen(req, timeout=10) as resp:
            data = resp.read()
            mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if data:
            NEWS_IMG_CACHE[url] = (data, mime)
            if len(NEWS_IMG_CACHE) > 400:
                for k in list(NEWS_IMG_CACHE)[:200]:
                    NEWS_IMG_CACHE.pop(k, None)
    except Exception:
        pass
    return data, mime


class Handler(SimpleHTTPRequestHandler):
    # HTTP/1.1 keep-alive：hls.js 分片请求高频（每分片一个请求），
    # 连接复用显著减少 TCP 握手开销（直播卡顿优化）。所有响应均带 Content-Length，安全。
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        # 网页版：前端在 www/ 子目录（proxy.py 在 pc-web/ 根）。
        # OTV_WEB_ROOT 与 web_entrypoint 共用同一个根，缓存表/健康字段也读它，
        # 避免「服务的目录」和「读 manifest 的目录」各说各话。
        super().__init__(*a, directory=str(_web_static_root()), **kw)

    # ---- 卡密激活同源转发（v1.25 需求②：网页版会员系统） ----
    # 直连云端 activate 会踩网关 CORS 头合并（函数回 * + 网关回显 Origin → "origin,*"
    # 非法头被浏览器拒收）；网页前端一律走同源 /api/activate，由服务端 urllib 转发。
    # 2026-09-16：后台迁回腾讯云 CloudBase（国内可直连），Sites 后端退役。
    _ACTIVATE_ENDPOINT = os.environ.get(
        "OTV_LICENSE_ENDPOINT",
        "https://appletv-d5ge1bth794873f76.service.tcloudbase.com/activate",
    )

    def _license_relay(self):
        """POST /api/activate {code,deviceId} → 云端 activate 响应原样回传。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            json.loads(raw)  # 非 JSON 直接 400（不透传垃圾到云端）
        except Exception:
            raw = b"{}"
        try:
            req = urlreq.Request(
                self._ACTIVATE_ENDPOINT, data=raw,
                headers={"Content-Type": "application/json"}, method="POST")
            with urlreq.urlopen(req, timeout=12) as resp:
                body = resp.read(64 * 1024)
                status = resp.status
        except urlreq.HTTPError as e:
            body = e.read(64 * 1024)
            status = e.code
        except Exception as e:
            body = json.dumps({"ret": -1, "msg": "relay_error:%s" % str(e)[:80]}).encode("utf-8")
            status = 200
        self.send_response(200)  # 云端错误也包 200+ret（前端按 ret 分支）
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/activate":
            self._license_relay()
            return
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
        football_route = (
            parsed.path == "/api/matches" or
            parsed.path.startswith("/api/stream/") or
            parsed.path == "/api/relay" or
            parsed.path.startswith("/api/relayts/")
        )
        enabled = football_route and (qs.get("football_vpn") or ["0"])[0] == "1"
        token = _UPSTREAM_PROXY_URL.set(FOOTBALL_PROXY_URL if enabled else None)
        try:
            self._do_get_with_upstream_proxy(parsed)
        finally:
            _UPSTREAM_PROXY_URL.reset(token)

    def _do_get_with_upstream_proxy(self, parsed):
        self._static_response = False
        if self._apk_update_manifest(parsed):
            return
        if parsed.path.startswith("/vod/") or parsed.path in ("/api/search", "/api/ime/hotwords"):
            self._vod_api(parsed)
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
        mm = re.match(r'^/api/stream/(\d+)$', parsed.path)
        if mm:
            qs = parse_qs(parsed.query)
            src = (qs.get("src") or ["a2"])[0]
            # fresh=1：绕过服务端成功缓存强制重新解析（静默续签/失败重试链路）
            fresh = (qs.get("fresh") or ["0"])[0].lower() in ("1", "true", "yes")
            # API 可读取本场源站清单中的任意已解析线路；不回落到旧 yoozb 插件线路。
            if src == "all":
                data, cached = resolve_88zhibo_streams(mm.group(1), fresh=fresh)
            else:
                data, cached = resolve_88zhibo_stream(mm.group(1), src, fresh=fresh)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Cache", "HIT" if cached else "MISS")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        mm = re.match(r'^/api/relayts/(\d+)$', parsed.path)
        if mm:
            # VLC 兜底引擎专用：HLS→连续 TS 直出（绕开 VLC adaptive 模块的 assert 崩溃，
            # 签名轮换由生成器内部自愈）。无 Content-Length 流式响应，连接关闭即结束。
            qs = parse_qs(parsed.query)
            src = (qs.get("src") or ["a2"])[0]
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                for chunk in relayts_generator(mm.group(1), src):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, GeneratorExit):
                return
            except Exception as e:
                try:
                    self.log_message("relayts error: %s" % str(e)[:120])
                except Exception:
                    pass
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
                if not rng:
                    served_body = None
                    cache_key = _relay_cache_key(u)
                    with _relay_lock:
                        hit = RELAY_CACHE.get(cache_key)
                        if hit and time.time() - hit[0] < RELAY_CACHE_TTL:
                            served_body = hit[3]
                    if served_body is not None:
                        # 注意：_prefetch_after 绝不能在 _relay_lock 内调用——
                        # Lock 不可重入，其内部的缓存查询会同线程死锁，
                        # 拖死整个中继（实测复现：全站分片请求超时）
                        self.send_response(200)
                        self.send_header("Content-Type", hit[2])
                        self.send_header("Cache-Control", "max-age=10")
                        self.send_header("Content-Length", str(len(served_body)))
                        self.end_headers()
                        try:
                            self.wfile.write(served_body)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        _prefetch_after(u, ref)  # 锁外触发：命中即按序消费 → 预取后继片
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
        if parsed.path == "/api/player-img":
            """球员头像：?name=（TheSportsDB searchplayers → strThumb 512×512）"""
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            url = _tdb_search_player(name)
            if url:
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                return
            self._send_pixel_png()
            return
        # ---------------- 影视（hhkan0.com，经 hhkan 模块） ----------------
        if parsed.path.startswith("/hhkan/"):
            self._hhkan_api(parsed)
            return
        # ---------------- 豆瓣刮削（高清海报 + 评分/简介） ----------------
        if parsed.path == "/api/douban":
            qs = parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                body = json.dumps({"error": "missing q"}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            data = douban_lookup(q)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/football-news":
            """足球热门新闻：抓直播吧足球资讯频道（news.zhibo8.cc/zuqiu/）。
            返回 {list: [{title, url, img}]}；图片抓不到时为空（前端兜底）。"""
            qs = parse_qs(parsed.query)
            n = _safe_int((qs.get("n") or ["12"])[0], 12, lo=1, hi=50)
            data = _fetch_football_news(n)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/news-img":
            """新闻图片代理：?u=（duoduocdn 防盗链需 Referer，代理后浏览器可读）。"""
            qs = parse_qs(parsed.query)
            u = (qs.get("u") or [""])[0]
            data, mime = _proxy_news_image(u) if u else (None, None)
            if data:
                self.send_response(200)
                self.send_header("Content-Type", mime or "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()
            return
        if parsed.path == "/api/news-video":
            """新闻视频解析：?u=（tcplayer 播放器 URL，尽力提取 m3u8）。"""
            qs = parse_qs(parsed.query)
            u = (qs.get("u") or [""])[0]
            url = _resolve_news_video(u)
            body = json.dumps({"url": url}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/football-videos":
            """足球比赛集锦/录像：?n=（hhkan 搜索聚合，过滤真实比赛标题）。
            返回 {list: [{id, title, cover, league}]}；前端点击 → /hhkan/detail/{id} 播放。"""
            qs = parse_qs(parsed.query)
            n = _safe_int((qs.get("n") or ["12"])[0], 12, lo=1, hi=50)
            data = _fetch_football_videos(n)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/football-results":
            """已结束比赛（带真实比分，zhibo8 战报）：?n=
            返回 {list: [{home, away, h, a, title, url}]}；前端回看往期卡显示比分。"""
            qs = parse_qs(parsed.query)
            n = _safe_int((qs.get("n") or ["12"])[0], 12, lo=1, hi=50)
            data = _fetch_zhibo8_results(n)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/scrape":
            """视频刮削：?title=&page=（hhkan 搜索+详情 + 16:9 海报 + 线路）"""
            qs = parse_qs(parsed.query)
            title = (qs.get("title") or [""])[0]
            page = _safe_int((qs.get("page") or ["1"])[0], 1, lo=1, hi=100)
            data = scraper.scrape(title, page)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/team-backdrop":
            """球队 16:9 海报：?name=（TheSportsDB fanart 1280×720）"""
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            url = team_backdrop.team_backdrop_url(name)
            body = json.dumps({"name": name, "backdrop": url,
                               "fallback": not bool(url)}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/team-backdrop-img":
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            url = team_backdrop.team_backdrop_url(name)
            if url:
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                return
            self._send_pixel_png()
            return
        if parsed.path == "/api/iptv":
            """网页版电视频道表：服务端按序拉取 IPTV 源（后台托管 latest 优先，
            上游镜像殿后）→ 解析为 {groups:[{name, channels:[{name, logo, urls}]}]}。
            服务端出站规避浏览器跨源（tcloudbasegateway / gh-proxy 均无 CORS 头），
            频道线路 10 分钟缓存（STALE 降级）。"""
            data = build_iptv_channels()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/sources":
            """数据源状态：直播源（当前/候选）、影视源域、播放器域名缓存。"""
            import hhkan as _hhkan
            live_status = []
            for u in SOURCE_MIRRORS:
                live_status.append({
                    "url": u,
                    "active": u == _active_source["url"],
                    "last_error": None,
                })
            body = json.dumps({
                "live": {"active": _active_source["url"], "mirrors": live_status},
                "vod": {"active": _hhkan.BASE, "domains": ["https://" + d for d in _hhkan.DOMAINS]},
                "player_host": PLAYER_HOST_CACHE.get("host"),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/health":
            # vod 字段（2026-09-18）：影视服务是否已预热。App 侧只把它当诊断信息，
            # 便于区分「后端没起来」和「后端起来了但影视还没就绪」。
            # assets 字段（2026-09-28）：当前内容寻址构建 + 归档保留的构建列表。
            # 归档降级（例如跳过损坏的历史档）必须在这里看得见，否则
            # compose healthcheck 与后台监控都无从发现「上一版本已丢失」。
            payload = {"ok": True, "time": time.strftime("%H:%M:%S"),
                       "vod": VOD_SERVICE is not None,
                       "assets": _web_asset_health()}
            # 桌面版（EXE）会注入这两个变量，让网页端只在 Windows 客户端里
            # 渲染版本号与「检查更新」；网页版不注入，行为与从前完全一致。
            if os.environ.get("OTV_DESKTOP"):
                payload["desktop"] = True
                payload["hotVersion"] = int(
                    os.environ.get("OTV_DESKTOP_HOT_VERSION") or 0)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._static_response = True   # 静态文件分支标记（end_headers 加缓存头用）
        return super().do_GET()

    def _vod_api(self, parsed):
        """统一豆瓣目录与播放源接口。"""
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            # 初始化失败必须回一份可读的 503，别让异常逃出处理器把响应整条丢掉。
            service = _get_vod_service()
            if path == "/api/ime/hotwords":
                payload = _get_ime_hotword_service().get()
            elif path == "/api/search":
                payload = service.search(
                    (query.get("q") or [""])[0],
                    page=_safe_int((query.get("page") or ["1"])[0], 1, lo=1, hi=100),
                    limit=_safe_int((query.get("limit") or ["30"])[0], 30, lo=1, hi=100))
            elif path == "/vod/home":
                refresh = (query.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")
                payload = service.home((query.get("category") or ["movie"])[0], refresh=refresh)
            elif path == "/vod/search":
                payload = service.search(
                    (query.get("q") or [""])[0],
                    _safe_int((query.get("page") or ["1"])[0], 1, lo=1, hi=100))
            elif path == "/vod/sources/status":
                payload = service.source_status()
            elif path == "/vod/sources/probe":
                # 设备侧探活（2026-09-30）：走**本机网络**探各 macms 源。
                # 后台的「测速」跑在 VPS 上，磐石云无境外出口，Cloudflare 段的源
                # 从 VPS 必然超时——2026-09-30 实测 10 个源有 8 个在后台显示「失败」，
                # 但同一批在设备网络下 7 个能正常取流。所以判断「源死没死」必须
                # 问设备，不能问 VPS。
                payload = vod_sources.probe_all_sources(
                    vod_sources.SourceRegistry.load_current().sources
                )
            elif path.startswith("/vod/filters/"):
                payload = service.filters(unquote(path[len("/vod/filters/"):]))
            elif path.startswith("/vod/show/"):
                payload = service.show(unquote(path[len("/vod/show/"):]), query)
            elif path == "/vod/resolve" or path.startswith("/vod/resolve/"):
                raw_mytv_id = path[len("/vod/resolve/"):] if path.startswith("/vod/resolve/") else ""
                mytv_id = unquote(raw_mytv_id)
                if not re.fullmatch(r"[0-9]+", mytv_id):
                    raise ValueError("mytv_id must be a positive integer")
                episode_values = query.get("episode")
                raw_episode = "1" if episode_values is None else episode_values[0]
                if len(episode_values or []) > 1 or not re.fullmatch(r"[0-9]+", raw_episode):
                    raise ValueError("episode must be a positive integer")
                episode = int(raw_episode)
                if episode < 1:
                    raise ValueError("episode must be a positive integer")
                payload = service.resolve(int(mytv_id), episode)
            elif path.startswith("/vod/detail/"):
                subject_id = unquote(path[len("/vod/detail/"):]).removeprefix("douban:")
                defer = (query.get("defer_sources") or ["0"])[0].lower() in ("1", "true", "yes")
                refresh = (query.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")
                payload = service.detail(subject_id, defer_sources=defer, refresh=refresh)
            elif path.startswith("/vod/play/"):
                parts = [unquote(part) for part in path[len("/vod/play/"):].split("/")]
                if len(parts) != 3:
                    raise ValueError("播放参数不完整")
                payload = service.play(parts[0].removeprefix("douban:"), parts[1],
                                       _safe_int(parts[2], 0, lo=0))
            else:
                self.send_error(404, "not found")
                return
            status = 200
        except (ValueError, LookupError) as exc:
            payload, status = {"ok": False, "error": str(exc)}, 400
        except Exception as exc:
            # The exception text carries internal topology (metadata-api host and
            # port, upstream URLs, cookies). Log it server-side, never echo it to
            # an anonymous caller.
            print("[%s] /vod api error: %s" % (
                time.strftime("%H:%M:%S"),
                re.sub(r"[\r\n\t]+", " ", str(exc))[:200]))
            payload, status = {"ok": False, "error": "vod unavailable"}, 503
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store" if (
            "/play/" in path or "/detail/" in path or path in ("/vod/resolve", "/api/ime/hotwords") or
            path.startswith("/vod/resolve/")
        ) else "max-age=30")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hhkan_api(self, parsed):
        """影视 API：/hhkan/home | /hhkan/channel/{cid} | /hhkan/latest | /hhkan/detail/{vid}
        | /hhkan/play/{vodid}/{pid}/{vid} | /hhkan/search?k= | /hhkan/proxy?u="""
        p = parsed.path
        qs = parse_qs(parsed.query)

        def q(name, default=None):
            v = qs.get(name)
            return v[0] if v else default

        # v1.22 网页版加载提速（2026-09-05 需求⑫）：目录类接口加进程内页缓存。
        # 旧版 /hhkan/* 每次实时抓源站 + 响应 no-store——云函数冷实例/弱网下进影视页、
        # 切分类都要等源站往返（秒级），且重复抓取易触发源站限流。
        # play/search/proxy 不缓存（签名 URL/实时性敏感）。
        cacheable = p == "/hhkan/home" or p.startswith((
            "/hhkan/channel/", "/hhkan/channel-sections/", "/hhkan/filters/",
            "/hhkan/show/", "/hhkan/latest", "/hhkan/detail/"))
        ckey = p + ("?" + parsed.query if parsed.query else "")

        def respond(obj, from_cache=False):
            if cacheable and not from_cache and obj.get("ok", True) is not False and "error" not in obj:
                # detail 存深拷贝：respond 之后启动的后台补线线程会原地改 d，
                # 共享引用会让并发读缓存与后台插键产生 dict-changed-during-
                # iteration 竞态（v1.32 修）；补线后的完整结果另走排名缓存。
                _HHKAN_PAGE_CACHE[ckey] = (time.time(),
                                           copy.deepcopy(obj) if p.startswith("/hhkan/detail/") else obj)
                if len(_HHKAN_PAGE_CACHE) > _HHKAN_PAGE_CACHE_MAX:
                    for k in sorted(_HHKAN_PAGE_CACHE, key=lambda k: _HHKAN_PAGE_CACHE[k][0])[:40]:
                        _HHKAN_PAGE_CACHE.pop(k, None)
                # 需求⑯：数据响应后后台暖封面磁盘缓存（客户端图片请求到达时大概率命中）
                try:
                    prefetch_response_covers(obj)
                except Exception:
                    pass
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            # 页缓存接口给浏览器 30s 复用（tab 往返不重拉）；其余（play/search/proxy）no-store
            self.send_header("Cache-Control", "max-age=30" if cacheable else "no-store")
            if cacheable:
                self.send_header("X-Cache", "HIT" if from_cache else "MISS")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # 命中检查须在 respond 闭包定义之后（否则 NameError——本地烟雾测试抓到）；
        # 筛选项接口按放宽 TTL 判过期（需求⑯），其余仍 90s
        if cacheable:
            # v1.32 性能：detail 优先吃「统一排名详情缓存」——补线+原生探测的完整
            # 结果 plain 与 ?enrich=1 同享，起播 enrich=1 不再重跑 ~8-10s 探测预算。
            if p.startswith("/hhkan/detail/"):
                try:
                    _dvid = int(p.rsplit("/", 1)[1])
                except ValueError:
                    _dvid = 0
                if _dvid:
                    ranked = _detail_ranked_get(_dvid)
                    if ranked is not None:
                        return respond(ranked, from_cache=True)
            hit = _HHKAN_PAGE_CACHE.get(ckey)
            ttl = (_HHKAN_PAGE_CACHE_TTL_FILTERS if p.startswith("/hhkan/filters/")
                   else _HHKAN_PAGE_CACHE_TTL)
            if hit and time.time() - hit[0] < ttl:
                return respond(hit[1], from_cache=True)

        try:
            if p == "/hhkan/home":
                try:
                    html = _hhkan_origin_call(lambda: hhkan.get_page("/"))
                    payload = {"sections": hhkan.parse_home(html),
                               "carousel": hhkan.parse_carousel(html),
                               "source": "hhkan", "stale": False}
                    if payload["sections"] or payload["carousel"]:
                        return respond(payload)
                except Exception as live_error:
                    print("hhkan home live failed: %s" % str(live_error)[:160])
                snapshot = _HHKAN_SNAPSHOT.home()
                if snapshot:
                    return respond(snapshot)
                raise RuntimeError("好好看实时首页与快照均不可用")
            if p.startswith("/hhkan/channel/"):
                cid = int(p.rsplit("/", 1)[1])
                try:
                    items = hhkan._parse_vod_list(_hhkan_origin_call(
                        lambda: hhkan.get_page("/channel/%d.html" % cid)))
                    if items:
                        return respond({"name": _HHKAN_CHANNELS.get(cid, str(cid)),
                                        "items": items, "source": "hhkan", "stale": False})
                except Exception as live_error:
                    print("hhkan channel %s live failed: %s" % (cid, str(live_error)[:160]))
                snapshot = _HHKAN_SNAPSHOT.channel(cid)
                if snapshot:
                    return respond(snapshot)
                raise RuntimeError("好好看实时频道与快照均不可用")
            if p.startswith("/hhkan/channel-sections/"):
                """分类页固定三栏目：好好看是唯一卡片目录源。"""
                cid = int(p.rsplit("/", 1)[1])
                primary = []
                try:
                    primary = hhkan.parse_channel_sections(_hhkan_origin_call(
                        lambda: hhkan.get_page("/channel/%d.html" % cid)))
                except Exception as e:
                    print("hhkan channel-sections %s failed: %s" % (cid, str(e)[:160]))
                if not primary:
                    snapshot = _HHKAN_SNAPSHOT.channel_sections(cid)
                    if snapshot:
                        return respond(snapshot)
                # 其他信号源只在详情页提供可选播放线路；目录为空时保持三栏空态，
                # 绝不以外部片单补卡、改排序或伪装成当前分类。
                return respond({
                    "cid": cid,
                    "source": "hhkan" if primary else "none",
                    "supplemented": False,
                    "sections": hhkan.complete_channel_sections(primary),
                })
            if p.startswith("/hhkan/filters/"):
                cid = int(p.rsplit("/", 1)[1])
                try:
                    filters = _hhkan_origin_call(lambda: hhkan.get_filters(cid))
                    if filters.get("types"):
                        filters.update({"source": "hhkan", "stale": False})
                        return respond(filters)
                except Exception as live_error:
                    print("hhkan filters %s live failed: %s" % (cid, str(live_error)[:160]))
                snapshot = _HHKAN_SNAPSHOT.filters(cid)
                if snapshot:
                    return respond(snapshot)
                return respond({"types": [], "areas": [], "langs": [], "years": []})
            if p.startswith("/hhkan/show/"):
                cid = _safe_int(p.rsplit("/", 1)[1], 0, lo=0)  # cid=0 → 全站"全部"
                # 筛选：type/area/lang/year/by/page（缺省「全部」/最热/第一页）
                page = _safe_int(q("page", "1"), 1, lo=1, hi=100)
                by = q("by", "") or "3"
                ftype, farea, flang, fyear = q("type", ""), q("area", ""), q("lang", ""), q("year", "")
                if cid == 0 and any((ftype, farea, flang, fyear)):
                    # 需求 影视#8：源站 /show/0-{筛选} 语义残缺（无论怎么筛只回个位数影片）——
                    # 全站筛选按 5 个频道逐个查询后合并去重（并行抓取，按频道序稳定排序），
                    # 各频道结果与源站单频道筛选页完全一致；has_more = 任一频道还有下一页
                    import concurrent.futures as _cf
                    chans = list(_HHKAN_CHANNELS.keys())

                    def _chan_page(c, pg):
                        try:
                            return _hhkan_origin_call(lambda: hhkan.get_show_page(
                                c, type_=ftype, area=farea, lang=flang,
                                year=fyear, page=pg, by=by))
                        except Exception:
                            snapshot = _HHKAN_SNAPSHOT.show(
                                c, by=by, page=pg, type_=ftype, area=farea, lang=flang, year=fyear)
                            return (snapshot or {}).get("items", [])
                    with _cf.ThreadPoolExecutor(max_workers=5) as ex:
                        cur = {c: ex.submit(_chan_page, c, page) for c in chans}
                        nxt = {c: ex.submit(_chan_page, c, page + 1) for c in chans}
                        merged, seen = [], set()
                        for c in chans:  # 固定频道序（电影→电视剧→动漫→综艺→短剧）稳定排序
                            for it in cur[c].result():
                                if it["id"] not in seen:
                                    seen.add(it["id"])
                                    merged.append(it)
                        more = any(nxt[c].result() for c in chans)
                    return respond({"cid": cid, "page": page, "items": merged, "has_more": more})
                # 2026-09-11 需求⑯（筛选/结果提速）：当前页与 has_more 探测页【并行】抓取
                # （旧版串行 = 每次筛选/翻页双倍往返，源站可达时 0.3s/页 ×2）；下一页结果
                # 直接写入页缓存（键与客户端翻页请求完全一致）→ 触底「加载更多」命中缓存
                # 秒回，仅再往后预取一页。
                try:
                    # 注意形参名：hhkan.get_show_page 的语言参数是 lang（旧代码传 lang_
                    # 会 TypeError → live 筛选查询全挂、只剩快照兜底，2026-09-11 实测修复）
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                        f_cur = ex.submit(lambda: _hhkan_origin_call(lambda: hhkan.get_show_page(
                            cid, type_=ftype, area=farea, lang=flang, year=fyear,
                            page=page, by=by)))
                        f_next = ex.submit(lambda: _hhkan_origin_call(lambda: hhkan.get_show_page(
                            cid, type_=ftype, area=farea, lang=flang, year=fyear,
                            page=page + 1, by=by)))
                    items = f_cur.result()
                    next_items = f_next.result()
                    more = bool(next_items)
                    if next_items:
                        nkey = ("/hhkan/show/%d?page=%d&by=%s&type=%s&area=%s&lang=%s&year=%s"
                                % (cid, page + 1, quote(by), quote(ftype),
                                   quote(farea), quote(flang), quote(fyear)))
                        _HHKAN_PAGE_CACHE[nkey] = (time.time(), {
                            "cid": cid, "page": page + 1, "items": next_items,
                            "has_more": True, "source": "hhkan", "stale": False,
                        })
                    return respond({"cid": cid, "page": page, "items": items,
                                    "has_more": more, "source": "hhkan", "stale": False})
                except Exception as live_error:
                    print("hhkan show %s live failed: %s" % (cid, str(live_error)[:160]))
                    # 注意形参名：SnapshotCatalog.show 的语言参数是 lang（旧代码传 lang_
                    # 会 TypeError → 快照兜底整体 500，快照模式下「筛选结果加载慢且空」
                    # 的直接来源，2026-09-11 实测修复）
                    snapshot = _HHKAN_SNAPSHOT.show(
                        cid, by=by, page=page, type_=ftype, area=farea, lang=flang, year=fyear)
                    if snapshot:
                        return respond(snapshot)
                    raise
            if p == "/hhkan/latest":
                page = int(q("page", "1"))
                path = "/label/new.html" if page <= 1 else "/new/%d.html" % page
                try:
                    items = hhkan._parse_vod_list(_hhkan_origin_call(lambda: hhkan.get_page(path)))
                    if items:
                        return respond({"page": page, "items": items,
                                        "source": "hhkan", "stale": False})
                except Exception as live_error:
                    print("hhkan latest live failed: %s" % str(live_error)[:160])
                snapshot = _HHKAN_SNAPSHOT.latest(page)
                if snapshot:
                    return respond(snapshot)
                raise RuntimeError("好好看实时更新页与快照均不可用")
            if p.startswith("/hhkan/detail/"):
                vid = int(p.rsplit("/", 1)[1])
                try:
                    d = hhkan.parse_detail(_hhkan_origin_call(
                        lambda: hhkan.get_page("/detail/%d.html" % vid)), vid)
                except Exception as live_error:
                    print("hhkan detail %s live failed: %s" % (vid, str(live_error)[:160]))
                    d = _HHKAN_SNAPSHOT.detail(vid) or {}
                if not d.get("title"):
                    return respond({"ok": False, "error": "未找到该影片"})
                # 需求⑮：mcms 池同名影片补线（≥720p 实测清晰度线路插最前）。
                # 默认请求给详情页快速返回好好看主数据；播放器起播前使用
                # ?enrich=1 同步等待完整补线。这避免客户端把首次的基础响应缓存后，
                # 永远看不到新增播放源。
                if q("enrich", "0") == "1":
                    enrich_detail_sources(d, vid)
                else:
                    threading.Thread(target=enrich_detail_sources, args=(d, vid), daemon=True).start()
                return respond(d)
            if p.startswith("/hhkan/play/"):
                _, _, _, vodid, pid, vid = p.split("/")
                fresh_args = {"fresh": True} if q("fresh", "0") == "1" else {}
                if int(pid) >= 100:
                    # 需求⑮：跨站补线（pid=100+站*50+组下标）——优先于 mcms 降级判定，
                    # 防止 hhkan 恰在 detail 与 play 之间失联时 pid 被钳到错误线路
                    try:
                        return respond(mcms_enriched_play(int(vodid), int(pid), int(vid), **fresh_args))
                    except Exception as e:
                        return respond({"line": "", "sources": [], "error": str(e)[:200]})
                try:
                    # 普通换集复用短期解析缓存；手动切源/重试需重新取得直链，
                    # 不能继续返回已卡住或过期的地址。
                    _ppayload = _hhkan_native_play(
                        int(vodid), int(pid), int(vid), fresh=q("fresh", "0") == "1")
                    if _ppayload is not None:
                        return respond(_ppayload)
                except Exception as live_error:
                    print("hhkan play %s live failed: %s" % (vodid, str(live_error)[:160]))
                # 旧缓存/旧客户端仍可能请求好好看 pid；源站受风控时按同片同集切到
                # 补充播放源。卡片与详情元数据仍来自快照中的好好看数据。
                fallback_detail = _HHKAN_SNAPSHOT.detail(int(vodid)) or {}
                enrich_detail_sources(fallback_detail, int(vodid))
                for line in fallback_detail.get("sources") or []:
                    episodes = line.get("episodes") or []
                    if episodes and int(episodes[0].get("pid", 0)) >= 100:
                        episode = episodes[max(0, min(int(vid), len(episodes) - 1))]
                        return respond(mcms_enriched_play(
                            int(vodid), int(episode["pid"]), int(episode["vid"]), **fresh_args))
                return respond({"line": "", "sources": [], "error": "可用播放线路暂不可达"})
            if p == "/hhkan/search":
                k = q("k", "")
                if not k:
                    return respond({"keyword": k, "items": []})
                # 搜索结果缓存：非空 10 分钟，空结果 20 秒。r=N（app 重试序号）参与缓存键——
                # 否则空结果缓存会把 app 的重试也吞掉（2026-08-29 需求 搜索#2）
                ck = (k, q("page", "1"), q("r", "0"))
                now_s = time.time()
                hit = _SEARCH_CACHE.get(ck)
                if hit:
                    ttl = 600 if hit[1] else 20
                    if now_s - hit[0] < ttl:
                        return respond({"keyword": k, "items": hit[1]})
                # 节流上游搜索（2026-08-29：app 逐键前缀搜索曾触发源站 429 限流，
                # 之后所有请求全空 = app「搜不出来」根因）。缓存命中不排队，未命中串行 + ≥1.5s 间隔。
                try:
                    items = _hhkan_origin_call(lambda: _search_throttled_fetch(k, q("page", "1")))
                except Exception as live_error:
                    print("hhkan search live failed: %s" % str(live_error)[:160])
                    items = _HHKAN_SNAPSHOT.search(k, q("page", "1"))
                if len(_SEARCH_CACHE) > 200:
                    _SEARCH_CACHE.clear()
                _SEARCH_CACHE[ck] = (now_s, items)
                return respond({"keyword": k, "items": items})
            if p == "/hhkan/proxy":
                return self._hhkan_proxy(q("u", ""))
            return respond({"ok": False, "error": "未知接口"})
        except Exception as e:
            try:
                respond({"ok": False, "error": str(e)[:200]})
            except Exception:
                pass

    def _hhkan_proxy(self, url):
        """影视播放代理：m3u8 全量重写（分片/密钥指回本代理），分片带 Range 转发。
        需求 影视#1：图片（海报/队标封面）带本地磁盘缓存——第二次加载零上游请求，
        海报墙滚动加载明显提速；m3u8/分片不缓存。"""
        if not (url.startswith("http://") or url.startswith("https://")):
            body = b"bad url"
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if _blocked_internal_url(url):
            body = b"blocked"
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        rng = self.headers.get("Range")
        is_douban_image = bool(DOUBAN_IMG_ANY_RE.match(url))

        def send_image_unavailable():
            # 失败不能以 200 + 可解码占位图返回：Coil 会把它写入自己的磁盘缓存，
            # 上游恢复后同 URL 仍可能长期显示空海报。no-store 同时约束 HTTP 中间缓存。
            body = b"poster temporarily unavailable"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---- 图片磁盘缓存命中（仅整文件 GET，Range/流媒体不走缓存）----
        if not rng:
            hit = _img_cache_load(url)
            if hit is not None:
                data, ctype = hit
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        # ---- 图片长连接快路径（2026-09-11 需求⑯）：可识别图片扩展名的整文件 GET
        #      走线程本地 keep-alive 池 + vres 镜像轮换（urllib 逐张新建 TLS 是海报
        #      加载慢的主根因）；失败回落下方通用路径（urllib 重试 + clash 兜底）----
        if not rng and _IMG_EXT_RE.search(urlparse(url).path):
            got = _img_fetch_with_mirrors(url)
            if got is not None:
                data, ctype = got
                if _is_cached_poster_placeholder(data):
                    _img_cache_remove(url)
                    return send_image_unavailable()
                _img_cache_store(url, data, ctype)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if is_douban_image:
                return send_image_unavailable()
        headers = {"User-Agent": hhkan.UA,
                   "Referer": DOUBAN_IMG_REFERER if is_douban_image else hhkan.BASE + "/"}
        if rng:
            headers["Range"] = rng
        try:
            # url 已在入口经 _blocked_internal_url（host 字符串）校验，
            # 出站前再做 _assert_public_http_url（DNS 解析结果全公网）校验。
            req = urlreq.Request(url, headers=headers)
            _assert_public_http_url(url)
            try:
                resp = urlreq.urlopen(req, timeout=30)
            except Exception:
                # 源站 CDN 首连常瞬断（实测同一 URL 前一请求 502、1ms 后重试即 200；
                # 全部页冷图并发时首连失败集中出现 = 「筛选后很多封面没有」根因）：
                # 直连失败立即原地重试一次（重连前复检，防 TTL 内 DNS 漂移）再走本机代理兜底
                _assert_public_http_url(url)
                try:
                    resp = urlreq.urlopen(req, timeout=30)
                except Exception:
                    resp = None
            if resp is None:
                # 直连不可达（源站 CDN 常被墙/仅代理可达）：走本机代理兜底（127.0.0.1:7890，环境变量 HHKAN_PROXY 可覆盖）
                import os as _os
                _px = _os.environ.get("HHKAN_PROXY", "http://127.0.0.1:7890")
                _opener = urlreq.build_opener(urlreq.ProxyHandler({"http": _px, "https": _px}))
                resp = _opener.open(req, timeout=30)
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            if is_douban_image and not ctype.lower().startswith("image/"):
                try:
                    resp.close()
                except Exception:
                    pass
                return send_image_unavailable()
            is_m3u8 = "mpegurl" in ctype or "m3u8" in url
        except Exception as e:
            body = ("proxy fail: %s" % str(e)[:200]).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if is_m3u8:
            text = resp.read().decode("utf-8", "ignore")
            out = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#EXT-X-KEY") and "URI=" in line:
                    m = re.search(r'URI="([^"]+)"', line)
                    if m:
                        key = m.group(1)
                        key_url = key if key.startswith("http") else urljoin(url, key)
                        line = line.replace('URI="%s"' % key,
                                            'URI="hhkan/proxy?u=%s"' % quote(key_url, safe=""))
                    out.append(line)
                    continue
                if line.startswith("#"):
                    out.append(line)
                    continue
                seg = line if line.startswith("http") else urljoin(url, line)
                out.append("hhkan/proxy?u=%s" % quote(seg, safe=""))
            body = ("\n".join(out)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 媒体分片：转发（保留 Range 语义）
        data = resp.read()
        if is_douban_image and _is_cached_poster_placeholder(data):
            return send_image_unavailable()
        status = resp.status if resp.status in (200, 206) else 200
        # 图片（海报/封面）落磁盘缓存：下次同 URL 直接本地回源（需求 影视#1）
        if status == 200 and not rng and ctype.startswith("image/") and len(data) < 8 * 1024 * 1024:
            _img_cache_store(url, data, ctype)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=600")
        self.send_header("Content-Length", str(len(data)))
        if "Content-Range" in resp.headers:
            self.send_header("Content-Range", resp.headers["Content-Range"])
        self.end_headers()
        self.wfile.write(data)

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
        # CORS：安卓 WebView 以 file:// 加载前端（origin 为 null），
        # fetch 到 http://127.0.0.1:8090 属跨源，必须允许（桌面同源访问不受影响）
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt, *args):
        request_target = str(getattr(self, "path", "") or "")
        if not request_target:
            requestline = str(getattr(self, "requestline", "") or "")
            requestline_parts = requestline.split()
            if len(requestline_parts) >= 2:
                request_target = requestline_parts[1]
        try:
            route = urlparse(request_target).path
        except ValueError:
            route = request_target
        route = route.split("?", 1)[0]
        if (route == "/vod/resolve" or route.startswith("/vod/resolve/") or
                "/vod/resolve/" in route):
            status = next((str(value) for value in args
                           if isinstance(value, int) and 100 <= value <= 599), None)
            if status is None:
                status = next((str(value) for value in args
                               if isinstance(value, str) and re.fullmatch(r"[1-5][0-9]{2}", value)),
                              "unknown")
            # The method comes from the raw request line. Keep letters only so a
            # crafted line such as "GE\x1b]0;PWNED\x07T" cannot inject terminal
            # escape sequences (or a fake log line) into stdout.
            method = (re.sub(r"[^A-Za-z]", "", str(getattr(self, "command", "") or ""))
                      .upper()[:12] or "GET")
            print("[%s] \"%s /vod/resolve/:mytv_id\" %s" % (
                time.strftime("%H:%M:%S"), method, status))
            return
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))


# ---------------- 网页版电视频道表（/api/iptv，2026-09-04 v1.21） ----------------
# 与 app IptvRepository.SOURCES 同源：线上主后端托管 latest（2026-09-17 起为 CloudBase
# appletv-d5ge1bth794873f76，自建站点对象存储已退役）优先，上游每日校验列表殿后。
# 服务端出站无跨源限制，客户端拿到即可播（线路经 /api/relay）。
_IPTV_LOCAL_ENDPOINT = os.environ.get("OTV_IPTV_LATEST_URL", "").strip()
IPTV_SOURCES = [
    _IPTV_LOCAL_ENDPOINT or "https://appletv-d5ge1bth794873f76.service.tcloudbase.com/iptv/latest",
    "https://gh-proxy.com/raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://gh-proxy.com/raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
]
IPTV_GROUP_ORDER = ["央视", "卫视", "体育", "地方", "数字付费", "港澳台", "美国/英国"]
_iptv_cache = {"ts": 0.0, "data": None, "lock": threading.Lock()}
_IPTV_TTL = 10 * 60
_IPTV_MAX_LINES = 8   # 单频道线路上限（与 app MAX_LINES_PER_CHANNEL 一致）


def _iptv_fetch_m3u(timeout=15.0):
    """按序拉取 IPTV 源，返回 (m3u文本, 源url)；全部失败返回 (None, None)。
    SSRF 防护（与 _hhkan 白名单同款口径）：仅 https + 域名白名单（源列表自身），
    且 DNS 解析结果全公网（阻断私网/环回/保留段与 DNS rebinding），按 host 缓存 60s。"""
    import socket
    import ipaddress
    allowed_hosts = set()
    for src in IPTV_SOURCES:
        h = (urlparse(src).hostname or "").lower()
        if h:
            allowed_hosts.add(h)

    def _host_public(host):
        now = time.time()
        cached = _dns_public_cache.get(host)
        if cached is not None and now - cached[0] < 60:
            return cached[1]
        ok = True
        try:
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if (ip.is_private or ip.is_loopback or ip.is_link_local or
                        ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                    ok = False
                    break
        except Exception:
            ok = False
        _dns_public_cache[host] = (now, ok)
        return ok

    for u in IPTV_SOURCES:
        pr = urlparse(u)
        host = (pr.hostname or "").lower()
        trusted_local = (
            bool(_IPTV_LOCAL_ENDPOINT)
            and u == _IPTV_LOCAL_ENDPOINT
            and pr.scheme == "http"
            and host == "business-api"
        )
        if host not in allowed_hosts or (
                not trusted_local and (pr.scheme != "https" or not _host_public(host))):
            continue
        try:
            req = urlreq.Request(u, headers={"User-Agent": UA})
            with urlreq.urlopen(req, timeout=timeout) as r:
                raw = r.read(24 * 1024 * 1024).decode("utf-8", "replace")
            if "#EXTM3U" in raw:
                return raw, u
        except Exception:
            continue
    return None, None


def build_iptv_channels():
    """m3u → 分组频道表（同名聚合多线路，组序=GROUP_ORDER 感知）。缓存 10 分钟，
    拉取失败降级返回旧缓存（带 stale:true），再失败返回空表。"""
    now = time.time()
    with _iptv_cache["lock"]:
        if _iptv_cache["data"] and now - _iptv_cache["ts"] < _IPTV_TTL:
            return _iptv_cache["data"]
    raw, src = _iptv_fetch_m3u()
    if raw is None:
        if _iptv_cache["data"]:
            out = dict(_iptv_cache["data"])
            out["stale"] = True
            return out
        return {"ret": 1, "error": "IPTV 源暂不可达", "groups": [], "channels": 0}
    by_key, order = {}, []
    name = logo = group = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = line[line.rfind(",") + 1:].strip()
            m = re.search(r'tvg-logo="([^"]*)"', line)
            logo = m.group(1) if m else ""
            m = re.search(r'group-title="([^"]*)"', line)
            group = (m.group(1) if m else "").strip()
        elif line.startswith(("http://", "https://")):
            if name and line:
                key = group + "|" + name
                ch = by_key.get(key)
                if ch is None:
                    ch = {"name": name, "logo": logo, "urls": []}
                    by_key[key] = ch
                    order.append((group, ch))
                if len(ch["urls"]) < _IPTV_MAX_LINES and line not in ch["urls"]:
                    ch["urls"].append(line)
            name = logo = group = ""
    groups = {}
    for g, ch in order:
        groups.setdefault(g or "其他", []).append(ch)

    def gsort(g):
        for i, kw in enumerate(IPTV_GROUP_ORDER):
            if kw in g:
                return (i, g)
        return (len(IPTV_GROUP_ORDER), g)

    out = {"ret": 0, "source": src, "stale": False,
           "channels": sum(len(v) for v in groups.values()),
           "groups": [{"name": k, "channels": groups[k]} for k in sorted(groups, key=gsort)]}
    with _iptv_cache["lock"]:
        _iptv_cache["ts"] = now
        _iptv_cache["data"] = out
    return out


# ---------------- 好好看详情补线池（macCMS 聚合 API） ----------------
# 产品边界：好好看是唯一目录/卡片/详情主数据源；下列站点只参与同名影片补充播放线路，
# 不得进入 /hhkan/home|channel|latest|search|show|channel-sections 的卡片响应。
#
# 2026-09-08 需求⑮「增加影视源，自动切换速度最快清晰度最高最稳定的源」：
# - 2026-09-09 按 tools/out/vodsource_probe/影视源推荐清单_MCMS_SITES.md 替换补线池；
#   移除已劣化的 fdzys，接入 8 个 T1 站。站点按 API 实测响应速度排序，失败自动降权。
# - 线路级「清晰度」：线路名提示（1080/蓝光/超清…）+ 真实探测（取该线第 1 集 master
#   m3u8 读 RESOLUTION，并行 2.5s 超时，URL 级 10min 缓存）→ 线路排序清晰度优先。
# - hhkan 详情【跨站补线】：detail 响应追加 mcms 池同名影片线路（pid≥100 命名空间），
#   三端 UI 零改动看到更多线路；高清晰度（≥720p 实测）线路排到 hhkan 线路之前 = 自动选最优。
# - 同批修复 mcms_play 忽略集数参数的存量 bug（此前恒返回整组 episodes，
#   前端取 sources[0] 会永远播第 1 集）。
# T2、成人内容站、HTTP-only/失效站以及历史浏览器型站点均不注册。
MCMS_SITES = [
    {"name": "bfzy", "api": "https://bfzyapi.com/api.php/provide/vod/"},
    {"name": "jszy", "api": "https://jszyapi.com/api.php/provide/vod/"},
    {"name": "jinying", "api": "https://jyzyapi.com/provide/vod/from/jinyingm3u8/"},
    {"name": "huya", "api": "https://www.huyaapi.com/api.php/provide/vod/"},
    {"name": "hhzy", "api": "https://hhzyapi.com/api.php/provide/vod/"},
    {"name": "hongniu", "api": "https://www.hongniuzy2.com/api.php/provide/vod/"},
    {"name": "subo", "api": "https://subocaiji.com/api.php/provide/vod/"},
    {"name": "360zy", "api": "https://360zy.com/api.php/provide/vod/"},
]
MCMS_API = MCMS_SITES[0]["api"]  # 兼容旧引用（域名白名单兜底）
MCMS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# 站点测速排序缓存：{"ts": 秒, "order": [站点下标…健康在前按速度], "healthy": {i: bool}}
_mcms_site_cache = {"ts": 0.0, "order": list(range(len(MCMS_SITES))), "healthy": {}}
_mcms_pool_lock = threading.Lock()
# 各站分类树不同；映射仅供内部诊断/强制模式使用，正式目录始终来自好好看。
MCMS_CHANNELS = {1: "电影", 2: "电视剧", 3: "动漫", 4: "综艺"}
MCMS_LEAVES_BY_SITE = {
    "bfzy":    {1: [20, 21, 22, 23, 24, 25, 26, 27, 28], 2: [30, 31, 32, 33, 34, 35, 36, 37, 38], 3: [40, 41, 42, 43, 44], 4: [45]},
    "jszy":    {1: [9, 10, 11, 12, 13, 14, 15, 16], 2: [3, 4, 5, 6, 7, 20, 28], 3: [23, 24, 25, 26], 4: [30, 31, 32, 33]},
    "jinying": {1: [9, 10, 11, 12, 13, 14, 15, 16], 2: [3, 4, 5, 6, 7, 20, 28], 3: [23, 24, 25, 26], 4: [36, 37, 38, 39]},
    "huya":    {1: [9, 10, 11, 12, 13, 14, 15, 16], 2: [3, 4, 5, 6, 7, 20, 28], 3: [23, 24, 25, 26], 4: [38, 39, 40, 41]},
    "hhzy":    {1: [9, 10, 11, 12, 13, 14, 15, 16], 2: [3, 4, 5, 6, 7, 20, 28], 3: [23, 24, 25, 26], 4: [30, 31, 32, 33]},
    "hongniu": {1: [5, 6, 7, 8, 9, 10, 11, 19], 2: [12, 13, 14, 15, 16, 17, 18], 3: [36, 37, 38], 4: [39, 40, 41, 42]},
    "subo":    {1: [6, 7, 8, 9, 10, 11, 12], 2: [14, 15, 16, 17, 18, 20, 21], 3: [24, 25, 26], 4: [33, 34, 35, 36]},
    "360zy":   {1: [5, 6, 7, 8, 9, 10, 11, 12], 2: [13, 14, 15, 16], 3: [38, 39, 40], 4: [34, 35, 36, 37]},
}
_vod_mode_cache = {"ts": 0.0, "mcms": False}
_VOD_MODE_TTL = 300


def _mcms_fetch_site(si, params, timeout=20.0):
    """单站 GET（si = MCMS_SITES 下标）。SSRF 防护与旧版同口径：
    仅 https + 该站 api 域名白名单，DNS 解析结果全公网。"""
    import socket
    import ipaddress
    site = MCMS_SITES[si]
    qs = "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in params.items())
    u = site["api"] + "?" + qs
    pr = urlparse(u)
    host = (pr.hostname or "").lower()
    if pr.scheme != "https" or host != urlparse(site["api"]).hostname.lower():
        raise RuntimeError("mcms url not allowed: %s" % host)
    now = time.time()
    cached = _dns_public_cache.get(host)
    if cached is not None and now - cached[0] < 60:
        pub = cached[1]
    else:
        pub = True
        try:
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if (ip.is_private or ip.is_loopback or ip.is_link_local or
                        ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                    pub = False
                    break
        except Exception:
            pub = False
        _dns_public_cache[host] = (now, pub)
    if not pub:
        raise RuntimeError("mcms host resolves to non-public address")
    req = urlreq.Request(u, headers={"User-Agent": MCMS_UA})
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(8 * 1024 * 1024).decode("utf-8", "replace"))


def _mcms_site_order(max_age=300.0):
    """站点测速排序（健康站按实测耗时升序，故障站殿后；缓存 5 分钟）。
    并行探测各站最小列表请求；探测失败不剔除（仅排尾），下次仍有机会。"""
    now = time.time()
    if now - _mcms_site_cache["ts"] < max_age:
        return _mcms_site_cache["order"]

    def _probe(si):
        t0 = time.time()
        try:
            d = _mcms_fetch_site(si, {"ac": "list", "pg": 1}, timeout=6.0)
            ok = isinstance(d.get("list"), list)
        except Exception:
            ok = False
        return si, (time.time() - t0) if ok else 1e9

    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=len(MCMS_SITES)) as ex:
        probed = list(ex.map(_probe, range(len(MCMS_SITES))))
    probed.sort(key=lambda x: x[1])
    order = [si for si, _t in probed]
    healthy = {si: t < 1e9 for si, t in probed}
    _mcms_site_cache.update({"ts": now, "order": order, "healthy": healthy})
    return order


def _mcms_fetch(params, timeout=20.0, prefer=None):
    """多站池 GET：按测速排序依次尝试（prefer 优先），站级失败自动换下一站。
    返回 (实际服务站下标, 响应 dict)——id 命名空间需要知道真实来源站。"""
    last_err = None
    order = list(_mcms_site_order())
    if prefer is not None and prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    for si in order:
        try:
            return si, _mcms_fetch_site(si, params, timeout)
        except Exception as e:
            last_err = e
            # 该站本次失败：立即降权（排尾），避免同请求周期内反复撞死站
            with _mcms_pool_lock:
                o = _mcms_site_cache["order"]
                if si in o:
                    o.remove(si)
                    o.append(si)
    raise RuntimeError("mcms all sites failed: %s" % (last_err and str(last_err))[:160])


def _hhkan_alive() -> bool:
    """hhkan 可用性探针：走一轮真实 cdndefend 挑战（抓首页 → 有挑战则解 PoW →
    带 cookie 复请求一次验证）。家宽：cookie 通过返回正常页 → True；云端 DC：
    复请求仍是挑战页 / 连接异常 → False（2026-09-04 实测：挑战页可达但 DC IP
    永远过不了挑战，_fetch 多轮重试烧 60s+ 后报「全部镜像域不可用」）。
    比完整 _fetch 快一个量级（不轮域不重试，总预算 ~12s，5 分钟缓存）。
    出站仅 https + hhkan 镜像域白名单 + DNS 解析全公网（SSRF 三重校验）。"""
    import socket
    import ipaddress
    allowed = {d.lower() for d in hhkan.DOMAINS}
    pr = urlparse(hhkan.BASE + "/")
    host = (pr.hostname or "").lower()
    if pr.scheme != "https" or host not in allowed:
        return False
    now = time.time()
    cached = _dns_public_cache.get(host)
    if cached is not None and now - cached[0] < 60:
        pub = cached[1]
    else:
        pub = True
        try:
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if (ip.is_private or ip.is_loopback or ip.is_link_local or
                        ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                    pub = False
                    break
        except Exception:
            pub = False
        _dns_public_cache[host] = (now, pub)
    if not pub:
        return False

    def _get(cookie=None):
        headers = {"User-Agent": hhkan.UA}
        if cookie:
            headers["Cookie"] = cookie
        req = urlreq.Request(hhkan.BASE + "/", headers=headers)
        try:
            with urlreq.urlopen(req, timeout=6) as r:
                return 200, r.read(200000).decode("utf-8", "ignore")
        except urlreq.HTTPError as e:
            if e.code in (403, 850, 701):
                # 挑战形态（2026-09-08 新增 701）：读体返回状态码，由调用方判挑战标记
                return e.code, e.read().decode("utf-8", "ignore")
            raise
    try:
        code, body = _get()
        if code == 200 and "cdndefend" not in body[:2000]:
            return True
        cookie = hhkan._solve_challenge(body)
        code2, body2 = _get(cookie)
        ok2 = code2 == 200 and "cdndefend" not in body2[:2000]
        if not ok2:
            print("hhkan alive probe: 挑战后仍被拒（code=%s cookie 未生效）" % code2)
        return ok2
    except Exception as _e:
        print("hhkan alive probe fail: %s: %s" % (type(_e).__name__, str(_e)[:160]))
        return False


def _vod_mcms_mode() -> bool:
    """hhkan 主站是否可达（5 分钟缓存）；不可达或强制 env → True（走 macCMS）。"""
    if os.environ.get("OTV_VOD") == "mcms":
        return True
    now = time.time()
    if _vod_mode_cache["ts"] and now - _vod_mode_cache["ts"] < _VOD_MODE_TTL:
        return _vod_mode_cache["mcms"]
    ok = _hhkan_alive()
    _vod_mode_cache["ts"] = now
    _vod_mode_cache["mcms"] = not ok
    return not ok


def _mcms_item(it, si=None):
    score = it.get("vod_douban_score") or it.get("vod_score") or 0
    try:
        score = round(float(score), 1)
    except (TypeError, ValueError):
        score = 0.0
    vid = it.get("vod_id") or 0
    if si is not None and vid:
        # 站命名空间（需求⑮）：不同站 vod_id 会撞号，+（站下标+1)*10M 编码来源站，
        # detail/play 反解——多站池下 id 永远指向正确站。
        vid = vid + (si + 1) * 10_000_000
    return {"id": vid, "title": (it.get("vod_name") or "").strip(),
            "cover": (it.get("vod_pic") or "").strip(), "score": score,
            "remark": (it.get("vod_remarks") or "").strip()}


def _mcms_site_of_id(vid):
    """namespaced id → (si, 站内真实 id)；未命名空间（旧缓存 id）→ 测速序第一健康站。"""
    if vid and vid > 10_000_000:
        si = vid // 10_000_000 - 1
        if 0 <= si < len(MCMS_SITES):
            return si, vid % 10_000_000
    order = _mcms_site_order()
    return (order[0] if order else 0), vid


def _mcms_list(params, si=None):
    """列表请求（si 优先站，站级故障自动切换；返回 id 按实际服务站命名空间）。"""
    si_used, d = _mcms_fetch(params, prefer=si)
    return ([_mcms_item(it, si_used) for it in (d.get("list") or []) if it.get("vod_id")],
            d.get("pagecount") or 1)


def _mcms_list_site(si, params):
    """单站列表（多站搜索合并用）：返回 items（吞错为空，站故障不拖垮合并）。"""
    try:
        d = _mcms_fetch_site(si, params, timeout=8.0)
        return [_mcms_item(it, si) for it in (d.get("list") or []) if it.get("vod_id")]
    except Exception:
        return []


def _mcms_paged(cid, page, per_limit=24):
    """频道页：叶子分类并行抓取 → 轮转合并（各叶均匀）→ (items, has_more)。
    全页固定单站（id 命名一致）；该站故障时返回空，由页缓存过期后换站自愈。"""
    import concurrent.futures as _cf
    got = []
    # 固定站点抓取，绝不拿 A 站分类 id 去故障转移后的 B 站请求。首站失效时，
    # 整体切到下一站并重新读取它自己的分类映射。
    for si in _mcms_site_order():
        site_name = MCMS_SITES[si]["name"]
        site_tree = MCMS_LEAVES_BY_SITE.get(site_name) or {}
        leaves = site_tree.get(cid) or []
        if not leaves:
            continue

        def _leaf(t):
            try:
                d = _mcms_fetch_site(si, {"ac": "videolist", "t": t, "pg": page}, timeout=8.0)
                return ([_mcms_item(it, si) for it in (d.get("list") or []) if it.get("vod_id")],
                        d.get("pagecount") or 1)
            except Exception:
                return [], 1

        with _cf.ThreadPoolExecutor(max_workers=min(6, len(leaves))) as ex:
            got = list(ex.map(_leaf, leaves))
        if any(items for items, _pc in got):
            break
    if not got or not any(items for items, _pc in got):
        return [], False
    merged, seen = [], set()
    i = 0
    while len(merged) < per_limit:
        added = False
        for items, _pc in got:
            if i < len(items):
                it = items[i]
                if it["id"] not in seen and it["title"]:
                    seen.add(it["id"])
                    merged.append(it)
                    added = True
                    if len(merged) >= per_limit:
                        break
        if not added:
            break
        i += 1
    more = bool(got) and any(page < pc for _, pc in got)
    return merged, more


def mcms_home():
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=4) as ex:
        res = dict(zip(MCMS_CHANNELS.keys(),
                       ex.map(lambda c: _mcms_paged(c, 1, 12), MCMS_CHANNELS.keys())))
    sections = []
    for cid, name in MCMS_CHANNELS.items():
        items = res.get(cid, ([], False))[0]
        if items:
            sections.append({"title": "近期%s" % name, "items": items})
    carousel = [{"id": it["id"], "title": it["title"], "backdrop": it["cover"],
                 "tags": [it["remark"] or "高清"]} for it in (sections[0]["items"][:5] if sections else [])]
    return {"sections": sections, "carousel": carousel, "source": "mcms"}


def _mcms_channel_sections(cid: int) -> list[dict]:
    """补充源的分类安全兜底。

    只对有明确 macCMS 分类映射的频道返回内容，短剧等无法可靠映射的频道宁可
    保持空态，也不混入全站内容。三个列表均来自同一分类；热门按评分排序，
    另外两栏按上游列表顺序取不同窗口，避免把同一批卡片重复铺满三栏。
    """
    if cid not in MCMS_CHANNELS:
        return []
    items, _more = _mcms_paged(cid, 1, 36)
    if not items:
        return []

    def score(item):
        try:
            return float(item.get("score") or 0)
        except (TypeError, ValueError):
            return 0.0

    recent = items[:12]
    # 上游不足 24 条时 items[12:24] 为空；原来 `or recent` 会让「最近更新」
    # 和「最新上线」字面变成同一批数据（2026-09-30 用户反馈"两栏内容一样"）。
    # 现在按 id 去重切分，三栏互斥；条目不够就留空，绝不用同一批冒充两栏。
    def _key(item):
        return str(item.get("vod_id") or item.get("id") or item.get("name") or "")

    updates = items[12:24]
    if not updates:
        used = {_key(i) for i in recent}
        updates = [i for i in items if _key(i) not in used][:12]
    used_updates = {_key(i) for i in updates}
    hot = [i for i in sorted(items, key=score, reverse=True)
           if _key(i) not in used_updates][:12]
    return [
        {"name": "最近热门", "items": hot},
        {"name": "最新上线", "items": recent},
        {"name": "最近更新", "items": updates},
    ]


def _mcms_detail_raw(vid):
    si, real = _mcms_site_of_id(vid)
    _si, d = _mcms_fetch({"ac": "videolist", "ids": real}, prefer=si)
    lst = d.get("list") or []
    if not lst:
        raise RuntimeError("未找到该影片")
    return lst[0]


# 补线/跨站播放的 videolist 结果缓存 (si, vod_id) -> (ts, item)（v1.32 性能）：
# detail 补线（_site_lines）与 mcms_enriched_play 每次换集都打同一站 API，
# 缓存 10 分钟后换集/换线只剩中继转发。item 只读（_mcms_groups 不改写），
# 直接共享引用即可；未命中/失败不缓存（站端数据变化尽快反映）。
_MCMS_VIDEOLIST_CACHE = {}
_MCMS_VIDEOLIST_TTL = 600.0
_MCMS_VIDEOLIST_MAX = 120
_mcms_videolist_lock = threading.Lock()
_MCMS_VIDEOLIST_PENDING = {}


def _mcms_videolist_item(si, vod_id, timeout=6.0, fresh=False):
    key = (si, vod_id)
    now = time.time()
    with _mcms_videolist_lock:
        if fresh:
            _MCMS_VIDEOLIST_CACHE.pop(key, None)
        else:
            hit = _MCMS_VIDEOLIST_CACHE.get(key)
            if hit and now - hit[0] < _MCMS_VIDEOLIST_TTL:
                return hit[1]
        ticket = object()
        _MCMS_VIDEOLIST_PENDING[key] = ticket
    item = None
    try:
        raw = _mcms_fetch_site(si, {"ac": "videolist", "ids": vod_id}, timeout=timeout)
        lst = raw.get("list") or []
        if lst:
            item = lst[0]
        return item
    finally:
        with _mcms_videolist_lock:
            if _MCMS_VIDEOLIST_PENDING.get(key) is ticket:
                _MCMS_VIDEOLIST_PENDING.pop(key, None)
                if item is not None:
                    _MCMS_VIDEOLIST_CACHE[key] = (time.time(), item)
                    if len(_MCMS_VIDEOLIST_CACHE) > _MCMS_VIDEOLIST_MAX:
                        for k in sorted(_MCMS_VIDEOLIST_CACHE, key=lambda k: _MCMS_VIDEOLIST_CACHE[k][0])[:60]:
                            _MCMS_VIDEOLIST_CACHE.pop(k, None)


# ---------------- 线路级清晰度探测与排序（2026-09-08 需求⑮） ----------------
# 线路名清晰度提示（站内惯例：1080zyk=1080P 线、bd/蓝光、超清…），仅作初筛；
# 真实清晰度以 master m3u8 的 RESOLUTION 为准（_probe_m3u8_height）。
_LINE_HINTS = (("1080", 1080), ("蓝光", 1080), ("2k", 1440), ("4k", 2160),
               ("超清", 720), ("bd", 720), ("高清", 480), ("m3u8", 0))

_m3u8_res_cache = {}  # url -> (ts, height, alive)；10 分钟
_mcms_warm_lock = threading.Lock()
_mcms_warming = set()


def _line_hint_height(name: str) -> int:
    low = (name or "").lower()
    for k, h in _LINE_HINTS:
        if k in low:
            return h
    return 0


def _probe_m3u8_height(url: str, timeout: float = 2.5):
    """取 master m3u8 读最高 RESOLUTION 高度；顺带验证线路存活。
    返回 (height, alive)。SSRF：仅 http/https + _blocked_internal_url 双重校验。
    结果按 URL 缓存 10 分钟（探测失败的线路缓存 2 分钟，尽快给复活机会）。"""
    if not url or not url.startswith(("http://", "https://")):
        return (0, False)
    now = time.time()
    hit = _m3u8_res_cache.get(url)
    if hit and now - hit[0] < (600 if hit[2] else 120):
        return hit[1], hit[2]
    height, alive = 0, False
    try:
        if not _blocked_internal_url(url):
            req = urlreq.Request(url, headers={"User-Agent": MCMS_UA})
            with urlreq.urlopen(req, timeout=timeout) as r:
                head = r.read(64 * 1024).decode("utf-8", "ignore")
            if "#EXTM3U" in head[:64]:
                alive = True
                for m in re.finditer(r"RESOLUTION=(\d+)x(\d+)", head):
                    height = max(height, int(m.group(2)))
    except Exception:
        pass
    _m3u8_res_cache[url] = (now, height, alive)
    if len(_m3u8_res_cache) > 600:
        for k in sorted(_m3u8_res_cache, key=lambda k: _m3u8_res_cache[k][0])[:200]:
            _m3u8_res_cache.pop(k, None)
    return height, alive


def _mcms_groups(it):
    """拆 vod_play_from/vod_play_url → [{name, hint, eps:[{ep,url}]}]（保序）。
    外部站点线（bilibili/ykyun 等网页播放器，url 无 .m3u8/.mp4）过滤——
    这类页面地址不是播放器可直接消费的媒体流；
    若全组都被滤掉（罕见）回退原样返回，保证不出现 0 线路。"""
    groups_from = (it.get("vod_play_from") or "").split("$$$")
    groups_url = (it.get("vod_play_url") or "").split("$$$")
    out = []
    for gfrom, gurl in zip(groups_from, groups_url):
        eps = []
        for pair in gurl.split("#"):
            if "$" not in pair:
                continue
            name, url = pair.split("$", 1)
            name, url = name.strip(), url.strip()
            if name and url.startswith(("http://", "https://")):
                eps.append({"ep": name, "url": _mcms_ascii_url(url)})
        if eps:
            out.append({"name": gfrom.strip() or "线路", "hint": _line_hint_height(gfrom), "eps": eps})
    media = [g for g in out
             if any(".m3u8" in e["url"].lower() or ".mp4" in e["url"].lower() for e in g["eps"])]
    return media if media else out


def _rank_mcms_groups(groups, site_tag="", pid_base=0, probe_top=2):
    """清晰度优先排序 + 死线探测（需求⑮「清晰度最高最稳定」）：
    并行探测 hint 最高的前 probe_top 条线路的第 1 集 master m3u8 ——
    读真实 RESOLUTION、验证存活；探测出的死线整条丢弃（稳定）。
    返回 (排序后的 groups, 探测结果 {name: (height, alive)})。
    pid_base：跨站补线的 pid 命名空间偏移（0=本站原生，100+si*50+i=补线）。
    注意 pid 用【原始组下标 i】而非排序位——排序会随探测缓存/死线变化漂移，
    原始下标保证 detail 与 play 两次解析始终指向同一条线路。"""
    import concurrent.futures as _cf
    probe = {}
    candidates = sorted(range(len(groups)), key=lambda i: -groups[i]["hint"])[:probe_top]
    with _cf.ThreadPoolExecutor(max_workers=max(1, len(candidates))) as ex:
        futs = {i: ex.submit(_probe_m3u8_height, groups[i]["eps"][0]["url"]) for i in candidates}
        for i, f in futs.items():
            try:
                probe[i] = f.result()
            except Exception:
                probe[i] = (0, False)

    def sort_key(i):
        g = groups[i]
        h, alive = probe.get(i, (g["hint"], True))
        # 有效高度：探测成功用实测，未探测用提示；死线沉底
        eff_h = h if i in probe else g["hint"]
        return (0 if alive else 1, -eff_h)

    order = sorted(range(len(groups)), key=sort_key)
    ranked = []
    for i in order:
        g = groups[i]
        # 探测失败（含探测超时/网络抖动）【不丢线】，靠排序沉底——真死线由播放器
        # 换线链路兜底；MuMu 实测曾因探测超时把全部线路丢弃 → 详情 0 线路不可播。
        g2 = dict(g)
        g2["name"] = (site_tag + "·" + g["name"]) if site_tag else g["name"]
        g2["pid"] = pid_base + i
        g2["probe_h"] = probe[i][0] if i in probe else g["hint"]  # 有效高度（实测或提示）
        ranked.append(g2)
    return ranked, None


def _warm_mcms_quality(vid):
    """后台预热 MacCMS 线路质量，避免详情首屏等待外站 m3u8 探测。"""
    with _mcms_warm_lock:
        if vid in _mcms_warming:
            return
        _mcms_warming.add(vid)
    try:
        it = _mcms_detail_raw(vid)
        _rank_mcms_groups(_mcms_groups(it), probe_top=2)
    except Exception:
        pass
    finally:
        with _mcms_warm_lock:
            _mcms_warming.discard(vid)


def mcms_detail(vid):
    it = _mcms_detail_raw(vid)
    desc = (it.get("vod_blurb") or "").strip()
    if not desc:
        desc = re.sub(r"<[^>]+>", "", (it.get("vod_content") or "")).strip()
    meta = " / ".join(str(x) for x in [
        it.get("vod_year") or "", it.get("vod_area") or "",
        (it.get("vod_class") or "").replace(",", " / ")] if x)
    # 首屏先按线路命名提示排序；真实 RESOLUTION/存活探测放到后台，后续请求
    # 会复用探测缓存，播放器仍保留线路回退。
    groups, _probe = _rank_mcms_groups(_mcms_groups(it), probe_top=0)
    threading.Thread(target=_warm_mcms_quality, args=(vid,), daemon=True).start()
    sources = [{"name": g["name"],
                "episodes": [{"ep": e["ep"], "pid": g["pid"], "vid": vi}
                             for vi, e in enumerate(g["eps"])]}
               for g in groups]
    return {"title": (it.get("vod_name") or "").strip(), "year": str(it.get("vod_year") or ""),
            "desc": desc, "cover": (it.get("vod_pic") or "").strip(),
            "score": _mcms_item(it)["score"], "meta": meta,
            "actors": (it.get("vod_actor") or "").replace(",,", "/").strip(" ,/"),
            "director": (it.get("vod_director") or "").strip(" ,"),
            "sources": sources}


def _mcms_ascii_url(u):
    """URL 里的非 ASCII 段（资源站常有「/第01集/」式中文路径）百分号编码——
    urllib3/urllib 出站不接受 Unicode URL，中继层会直接 UnicodeEncodeError。"""
    return re.sub(r"[^\x00-\x7f]+", lambda m: quote(m.group(0)), u)


def mcms_play(vodid, pid, vid):
    """按 detail 契约反解：组 pid 内【第 vid 集】的直链（重新取原文保证 URL 原样）。
    2026-09-08 修复：旧版忽略 vid 恒返回整组 episodes，前端取 sources[0]
    会永远播第 1 集（mcms 降级模式下换集失效的存量 bug）。vid 越界回退第 1 集。"""
    groups = _mcms_groups(_mcms_detail_raw(vodid))
    if not groups:
        return {"line": "", "sources": []}
    pid = max(0, min(pid, len(groups) - 1))
    g = groups[pid]
    vid = max(0, min(vid, len(g["eps"]) - 1))
    e = g["eps"][vid]
    line = g["name"]
    name = "%s·%s" % (line, e["ep"]) if len(g["eps"]) > 1 else line
    return {"line": line, "sources": [{"name": name, "url": e["url"]}]}


# ---------------- hhkan 详情跨站补线（2026-09-08 需求⑮） ----------------
# 设备端 hhkan 正常时详情页只有源站线路；本层把 mcCMS 池同名影片的线路追加进
# detail 响应（pid≥100 命名空间：100+站下标*50+原始组下标），
# 只交给播放器做自动选源/换源，详情页不暴露线路选择。
# 实测清晰度 ≥720p 的补线插到 hhkan 线路之前 = 播放器默认线自动选到更清晰的源。
# 全链路带阶段死线（搜索 3s / 详情 3s / 探测 2.5s，总预算 ~4.5s）+ 异常静默，
# 不拖垮 hhkan 主路径；env OTV_ENRICH=0 可整体关闭。
_ENRICH_TTL = 600.0
_enrich_search_cache = {}   # title -> (ts, {站下标: vod_id 或 0})；0=该站未命中（负缓存）
_enrich_vid_title = {}      # hhkan vodid -> (ts, title)
_enrich_lock = threading.Lock()


def _title_norm(t):
    """标题归一化：去标点/空白后比较——「蜘蛛侠：英雄无归」vs「蜘蛛侠英雄无归」
    这类全角冒号/间隔号差异是跨站同名匹配的主要漏配来源。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "")


def _enrich_find_sites(title):
    """同名影片在 mcms 池各站的命中。搜索策略：全标题 → 归一化前缀6 → 前缀4
    （MacCMS wd 是连续子串匹配，「蜘蛛侠英雄无归」搜不到「蜘蛛侠：英雄无归」，
    前缀降级可命中标点变体；命中即早停），结果按归一化标题等值校验。"""
    now = time.time()
    with _enrich_lock:
        hit = _enrich_search_cache.get(title)
    if hit and now - hit[0] < _ENRICH_TTL:
        return hit[1]
    want = _title_norm(title)
    tn = re.sub(r"[^\w\u4e00-\u9fff]+", "", title)
    kws = [title]
    for n in (6, 4):
        if len(tn) > n and tn[:n] not in _title_norm(kws[0]):
            kws.append(tn[:n])

    def _match(d):
        for it in (d.get("list") or [])[:20]:
            if _title_norm(it.get("vod_name") or "") == want and it.get("vod_id"):
                return it.get("vod_id")
        return 0

    def _search(si):
        t0 = time.time()
        for kw in kws:
            try:
                d = _mcms_fetch_site(si, {"ac": "videolist", "wd": kw, "pg": 1}, timeout=3.0)
                vid = _match(d)
                if vid:
                    return vid
            except Exception:
                break  # 站故障：再换关键词也是白撞
            if time.time() - t0 > 4.0:
                break  # 站级搜索预算
        return 0

    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=len(MCMS_SITES)) as ex:
        found = dict(zip(range(len(MCMS_SITES)), ex.map(_search, range(len(MCMS_SITES)))))
    with _enrich_lock:
        _enrich_search_cache[title] = (now, found)
        if len(_enrich_search_cache) > 300:
            for k in sorted(_enrich_search_cache, key=lambda k: _enrich_search_cache[k][0])[:100]:
                _enrich_search_cache.pop(k, None)
    return found


def _hhkan_title_of(vodid):
    now = time.time()
    with _enrich_lock:
        hit = _enrich_vid_title.get(vodid)
    if hit and now - hit[0] < _ENRICH_TTL:
        return hit[1]
    try:
        d = hhkan.parse_detail(_hhkan_origin_call(
            lambda: hhkan.get_page("/detail/%d.html" % vodid)), vodid)
        title = (d.get("title") or "").strip()
    except Exception:
        title = ""
    if not title:
        title = ((_HHKAN_SNAPSHOT.detail(vodid) or {}).get("title") or "").strip()
    with _enrich_lock:
        _enrich_vid_title[vodid] = (now, title)
        if len(_enrich_vid_title) > 300:
            for k in sorted(_enrich_vid_title, key=lambda k: _enrich_vid_title[k][0])[:100]:
                _enrich_vid_title.pop(k, None)
    return title


# 原生线探测缓存 {(vodid, pid): (ts, height, alive)}；成功 10 分钟，失败（alive=False）
# 2 分钟给复活机会——与 _m3u8_res_cache 同参；容量上限 400 防长驻进程无界增长。
_NATIVE_PROBE_CACHE = {}
_NATIVE_PROBE_LOCK = threading.Lock()
_NATIVE_PROBE_MAX = 400


def _native_probe_evict():
    if len(_NATIVE_PROBE_CACHE) > _NATIVE_PROBE_MAX:
        for k in sorted(_NATIVE_PROBE_CACHE, key=lambda k: _NATIVE_PROBE_CACHE[k][0])[:200]:
            _NATIVE_PROBE_CACHE.pop(k, None)


# 统一排名后的详情缓存（v1.32 性能）：vid -> (ts, d 深拷贝)。
# App/前端的「详情(快速) → 起播 enrich=1」两段式请求共享排名结果：第一段的
# 后台补线+原生探测完成即写此缓存，起播 enrich=1 直接命中（旧版 ?enrich=1
# 键不同永远绕过页缓存，每次起播同步吃满 ~8-10s 探测预算）。
# 存取均为深拷贝：后台线程只改自己的 d，读方拿独立副本，消除「边序列化边插键」竞态。
_DETAIL_RANKED_CACHE = {}
_DETAIL_RANKED_TTL = 300.0
_DETAIL_RANKED_MAX = 200
_detail_ranked_lock = threading.Lock()


def _detail_ranked_get(vid):
    with _detail_ranked_lock:
        hit = _DETAIL_RANKED_CACHE.get(vid)
        if hit and time.time() - hit[0] < _DETAIL_RANKED_TTL:
            return copy.deepcopy(hit[1])
    return None


def _detail_ranked_put(d):
    vid = d.get("id") or 0
    if not vid:
        return
    with _detail_ranked_lock:
        _DETAIL_RANKED_CACHE[vid] = (time.time(), copy.deepcopy(d))
        if len(_DETAIL_RANKED_CACHE) > _DETAIL_RANKED_MAX:
            for k in sorted(_DETAIL_RANKED_CACHE, key=lambda k: _DETAIL_RANKED_CACHE[k][0])[:80]:
                _DETAIL_RANKED_CACHE.pop(k, None)


# hhkan 原生 play 页解析缓存 (vodid, pid, vid) -> (ts, payload)：换集/换线重进、
# 播放失败重试不再每次回源抓播放页（0.5-2s/次）。TTL 5 分钟（直链 CDN 短期稳定，
# 兼防带签名直链过期）；只缓存解析成功（sources 非空）的结果。
_PLAY_PARSE_CACHE = {}
_PLAY_PARSE_TTL = 300.0
_PLAY_PARSE_MAX = 300
_play_parse_lock = threading.Lock()
_PLAY_PARSE_PENDING = {}


def _play_parse_get(key):
    with _play_parse_lock:
        hit = _PLAY_PARSE_CACHE.get(key)
        if hit and time.time() - hit[0] < _PLAY_PARSE_TTL:
            return copy.deepcopy(hit[1])
    return None


def _play_parse_put(key, payload):
    with _play_parse_lock:
        _PLAY_PARSE_CACHE[key] = (time.time(), copy.deepcopy(payload))
        if len(_PLAY_PARSE_CACHE) > _PLAY_PARSE_MAX:
            for k in sorted(_PLAY_PARSE_CACHE, key=lambda k: _PLAY_PARSE_CACHE[k][0])[:120]:
                _PLAY_PARSE_CACHE.pop(k, None)


def _hhkan_native_play(vodid, pid, vid, fresh=False):
    """A forced retry invalidates old links and supersedes in-flight cache writes."""
    key = (vodid, pid, vid)
    with _play_parse_lock:
        if fresh:
            _PLAY_PARSE_CACHE.pop(key, None)
        else:
            hit = _PLAY_PARSE_CACHE.get(key)
            if hit and time.time() - hit[0] < _PLAY_PARSE_TTL:
                return copy.deepcopy(hit[1])
        ticket = object()
        _PLAY_PARSE_PENDING[key] = ticket
    payload = None
    try:
        parsed = hhkan.parse_play_page(_hhkan_origin_call(
            lambda: hhkan.get_page("/play/%s-%s-%s.html" % key)))
        if parsed.get("sources"):
            payload = {"line": parsed["line"], "sources": parsed["sources"]}
        return payload
    finally:
        with _play_parse_lock:
            # A slow older parse may still finish, but must not overwrite the
            # link produced by the user's newer retry, including failed retries.
            if _PLAY_PARSE_PENDING.get(key) is ticket:
                _PLAY_PARSE_PENDING.pop(key, None)
                if payload is not None:
                    _PLAY_PARSE_CACHE[key] = (time.time(), copy.deepcopy(payload))
                    if len(_PLAY_PARSE_CACHE) > _PLAY_PARSE_MAX:
                        oldest = sorted(_PLAY_PARSE_CACHE, key=lambda k: _PLAY_PARSE_CACHE[k][0])[:120]
                        for old_key in oldest:
                            _PLAY_PARSE_CACHE.pop(old_key, None)


def _first_pid(s):
    try:
        return int((s.get("episodes") or [{}])[0].get("pid", 0))
    except Exception:
        return 0


def _unified_line_key(s):
    """统一排名键：(存活, 高度降序, 原生优先)。原生线无实测时回退线路名提示；
    死线沉底（真死线由播放器换线链路兜底，探测超时不判死）。"""
    if _first_pid(s) < 100:
        return (0 if s.get("alive", True) else 1,
                -(s.get("probe_h") or _line_hint_height(s.get("name", ""))), 0)
    return (0, -(s.get("probe_h") or 0), 1)


def _hhkan_breaker_open():
    """熔断器是否处于 open（只读状态，不参与单飞探测——探测线程专用）。"""
    with _HHKAN_ORIGIN_COND:
        return (_HHKAN_ORIGIN_STATE["status"] == "open"
                and time.time() < _HHKAN_ORIGIN_STATE["until"])


def _probe_native_sources(vodid, sources, budget=6.0):
    """好好看原生线路参与清晰度排名：并行取每条线第 1 集 play 页 → 直链 →
    master m3u8 读 RESOLUTION（复用 _probe_m3u8_height，含 URL 级缓存），
    结果写回 s["probe_h"]/s["alive"]；探测异常回退名字提示、不判死。
    budget 为墙钟预算：超时未完成的线保持无 probe_h（排序时按提示兜底）。
    熔断 open 期间整体跳过（探测直连 get_page，绕过熔断会让源站风控期
    每次详情都被后台探测再打 N 次——v1.32 修，探测结果回退线路名提示）。"""
    import concurrent.futures as _cf
    # 按 pid 去重（同一线路多版本名共享一条探测）；探测是尽力而为的后台动作，
    # 直接并发抓播放页——不走 _hhkan_origin_call：熔断单飞会让并行探测在
    # 条件变量上排队（首探针期间其余最多等 8s），6s 预算内根本轮不到后面几条。
    seen, natives = set(), []
    for s in sources:
        if _first_pid(s) >= 100:
            continue
        pid = _first_pid(s)
        if pid in seen:
            continue
        seen.add(pid)
        natives.append((pid, [x for x in sources if _first_pid(x) == pid]))
    if not natives:
        return
    if _hhkan_breaker_open():
        return
    t0 = time.time()

    def one(pid, group):
        key = (vodid, pid)
        with _NATIVE_PROBE_LOCK:
            hit = _NATIVE_PROBE_CACHE.get(key)
        if hit and time.time() - hit[0] < (600 if hit[2] else 120):
            h, alive = hit[1], hit[2]
        else:
            h, alive = 0, True
            try:
                ep = (group[0].get("episodes") or [{}])[0]
                page = hhkan.get_page(
                    "/play/%s-%s-%s.html" % (vodid, pid, ep.get("vid", 0)))
                parsed = hhkan.parse_play_page(page)
                url = next((x["url"] for x in parsed["sources"]
                            if ".m3u8" in x["url"].lower()), "")
                if url:
                    h, alive = _probe_m3u8_height(url)
            except Exception:
                pass
            with _NATIVE_PROBE_LOCK:
                _NATIVE_PROBE_CACHE[key] = (time.time(), h, alive)
                _native_probe_evict()
        for s in group:
            s["probe_h"] = h or _line_hint_height(s.get("name", ""))
            s["alive"] = alive

    ex = _cf.ThreadPoolExecutor(max_workers=min(4, len(natives)))
    try:
        futs = {p: ex.submit(one, p, g) for p, g in natives}
        for p, f in futs.items():
            try:
                f.result(timeout=max(0.5, budget - (time.time() - t0)))
            except Exception:
                pass    # 超时线不阻塞响应，排序按名字提示兜底
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def enrich_detail_sources(d, vodid):
    """hhkan 详情补线主入口（在 detail 响应组装后调用，原地修改 d["sources"]）。
    各命中站【并行】取线路（含探测），总预算 ~8s；超时站点静默放弃（命中已缓存，
    下次打开秒出完整结果）。
    v1.31 需求：好好看原生线路也参与排名——探测线程与补线并行跑，最后全部
    线路按 (存活, 实测高度) 统一降序（同高原生优先），排名即默认线路与前端
    换源面板展示顺序。
    v1.32 性能：排名完成即写 _DETAIL_RANKED_CACHE（深拷贝），后续 detail /
    ?enrich=1 请求 5 分钟内直接命中，不再重跑探测与补线。"""
    if os.environ.get("OTV_ENRICH") == "0":
        _detail_ranked_put(d)
        return
    title = (d.get("title") or "").strip()
    if not title:
        _detail_ranked_put(d)
        return
    t0 = time.time()
    # 原生线探测与补线并行（探测走 play 页直链再读 RESOLUTION，比补线慢一拍，
    # 单独 6s 预算；join 上限 10s，超时则未探测完的原生线按名字提示参与排序）。
    # 无补线命中时原生线同样参与排名（提前 join+排序后返回）。
    merged_srcs = list(d.get("sources") or [])
    _nt = threading.Thread(target=_probe_native_sources,
                           args=(vodid, merged_srcs, 6.0), daemon=True)
    _nt.start()
    try:
        found = _enrich_find_sites(title)
    except Exception:
        found = {}
    hits = {si: v for si, v in found.items() if v}
    if not hits:
        _nt.join(timeout=max(0.0, 10.0 - (time.time() - t0)))
        merged_srcs.sort(key=_unified_line_key)
        d["sources"] = merged_srcs
        _detail_ranked_put(d)
        return

    def _site_lines(si, vod_id):
        it = _mcms_videolist_item(si, vod_id, timeout=3.5)
        if not it:
            return []
        groups, _p = _rank_mcms_groups(_mcms_groups(it),
                                       site_tag=MCMS_SITES[si]["name"],
                                       pid_base=100 + si * 50)
        return groups[:4]

    import concurrent.futures as _cf
    hi, lo = [], []
    with _cf.ThreadPoolExecutor(max_workers=max(1, len(hits))) as ex:
        futs = {si: ex.submit(_site_lines, si, vid) for si, vid in hits.items()}
        for si, f in futs.items():
            try:
                # 站级死线：搜索后剩余预算（并行等待，总墙钟可控）
                left = 8.0 - (time.time() - t0)
                groups = f.result(timeout=max(0.5, left))
            except Exception:
                continue
            for g in groups:
                src = {"name": g["name"],
                       "probe_h": g.get("probe_h", 0),
                       "episodes": [{"ep": e["ep"], "pid": g["pid"], "vid": vi}
                                    for vi, e in enumerate(g["eps"])]}
                (hi if g.get("probe_h", 0) >= 720 else lo).append(src)
    _nt.join(timeout=max(0.0, 10.0 - (time.time() - t0)))
    # Keep the groups just resolved from the cross-site pool. They are assigned
    # pid>=100 above so /hhkan/play can route them through mcms_enriched_play;
    # previously hi/lo were populated but never merged, leaving visible HHKan
    # lines as the only choices and making every fallback play return empty.
    merged_srcs.extend(hi)
    merged_srcs.extend(lo)
    merged_srcs.sort(key=_unified_line_key)
    d["sources"] = merged_srcs
    _detail_ranked_put(d)


def mcms_enriched_play(vodid, pid, vid, fresh=False):
    """补线播放解析：pid=100+站*50+原始组下标 → 该站同名影片该组该集直链。"""
    si, li = divmod(pid - 100, 50)
    if si < 0 or si >= len(MCMS_SITES) or not (0 <= li < 50):
        raise RuntimeError("补线 pid 非法: %s" % pid)
    title = _hhkan_title_of(vodid)
    if not title:
        raise RuntimeError("补线解析失败：影片不存在")
    vod_id = _enrich_find_sites(title).get(si) or 0
    if not vod_id:
        raise RuntimeError("补线影片已下架")
    fresh_args = {"fresh": True} if fresh else {}
    it = _mcms_videolist_item(si, vod_id, timeout=6.0, **fresh_args)
    if not it:
        raise RuntimeError("补线影片已下架")
    groups = _mcms_groups(it)
    if li >= len(groups):
        raise RuntimeError("补线不存在")
    g = groups[li]
    vid = max(0, min(vid, len(g["eps"]) - 1))
    e = g["eps"][vid]
    line = MCMS_SITES[si]["name"] + "·" + g["name"]
    return {"line": line,
            "sources": [{"name": "%s·%s" % (line, e["ep"]) if len(g["eps"]) > 1 else line,
                         "url": e["url"]}]}


def _startup_warm():
    """启动预热（后台线程）：影视服务 + 图片域探测 + 搜索 token 预取，让首个请求零等待。

    2026-09-18：影视服务改成开机预热（一次性，不是常驻线程）。旧实现是「首个 /vod/*
    请求现场构建」——MediaIndex 建库 + 22MB 快照预热搜索索引都压在那一次请求上，
    手机上叠加第一轮豆瓣冷抓很容易顶到客户端 45s 读超时（用户报障「影视界面崩了」）。
    注：这里只构建服务，不预抓豆瓣；目录数据的冷抓由请求触发、且已在快照兜底后
    转后台（见 douban_catalog._coalesced 的冷缓存分支）。
    """
    try:
        _get_vod_service()
        print("影视服务预热完成")
    except Exception as e:
        print("影视服务预热失败（请求时再试）: %r" % (e,))
    # 懒加载快照的后台预载：首个 /hhkan/* 请求到来时大概率已就绪（2026-09-19 C1）
    _HHKAN_SNAPSHOT.preload()
    try:
        dom = hhkan.probe_img_domain()
        print("图片资源域首选: %s" % dom)
    except Exception:
        pass
    try:
        _hhkan_origin_call(hhkan.get_search_token)
        print("搜索 token 预取完成")
    except Exception:
        pass


def _img_domain_refresher():
    """每 10 分钟重新探测图片域：主域失联/恢复自动切换。"""
    while True:
        time.sleep(600)
        try:
            hhkan.probe_img_domain()
        except Exception:
            pass


def main(port=None, bind=None):
    """启动代理服务器（阻塞）。安卓 Chaquopy 通过 main() 显式调用；
    桌面 `python proxy.py [port]` 由 __main__ 块调用；
    云端容器（网页版 CloudRun）不传参 —— 端口/绑定地址读环境变量
    PORT（容器平台注入）与 OTV_BIND（默认 127.0.0.1，容器设 0.0.0.0）。
    安卓侧 proxy_runner 传 bind="0.0.0.0"（投屏：电视需经局域网访问本机中继流）。"""
    if port is None:
        try:
            port = int(os.environ.get("PORT") or 8090)
        except ValueError:
            port = 8090
    if bind is None:
        bind = os.environ.get("OTV_BIND") or "127.0.0.1"
    # 多线程：视频分片/播放列表是并发请求，单线程服务器会互相阻塞
    # 2026-09-18：先绑定端口再预热。TV 版/移动版同装一台设备时 8090 只有一方能绑定，
    # 绑定失败方由 proxy_runner 每 5s 重试；旧实现把预热放在绑定之前 → 每次重试都重建
    # 一遍影视索引（MediaIndex + 22MB 快照），失败方会在 20 分钟里每 5 秒空转一次
    # （实测模拟器上持续占 CPU，并拖慢同机另一侧的请求）。
    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=_startup_warm, daemon=True).start()
    threading.Thread(target=_img_domain_refresher, daemon=True).start()
    print("球迹直播 数据代理服务器 http://%s:%d  (数据源 %s)" % (bind, port, _active_source["url"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
