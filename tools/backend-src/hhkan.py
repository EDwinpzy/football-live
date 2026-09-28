"""hhkan0.com (好好看) 抓取客户端。

- 破解 cdndefend JS 挑战（SHA1 proof-of-work）换取访问 cookie
- 提供首页 / 分类 / 最新 / 详情 / 播放页的抓取与解析
- 图片统一走 vres 资源域，支持备用域切换
"""
from __future__ import annotations

import hashlib
import html as _html
import re
import threading
import time
import urllib.parse
import urllib.request

import source_dns

BASE = "https://www.hhkan0.com"
# 同站镜像域（2026-08-15 验证 hhkan1.com 存活，同一套 cdndefend）：主域失效自动回退
DOMAINS = ["www.hhkan0.com", "www.hhkan1.com"]
IMG_DOMAINS = [
    # 2026-09-19 复测（每域 5 张封面取中位耗时）：cyscyy 0.1~0.4s 最快，zyxpedu 1.2~4.8s，
    # enbymae 1.3~31.2s 最慢——旧结论「cyscyy 已失联」已过期，它已恢复且反超。
    # 三域内容完全一致（同一份存储），首选域直接决定首屏出图速度：此前每张图都先撞
    # 最慢的 enbymae，实测最长 31s 已越过 _IMG_FETCH_TIMEOUT=20s 被判失败 →
    # 网页版「最近热门」间歇整片空卡。现按实测速度重排。
    "https://vres.cyscyy.com",
    "https://vres.zyxpedu.com",
    "https://vres.enbymae.com",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_lock = threading.Lock()
_cookie: str | None = None
_domain_i = [0]  # 当前生效域下标（多域回退）


# ---------------------------------------------------------------- 反爬破解

def _solve_challenge(html: str, deadline: float = None) -> str:
    """从 cdndefend 挑战页解析常量并计算通过 cookie（SHA1 PoW）。

    挑战逻辑（2026-08-15 起为动态规则，全量从 JS 实时提取，站点改参数也能适配）：
    - 常量 c 取自洗牌数组；校验位置 n1 = int('0x' + c[idx])（idx 也动态提取）；
    - 求最小 i 使 sha1(c + i) 的第 n1、n1+off 字节为 target0、target1；
    - cookie 为 cdndefend_js_cookie={c}{i}。
    """
    m = re.search(r"a0_0x2a54=\['([0-9A-F]{20,})','(cdndefend_[^']+)','([a-z]+)'\]", html)
    if not m:
        raise RuntimeError("cdndefend 挑战格式变化，无法解析常量")
    const, name, _ = m.groups()  # name 形如 'cdndefend_js_cookie='（自带等号）

    # 校验位置下标（parseInt('0x'+c[0xN])），默认 0
    mn = re.search(r"parseInt\('0x'\+c\[0x([0-9a-f]+)\]\)", html)
    idx = int(mn.group(1), 16) if mn else 0
    n1 = int(const[idx], 16)

    # 第二个字节的偏移与目标值（s[n1+0xOFF]===0xT1），默认 off=1 t1=0x0b
    mo = re.search(r"s\[n1\+0x([0-9a-f]+)\]===0x([0-9a-f]+)", html)
    off = int(mo.group(1), 16) if mo else 1
    t1 = int(mo.group(2), 16) if mo else 0x0b

    # 首字节目标值（s[n1]===0xT0），默认 0xb0
    mt = re.search(r"s\[n1\]===0x([0-9a-f]+)", html)
    t0 = int(mt.group(1), 16) if mt else 0xb0

    i = 0
    while True:
        if deadline is not None and i % 1024 == 0 and time.monotonic() >= deadline:
            raise TimeoutError("hhkan source request deadline exceeded")
        d = hashlib.sha1((const + str(i)).encode()).digest()
        if d[n1] == t0 and d[n1 + off] == t1:
            return f"{name}{const}{i}"
        i += 1


# SSRF 防护（2026-08-29）：hhkan 抓取仅允许 https + 固定白名单域，
# DNS 解析后拒绝私网/环回/链路本地地址，重定向逐跳校验。
_ALLOWED_HOSTS = ("www.hhkan0.com", "www.hhkan1.com")
_private_ip = re.compile(
    r"^(127\.|10\.|0\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$|f[cd]|fe80)")


def _host_of(url: str) -> str:
    netloc = urllib.parse.urlparse(url).netloc
    return (netloc.rsplit("@", 1)[-1].split(":")[0] or "").lower()


def _check_url_safety(url: str, timeout: float = None) -> None:
    """仅 https + 白名单域；解析 IP 落在私网/环回/保留段直接拒绝。"""
    scheme = urllib.parse.urlparse(url).scheme
    host = _host_of(url)
    if scheme != "https" or host not in _ALLOWED_HOSTS:
        raise ValueError("blocked url host: %s" % host)
    try:
        for info in source_dns.getaddrinfo(host, 443, timeout=timeout):
            ip = info[4][0]
            if _private_ip.match(ip) or ip == "::1":
                raise ValueError("blocked private ip: %s" % ip)
    except TimeoutError:
        raise
    except ValueError:
        raise
    except Exception:
        pass  # 解析失败交由请求层报错


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向逐跳校验：目标协议非 http/https 或域名不在白名单即拒绝。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlparse(newurl).scheme
        if scheme not in ("http", "https") or _host_of(newurl) not in _ALLOWED_HOSTS:
            raise ValueError("blocked redirect to: %s" % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SafeBoundedHTTPHandler(source_dns.BoundedHTTPHandler):
    def http_open(self, req):
        _check_url_safety(req.full_url, timeout=getattr(req, "timeout", None))
        return super().http_open(req)


class _SafeBoundedHTTPSHandler(source_dns.BoundedHTTPSHandler):
    def https_open(self, req):
        _check_url_safety(req.full_url, timeout=getattr(req, "timeout", None))
        return super().https_open(req)


_safe_opener = urllib.request.build_opener(
    _SafeRedirectHandler, _SafeBoundedHTTPHandler, _SafeBoundedHTTPSHandler)


def _safe_urlopen(req, timeout):
    return _safe_opener.open(req, timeout=timeout)


def _remaining_timeout(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("hhkan source request deadline exceeded")
    return remaining


def _set_response_socket_timeout(response, timeout):
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None and timeout is not None:
        sock.settimeout(max(0.001, timeout))


def _read_response_body(response, max_bytes=None, deadline=None):
    if max_bytes is None and deadline is None:
        return response.read()

    chunks = []
    received = 0
    while max_bytes is None or received <= max_bytes:
        remaining = _remaining_timeout(deadline)
        _set_response_socket_timeout(response, remaining)
        read_size = 16 * 1024 if max_bytes is None else min(16 * 1024, max_bytes + 1 - received)
        if read_size <= 0:
            break
        chunk = response.read(read_size)
        if not chunk:
            break
        chunks.append(chunk)
        received += len(chunk)
        if max_bytes is not None and received > max_bytes:
            raise ValueError("hhkan response exceeds byte limit")
    return b"".join(chunks)


def _fetch(url: str, referer: str = None, timeout: float = None,
           max_bytes: int = None) -> str:
    """带 cookie 抓取：挑战页自动重算 cookie 重试；网络失败自动切镜像域。
    429（源站限流，2026-08-29 实测逐键前缀搜索会触发）指数退避 1s/2s/4s 重试。"""
    global _cookie, BASE
    # 保留调用方请求的 path/query，但每次重试都用当前镜像重建完整 URL。
    # 旧实现只修改 BASE，局部变量 url 仍指向首次失败的域名，实际从未切到备用镜像。
    target = urllib.parse.urlsplit(url)
    target_path = urllib.parse.urlunsplit(("", "", target.path or "/", target.query, target.fragment))
    explicit_referer = referer
    backoff_429 = 0
    deadline = time.monotonic() + max(0.0, float(timeout)) if timeout is not None else None
    for attempt in range(len(DOMAINS) * 2 + 6):
        remaining = _remaining_timeout(deadline)
        active_base = "https://" + DOMAINS[_domain_i[0]]
        request_url = urllib.parse.urljoin(active_base + "/", target_path.lstrip("/"))
        request_referer = explicit_referer or (active_base + "/")
        headers = {"User-Agent": UA, "Referer": request_referer}
        if _cookie:
            headers["Cookie"] = _cookie
        req = urllib.request.Request(request_url, headers=headers)
        try:
            previous_dns_deadline = (
                source_dns.set_deadline(deadline) if deadline is not None else None)
            try:
                resp = _safe_urlopen(req, 25 if remaining is None else remaining)
            finally:
                if deadline is not None:
                    source_dns.restore_deadline(previous_dns_deadline)
            try:
                body = _read_response_body(resp, max_bytes, deadline).decode("utf-8", "ignore")
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as e:
            if e.code in (403, 850, 701):
                # 收到挑战（或 403/701）：现场求解并带新 cookie 重试（同域）。
                # 701 为 2026-09-08 实测新增的挑战形态（设备端反复 HTTP 701，
                # MuMu 抓包定位；旧代码只认 403/850 → 探针误判源站死亡 →
                # 设备被翻进 macCMS 降级模式，hhkan 影片全部「不存在」）。
                with _lock:
                    _cookie = _solve_challenge(
                        _read_response_body(e, max_bytes, deadline).decode("utf-8", "ignore"),
                        deadline=deadline)
                _remaining_timeout(deadline)
                continue
            if e.code == 429 and backoff_429 < 4:
                # 源站限流：退避 1.5s/3s/6s/10s 后重试（sleep 在锁外，不阻塞其他线程的挑战求解）
                backoff_429 += 1
                delay = (1 << backoff_429) * 0.75
                remaining_after_response = _remaining_timeout(deadline)
                if remaining_after_response is not None:
                    time.sleep(min(delay, remaining_after_response))
                    _remaining_timeout(deadline)
                else:
                    time.sleep(delay)
                continue
            raise
        except Exception as exc:
            # 网络失败（超时/断连/域名失效）：切换镜像域后重试
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("hhkan source request deadline exceeded") from exc
            with _lock:
                _domain_i[0] = (_domain_i[0] + 1) % len(DOMAINS)
                BASE = "https://" + DOMAINS[_domain_i[0]]
                _cookie = None  # 换域后 cookie 不通用，需重新解挑战
            continue
        if "cdndefend" in body[:2000]:
            with _lock:
                _cookie = _solve_challenge(body, deadline=deadline)
            _remaining_timeout(deadline)
            continue
        return body
    raise RuntimeError("抓取失败：全部镜像域不可用")


def get_page(path: str, timeout: float = None, max_bytes: int = None) -> str:
    if timeout is None and max_bytes is None:
        return _fetch(BASE + path)
    return _fetch(BASE + path, timeout=timeout, max_bytes=max_bytes)


# 固定第三方数据源白名单（7m 即时比分，用于足球直播比分补全）
FETCH_PLAIN_HOSTS = ("js-live.7m.com.cn", "data.7m.com.cn")


def fetch_plain(url: str, timeout: int = 12) -> str:
    """简单 GET 文本抓取：无挑战处理、无域切换，仅供白名单内固定比分源使用。

    安全边界：仅 http/https；host 必须命中 FETCH_PLAIN_HOSTS 白名单；
    不跟随重定向（30x 视为失败）。
    """
    import http.client as _hc
    sp = urllib.parse.urlsplit(url)
    if sp.scheme not in ("http", "https") or sp.hostname not in FETCH_PLAIN_HOSTS:
        raise RuntimeError("fetch_plain: host not allowed: %s" % sp.hostname)
    conn_cls = _hc.HTTPSConnection if sp.scheme == "https" else _hc.HTTPConnection
    conn = conn_cls(sp.hostname, sp.port or (443 if sp.scheme == "https" else 80), timeout=timeout)
    try:
        path = sp.path or "/"
        if sp.query:
            path += "?" + sp.query
        conn.request("GET", path, headers={"User-Agent": UA, "Host": sp.netloc})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError("fetch_plain: HTTP %d" % resp.status)
        data = resp.read()
    finally:
        conn.close()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


# 分类筛选选项缓存：filters[cid] = {types, areas, langs, years}（按频道静态，缓存一次）
_FILTERS_CACHE: dict = {}
# 通用年份筛选（各频道一致）
ALL_YEARS = ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]


def get_filters(cid: int) -> dict:
    """频道的筛选选项（类型/地区/语言/年份），从 /show/{cid}------.html 提取并缓存。

    macms 站点 show 用途筛选 URL：/show/{tid}-{type}-{area}-{lang}-{year}-{const}-{page}.html
    段为空表示「全部」。返回 {"types":[..], "areas":[..], "langs":[..], "years":[..]}。
    """
    if cid in _FILTERS_CACHE:
        return _FILTERS_CACHE[cid]
    result = {"types": [], "areas": [], "langs": [], "years": list(ALL_YEARS)}
    try:
        html = get_page("/show/%d------.html" % cid)
        seen = {"types": set(), "areas": set(), "langs": set()}
        for m in re.finditer(r'href="/show/%d-([^"]*)\.html"' % cid, html):
            # group(1) = cid 之后的部分：{type}-{area}-{lang}-{year}-{const}-{page}
            parts = m.group(1).split("-")
            if len(parts) < 6:
                continue
            for key, idx in (("types", 0), ("areas", 1), ("langs", 2)):
                val = urllib.parse.unquote(parts[idx]).strip()
                if val and val not in seen[key]:
                    seen[key].add(val)
                    result[key].append(val)
    except Exception:
        pass
    # 修复（2026-09-07 需求①筛选显示不全）：空结果不缓存——服务冷启动首抓
    # 超时/被限时，except 吞错后空表被毒进本缓存且永不过期，前端重试 4 次
    # 全部命中空缓存，筛选条永远空白。空表保持不缓存，让下次请求重抓。
    if result["types"]:
        _FILTERS_CACHE[cid] = result
    return result


def get_show_page(cid: int, type_: str = "", area: str = "", lang: str = "",
                  year: str = "", page: int = 1, by: str = "3") -> list[dict]:
    """带筛选的分页列表：/show/{cid}-{type}-{area}-{lang}-{year}-{by}-{page}.html。
    by 为站点排序段（3=最热默认；1/2 等其他排序由前端传入）。
    返回该页卡片列表（id/title/cover/score/remark）。"""
    seg = [str(cid), type_, area, lang, year, str(by or "3"), str(page)]
    path = "/show/%s.html" % "-".join(urllib.parse.quote(s, safe="") for s in seg)
    return _parse_vod_list(get_page(path))


# 搜索 token 缓存（全站一致）：搜索高频，TTL 内复用，避免每次搜索都重新抓首页（请求量减半、降低限流风险）
_TOKEN_TTL = 600.0
_token_cache = None  # (token, 过期时间戳)


def get_search_token(timeout: float = None, max_bytes: int = None) -> str:
    """搜索表单里的 token（全站一致，从首页提取；10 分钟 TTL 缓存）。"""
    global _token_cache
    if _token_cache and _token_cache[1] > time.time():
        return _token_cache[0]
    if timeout is None and max_bytes is None:
        html = get_page("/")
    else:
        html = get_page("/", timeout=timeout, max_bytes=max_bytes)
    m = re.search(r'name="t" value="([^"]+)"', html)
    if not m:
        raise RuntimeError("无法获取搜索 token")
    token = m.group(1)
    _token_cache = (token, time.time() + _TOKEN_TTL)
    return token


def img_url(rel: str) -> str:
    """相对路径 /vod1/... 转图片资源域完整 URL（用当前探测可用的首选域）。"""
    if rel.startswith("http"):
        return rel
    return IMG_DOMAINS[_img_domain_i[0]] + rel


# 图片域探测：主域失联时整体切换（2026-08-28 cyscyy 断连曾致全站海报 404）
_img_domain_i = [0]


def probe_img_domain():
    """TCP 探测 IMG_DOMAINS（443 可连=域名可用），把首选域切到第一个可达域。

    仅做 TCP 层连通性检查，不发起 HTTP 请求、不解析响应；
    顺序与 IMG_DOMAINS 一致，全部失联时保持当前首选域不变。
    """
    import socket as _socket
    for k, dom in enumerate(IMG_DOMAINS):
        host = dom.split("//", 1)[1]
        try:
            conn = _socket.create_connection((host, 443), timeout=5)
            conn.close()
            _img_domain_i[0] = k
            return dom
        except OSError:
            continue
    return IMG_DOMAINS[_img_domain_i[0]]


# ---------------------------------------------------------------- 页面解析

# 好好看频道页的 class 与内部标签会随模板调整；这里按 class token 切卡，而不是
# 依赖旧版完整的 </div></a></div> 结构，避免模板小改后整页被解析为空。
_MODULE_ITEM_RE = re.compile(
    r'<(?:div|article)\b[^>]*\bclass\s*=\s*["\'][^"\']*\bmodule-item\b[^"\']*["\'][^>]*>',
    re.I,
)
_SECTION_HEADER_RE = re.compile(
    r'<(?P<tag>div|h[1-6])\b[^>]*\bclass\s*=\s*["\'][^"\']*\bsection-header-title\b[^"\']*["\'][^>]*>'
    r'(?P<title>.*?)</(?P=tag)>',
    re.I | re.S,
)

# 面向产品稳定的三个栏目名。源站常见名为「最新上线 / 热门推荐 / 最近更新」，
# 对外统一成原产品的「最近热门 / 最新上线 / 最近更新」。
CHANNEL_SECTION_ORDER = ("最近热门", "最新上线", "最近更新")
_SOURCE_SECTION_ORDER = ("最新上线", "最近热门", "最近更新")


def _clean_text(value: str) -> str:
    return _html.unescape(re.sub(r'<[^>]+>', '', value or '')).strip()


def _parse_card(block: str) -> dict | None:
    """解析一个 module-item 卡片；兼容 data-original/data-src/src 三种海报写法。"""
    m = re.search(r'href\s*=\s*["\'](?:https?://[^"\']+)?/detail/(\d+)\.html', block, re.I)
    if not m:
        return None
    # 源站会在真实标题前后插入 display:none 的站点水印节点；取第一个可见标题，
    # 不能再用单次 regex 命中第一个 v-item-title。
    title = ""
    title_nodes = re.finditer(
        r'<(?P<tag>[a-z][\w:-]*)\b(?P<attrs>[^>]*\bclass\s*=\s*["\'][^"\']*\bv-item-title\b[^"\']*["\'][^>]*)>'
        r'(?P<body>.*?)</(?P=tag)>',
        block, re.I | re.S)
    for node in title_nodes:
        attrs = node.group("attrs")
        if re.search(r'display\s*:\s*none', attrs, re.I):
            continue
        candidate = _clean_text(node.group("body"))
        if candidate:
            title = candidate
            break
    if not title:
        return None
    # 同一卡片第一张 img 是 logo_placeholder_vertical，第二张才是真实海报。
    # 按懒加载属性优先级逐标签选择，并显式剔除占位资源/隐藏探针。
    cover = ""
    for image in re.finditer(r'<img\b(?P<attrs>[^>]*)>', block, re.I | re.S):
        attrs = image.group("attrs")
        if re.search(r'\bid\s*=\s*["\']noneCoverImg["\']', attrs, re.I):
            continue
        for attr in ("data-original", "data-src", "data-lazy", "src"):
            value_m = re.search(r'\b' + re.escape(attr) + r'\s*=\s*["\']([^"\']+)', attrs, re.I)
            if not value_m:
                continue
            candidate = value_m.group(1).strip()
            low = candidate.lower()
            if candidate and not candidate.startswith("data:") and "placeholder" not in low:
                cover = candidate
                break
        if cover:
            break
    if cover.startswith("//"):
        cover = "https:" + cover
    if cover.startswith("data:"):
        cover = ""
    score_m = re.search(r'>\s*(?:豆瓣\s*[:：]\s*)?([\d.]+)\s*分\s*<', block)
    remark_m = re.search(
        r'<[^>]+\bclass\s*=\s*["\'][^"\']*\bv-item-bottom\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        block, re.I | re.S)
    try:
        score = float(score_m.group(1)) if score_m else 0.0
    except ValueError:
        score = 0.0
    return {
        "id": int(m.group(1)),
        "title": title,
        "cover": img_url(cover) if cover else "",
        "score": score,
        "remark": _clean_text(remark_m.group(1)) if remark_m else "",
    }


def _parse_vod_list(page: str) -> list[dict]:
    """解析页面里所有 .module-item 卡片（保序、按 id 去重）。"""
    starts = list(_MODULE_ITEM_RE.finditer(page))
    items, seen = [], set()
    for i, start in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(page)
        card = _parse_card(page[start.start():end])
        if card and card["id"] not in seen:
            seen.add(card["id"])
            items.append(card)
    return items


def _titled_sections(page: str):
    """按 section-header-title 取出标题与其后、下一标题前的内容。"""
    headers = list(_SECTION_HEADER_RE.finditer(page))
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(page)
        yield _clean_text(header.group("title")), page[header.end():end]


def _canonical_channel_section_name(name: str) -> str:
    """将源站可变栏目名归一为产品固定三栏目名。"""
    text = _clean_text(name)
    if re.search(r'更新|连载|追更', text):
        return "最近更新"
    if re.search(r'热门|热播|推荐|人气|精选', text):
        return "最近热门"
    if re.search(r'最新|上线|上新|新片|新剧', text):
        return "最新上线"
    return ""


def normalize_channel_sections(sections: list[dict]) -> list[dict]:
    """保留好好看栏目内容，统一标题并按产品规定顺序输出。

    若源站改了标题文案但仍保持三段布局，未识别段按其原始频道顺序补到空位，
    以免一次文案改版把整组栏目直接丢掉。
    """
    picked, unknown = {}, []
    for sec in sections:
        items = sec.get("items") or []
        if not items:
            continue
        target = _canonical_channel_section_name(sec.get("name", ""))
        if target:
            old = picked.get(target)
            if old is None or len(items) > len(old["items"]):
                picked[target] = {"name": target, "items": items}
        else:
            unknown.append(items)
    for target in _SOURCE_SECTION_ORDER:
        if target not in picked and unknown:
            picked[target] = {"name": target, "items": unknown.pop(0)}
    return [picked[name] for name in CHANNEL_SECTION_ORDER if name in picked]


def complete_channel_sections(sections: list[dict]) -> list[dict]:
    """返回固定三栏目；空栏目也保留，供前端呈现明确的空态而非吞掉标题。"""
    picked = {sec["name"]: sec["items"] for sec in normalize_channel_sections(sections)}
    return [{"name": name, "items": picked.get(name, [])} for name in CHANNEL_SECTION_ORDER]


def parse_channel_sections(page: str) -> list[dict]:
    """解析频道页三栏目并归一为产品固定标题。"""
    raw = []
    for name, body in _titled_sections(page):
        items = _parse_vod_list(body)
        if name and items:
            raw.append({"name": name, "items": items})
    return normalize_channel_sections(raw)


def parse_carousel(html: str) -> list[dict]:
    """首页轮播焦点图（16:9 超宽横版海报，3360×1080 实测）。

    每个轮播项：横版大图 + 影片链接 + 标题 + 标签。
    TV 首页 hero/横幅直接用这些图（cover 裁切任意比例）。
    """
    items = []
    for m in re.finditer(
        r'<div class="swiper-slide">\s*<a href="/detail/(\d+)\.html" class="carousel-item">(.*?)</a>\s*</div>',
        html, re.S):
        vid, block = m.group(1), m.group(2)
        img = re.search(r'data-original="(/vod1/vod/upload/[^"]+)"', block)
        title = re.search(r'carousel-item-title">([^<]+)</div>', block)
        tags = re.findall(r'<span class="tag">([^<]+)</span>', block)
        if img:
            items.append({
                "id": int(vid),
                "title": title.group(1).strip() if title else "",
                "backdrop": img_url(img.group(1)),
                "tags": tags,
            })
    return items


def parse_home(page: str) -> list[dict]:
    """首页：按标题分段解析，和频道页共用抗模板漂移的解析逻辑。"""
    sections = []
    for title, body in _titled_sections(page):
        items = _parse_vod_list(body)
        if title and items:
            sections.append({"title": title, "items": items})
    return sections


def parse_play_page(html: str) -> dict:
    """播放页：提取当前线路的直链地址（playSource 内 src）。

    返回 {"line": 当前线路名, "sources": [{name, url}, ...]}。
    src 为播放页上当前集可用的直链（可能多个清晰度）；线路名取自
    source-item-active 高亮项，避免与全部线路名列表错位。
    """
    # 当前激活线路名（无高亮时取第一个线路标签兜底）
    active = re.search(
        r'<a[^>]*class="[^"]*source-item-active[^"]*"[^>]*>.*?'
        r'<span class="source-item-label">([^<]+)</span>',
        html, re.S)
    line = active.group(1).strip() if active else ""
    if not line:
        names = re.findall(
            r'<span class="source-item-label">([^<]+)</span>', html)
        line = names[0].strip() if names else ""
    # 所有 src 直链（m3u8 / mp4）
    srcs = re.findall(
        r'src:\s*"((?:https?|//)[^"]+?\.(?:m3u8|mp4)[^"]*)"', html)
    sources = []
    for i, url in enumerate(srcs):
        if url.startswith("//"):
            url = "https:" + url
        # 多个清晰度时按序号命名；单个时直接用线路名
        name = f"{line}·清晰{i + 1}" if len(srcs) > 1 else line
        sources.append({"name": name, "url": url})
    return {"line": line, "sources": sources}


def parse_detail(html: str, vid: int = 0) -> dict:
    """详情页：元数据 + 多源选集。vid 由调用方传入（页面内不含主 vod_id）。"""
    titles = re.search(
        r'<div class="detail-title">\s*<strong>[^<]*</strong>\s*<strong>([^<]+)</strong>',
        html)
    title = titles.group(1).strip() if titles else ""
    cover = re.search(r'detail-pic">\s*<img[^>]*data-original="([^"]+)"', html)
    # 标签：类型 / 年份 / 地区
    tags = [t.strip() for t in re.findall(
        r'class="detail-tags-item"[^>]*>([^<]+)</a>', html)]
    year = next((t for t in tags if re.fullmatch(r"\d{4}", t)), "")
    meta = " / ".join(tags)
    # 导演 / 演员
    director = re.search(r'导演:</div>\s*<div class="detail-info-row-main">\s*(.*?)</div>', html, re.S)
    actors = re.search(r'演员:</div>\s*<div class="detail-info-row-main">\s*(.*?)</div>', html, re.S)
    def _clean(block: str | None) -> str:
        if not block:
            return ""
        return re.sub(r"<[^>]+>", "", block).replace("/", " / ").strip()
    # 描述
    desc = re.search(r'<div class="detail-desc">\s*<p>\s*(.*?)\s*</p>', html, re.S)
    d = {
        "id": vid,
        "title": title,
        "cover": img_url(cover.group(1)) if cover else "",
        "score": 0.0,
        "year": year,
        "meta": meta,
        "director": _clean(director.group(1)) if director else "",
        "actors": _clean(actors.group(1)) if actors else "",
        "desc": re.sub(r"&lt;/?br&gt;", " ", re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else "",
        "sources": [],
    }
    # 源 + 对应选集
    sources_html = re.search(r'<div class="source-list-box-main">(.*?)</div>\s*</div>\s*</div>', html, re.S)
    episodes_html = re.search(
        r'<div class="episode-list-box-main">(.*?)</div>\s*</div>\s*</div>\s*</div>', html, re.S)
    if sources_html and episodes_html:
        names = re.findall(
            r'<span class="source-item-label">([^<]+)</span>', sources_html.group(1))
        lists = re.findall(r'<div class="episode-list"[^>]*>(.*?)</div>', episodes_html.group(1), re.S)
        for i, ep_block in enumerate(lists):
            eps = []
            for m in re.finditer(
                r'href="/play/(\d+)-(\d+)-(\d+)\.html"[^>]*>\s*<span>([^<]*)</span>',
                ep_block):
                eps.append({
                    "ep": m.group(4).strip(),
                    "pid": int(m.group(2)),
                    "vid": int(m.group(3)),
                })
            if eps:
                d["sources"].append({
                    "name": names[i] if i < len(names) else f"线路{i + 1}",
                    "episodes": eps,
                })
    # 站点 "4K" 线路（pid=35）是全站占位死线路：播放页 src 恒空（2026-08-14
    # 扫描 12/12 影片验证，登录后同样为空，站点无 VIP/会员体系）。
    # 过滤掉，避免 App 默认选中后必然解析失败。
    d["sources"] = [s for s in d["sources"] if s["name"].strip() != "4K"]
    return d
