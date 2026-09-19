"""Playback-source registry, discovery, matching and quality ranking."""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import concurrent.futures
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_PATH = Path(__file__).with_name("vod_sources.default.json")
RUNTIME_PATH = Path(__file__).with_name("vod_sources.runtime.json")
AUTO_MATCH_THRESHOLD = 80
TITLE_SEPARATORS = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
PRIVATE_HOSTS = {"localhost", "localhost.localdomain"}
"""代理 fake-IP 承载段：Clash / Surge / sing-box 默认把域名映射到 198.18.0.0/15，
真实连接由本机代理完成。国内用户装这类代理是常态，若把这段当「内网」拦掉，
除直连 IP 外的影视源会全灭（表现为只剩「好好看」一个源）。字面量 IP 落在这段仍拦。"""
PROXY_FAKE_IP_RANGES = (ipaddress.ip_network("198.18.0.0/15"),)
"""源匹配重试：9 个源同时失联（弱网/代理抖动/出口 DNS 抽风）不是「没有这部片」，
但旧实现直接回空列表，用户看到的是「没有信号源 / 无法播放」。出现异常时重试一次。"""
RESOLVE_ATTEMPTS = 2
RESOLVE_RETRY_DELAY = 0.6
RESOLVE_MAX_WORKERS = 12
"""竞速早返回（2026-09-19 起）：批次不再被最慢源拖到其超时（MacCMS 8s / hhkan 12s）。
搜索随到随判：≥RESOLVE_EARLY_SOURCES 台源出有效线路即进探测，剩余源后台补完；
探测同理：首个可播线路出现后再宽限 PROBE_EARLY_GRACE 秒收束排名质量，
或凑够 PROBE_EARLY_EXIT_ALIVE 条可播即返回。RESOLVE_TOTAL_BUDGET 是总预算硬顶，
到点拿已有最优结果返回，永不比旧「全等」实现更慢。后台补完的结果进短 TTL 缓存
（RESOLVE_CACHE_TTL），同片再查直接给全量。"""
RESOLVE_TOTAL_BUDGET = 10.0
RESOLVE_EARLY_SOURCES = 2
RESOLVE_CACHE_TTL = 1800
"""探测预算：9 个源全通时线路能到 40+ 条，逐条探测会把详情拖到 15~40 秒（实测），
而播放器只展示综合排名前 5。这里按源轮转取前 N 条探测，其余保留「未探测」状态
（alive=None，排序垫后但不判死），既保住排序质量又压住耗时。"""
PROBE_MAX_LINES = 18
PROBE_MAX_WORKERS = 18
PROBE_TIMEOUT_SECONDS = 4
PROBE_EARLY_EXIT_ALIVE = 3
PROBE_EARLY_GRACE = 0.8
"""源质量画像 + 熔断：连续 BREAKER_CONSEC_FAILS 次搜索失败 → 冷却
BREAKER_COOLDOWN_SECONDS 秒内跳过该源（冷却到期自动放行试探，成功即复位）；
延迟用半衰期 STATS_EMA_HALFLIFE_SECONDS 的 EMA，老数据自动失权。"""
BREAKER_CONSEC_FAILS = 3
BREAKER_COOLDOWN_SECONDS = 1800
STATS_EMA_HALFLIFE_SECONDS = 7 * 86400


class UnsafeSourceUrl(ValueError):
    pass


@dataclass(frozen=True)
class VodSource:
    id: str
    name: str
    site_url: str
    api_url: str
    adapter_type: str
    enabled: bool = True
    revision: int = 1


class SourceRegistry:
    def __init__(self, sources=(), version=1, history=None, probe=None, stats=None):
        self.sources = tuple(sources)
        self.version = int(version)
        self.history = history
        self.probe = probe or probe_stream
        self.stats = stats
        self._resolve_cache = {}
        self._cache_lock = threading.Lock()

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        sources = []
        for raw in payload.get("sources") or []:
            if not isinstance(raw, dict):
                continue
            sources.append(VodSource(
                id=str(raw.get("id") or "").strip(),
                name=str(raw.get("name") or "").strip(),
                site_url=str(raw.get("site_url") or "").strip(),
                api_url=str(raw.get("api_url") or "").strip(),
                adapter_type=str(raw.get("adapter_type") or "").strip(),
                enabled=bool(raw.get("enabled", True)),
                revision=int(raw.get("revision") or 1),
            ))
        return cls(sources, payload.get("version") or 1)

    @classmethod
    def load_default(cls):
        return cls.from_dict(json.loads(DEFAULT_PATH.read_text(encoding="utf-8")))

    @classmethod
    def load_current(cls):
        try:
            runtime = cls.from_dict(json.loads(RUNTIME_PATH.read_text(encoding="utf-8")))
            if runtime.sources:
                return runtime
        except (OSError, ValueError, TypeError):
            pass
        return cls.load_default()

    def to_dict(self):
        return {"schema": 1, "version": self.version, "sources": [asdict(source) for source in self.sources]}

    def resolve_subject(self, subject):
        """Search enabled adapters and return only confident, normalized lines.

        竞速契约（2026-09-19）：源按画像得分排序发车、熔断源跳过；≥EARLY_SOURCES 台
        出有效线路即进探测，首个可播线路 + 短宽限即返回；总预算硬顶 RESOLVE_TOTAL_BUDGET。
        早返回时被甩下的源/线路在后台线程补完，结果进 RESOLVE_CACHE_TTL 短缓存，
        并喂给 SourceStats 画像——弱源不再挡路，但每次失败都会变成熔断的证据。

        重试契约：只有当**确有源抛异常**（弱网/代理抖动导致整批失联）时才补一次，
        避免用户看到「没有信号源 / 无法播放」；源都正常应答、只是标题匹配不上时
        立刻返回空，不做无谓的二次等待。
        """
        enabled = [source for source in self.sources if source.enabled]
        if not enabled:
            return []
        stats = self.stats
        if stats:
            active = [source for source in enabled if not stats.is_circuit_open(source.id)]
            # 全部被熔断时忽略熔断器照常发车：宁可慢，不能没片看。
            enabled = stats.sort_sources(active or enabled)
        cache_key = _subject_cache_key(subject)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        deadline = time.monotonic() + max(1.0, float(RESOLVE_TOTAL_BUDGET))
        lines, pending_searches = [], []
        for attempt in range(max(1, RESOLVE_ATTEMPTS)):
            lines, failures, pending_searches = self._race_search(enabled, subject, deadline, stats)
            if lines or not failures or attempt + 1 >= max(1, RESOLVE_ATTEMPTS):
                break
            time.sleep(RESOLVE_RETRY_DELAY)

        picked = _pick_probe_lines(lines, PROBE_MAX_LINES)
        pending_probes = self._race_probe(picked, deadline)

        if pending_searches or pending_probes:
            # 早返回：剩余搜索/探测后台补完（自带着各自的网络超时兜底），
            # 合并全量进缓存；响应先行，不等。
            threading.Thread(
                target=self._complete_in_background,
                args=(cache_key, list(lines), pending_searches, pending_probes, stats),
                daemon=True,
            ).start()
        elif lines:
            self._cache_put(cache_key, lines)
            if stats:
                stats.flush()

        history = ({line["id"]: self.history.success_rate(line["id"]) for line in lines}
                   if self.history else None)
        return rank_lines(lines, history)

    def _search_one(self, source, subject):
        started = time.monotonic()
        try:
            adapter = HhkanAdapter(source) if source.adapter_type == "hhkan" else MacCmsAdapter(source)
            candidates = adapter.search(subject)
            best = max(candidates, key=lambda item: match_score(subject, item), default=None)
            if not best or match_score(subject, best) < AUTO_MATCH_THRESHOLD:
                return source, [], None, _elapsed_ms(started)
            return source, adapter.detail(best), None, _elapsed_ms(started)
        except Exception as exc:  # noqa: BLE001 - 单源失败不拖累整批，但要记下来
            return source, [], str(exc)[:120], _elapsed_ms(started)

    def _race_search(self, enabled, subject, deadline, stats):
        """并行搜索，as_completed 随到随判；≥EARLY_SOURCES 台出线或到点即收队。
        返回 (lines, failures, pending_futures)：pending 是未等到的 future，由后台补完。"""
        workers = min(RESOLVE_MAX_WORKERS, len(enabled))
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        futures = [pool.submit(self._search_one, source, subject) for source in enabled]
        pending = list(futures)
        lines, failures = [], []
        sources_with_lines = 0
        try:
            for future in concurrent.futures.as_completed(
                    futures, timeout=max(0.1, deadline - time.monotonic())):
                pending.remove(future)
                _source, group, error, elapsed = future.result()
                if error is not None:
                    failures.append(error)
                if stats:
                    stats.record_search(_source.id, error is None, elapsed)
                if group:
                    sources_with_lines += 1
                    lines.extend(group)
                if sources_with_lines >= max(1, RESOLVE_EARLY_SOURCES):
                    break
        except concurrent.futures.TimeoutError:
            pass
        pool.shutdown(wait=False)
        return lines, failures, pending

    def _inspect_line(self, line):
        try:
            source = next(item for item in self.sources if item.id == line.get("source_id"))
            adapter = HhkanAdapter(source) if source.adapter_type == "hhkan" else MacCmsAdapter(source)
            resolved = adapter.resolve(line, 0)
            line.update(self.probe(resolved.get("url")))
        except Exception:
            line.update({"alive": False, "latency_ms": None, "height": 0})
        return line

    def _race_probe(self, picked, deadline):
        """并行探测；凑够 EARLY_EXIT_ALIVE 条可播、或首个可播后宽限期满、或到点即收队。
        返回未等到的 future 列表（后台补完用）。"""
        if not picked:
            return []
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(PROBE_MAX_WORKERS, len(picked)))
        futures = [pool.submit(self._inspect_line, line) for line in picked]
        pending = list(futures)
        alive = 0
        first_alive_at = None
        try:
            for future in concurrent.futures.as_completed(
                    futures, timeout=max(0.1, deadline - time.monotonic())):
                pending.remove(future)
                line = future.result()
                if line.get("alive"):
                    alive += 1
                    if first_alive_at is None:
                        first_alive_at = time.monotonic()
                if alive >= max(1, PROBE_EARLY_EXIT_ALIVE):
                    break
                if (first_alive_at is not None
                        and time.monotonic() - first_alive_at >= max(0.0, PROBE_EARLY_GRACE)):
                    break
        except concurrent.futures.TimeoutError:
            pass
        pool.shutdown(wait=False)
        return pending

    def _complete_in_background(self, cache_key, lines, pending_searches, pending_probes, stats):
        """早返回后的补完线程：等齐迟到源 + 迟到探测，探测新到的线路，
        合并全量进短缓存并落画像。任何异常都不允许回传到请求线程。"""
        try:
            late_lines = []
            for future in concurrent.futures.as_completed(pending_searches):
                source, group, error, elapsed = future.result()
                if stats:
                    stats.record_search(source.id, error is None, elapsed)
                if group:
                    late_lines.extend(group)
            for future in concurrent.futures.as_completed(pending_probes):
                future.result()
            lines.extend(late_lines)
            late_picked = _pick_probe_lines(late_lines, PROBE_MAX_LINES)
            if late_picked:
                pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(PROBE_MAX_WORKERS, len(late_picked)))
                try:
                    list(pool.map(self._inspect_line, late_picked))
                finally:
                    pool.shutdown(wait=False)
            if lines:
                self._cache_put(cache_key, lines)
            if stats:
                stats.flush()
        except Exception:  # noqa: BLE001 - 后台补完是锦上添花，挂了只丢缓存不丢响应
            pass

    def _cache_get(self, key):
        if not key:
            return None
        with self._cache_lock:
            item = self._resolve_cache.get(key)
        if not item or time.time() - item[0] > max(60, RESOLVE_CACHE_TTL):
            return None
        return copy.deepcopy(item[1])

    def _cache_put(self, key, lines):
        if not key or not lines:
            return
        with self._cache_lock:
            self._resolve_cache[key] = (time.time(), copy.deepcopy(lines))

    def resolve_line(self, line, episode_index):
        source = next((item for item in self.sources if item.id == line.get("source_id")), None)
        if not source:
            raise LookupError("影视源不存在")
        adapter = HhkanAdapter(source) if source.adapter_type == "hhkan" else MacCmsAdapter(source)
        return adapter.resolve(line, episode_index)


def normalize_title(value):
    return TITLE_SEPARATORS.sub("", str(value or "")).casefold()


def _titles(item):
    values = [item.get("title"), item.get("original_title")]
    values.extend(item.get("aliases") or [])
    return {normalize_title(value) for value in values if normalize_title(value)}


def _year(value):
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group(0)) if match else None


def match_score(subject, candidate):
    if not (_titles(subject) & _titles(candidate)):
        return 0
    score = 60
    subject_year, candidate_year = _year(subject.get("year")), _year(candidate.get("year"))
    hard_year_conflict = bool(subject_year and candidate_year and abs(subject_year - candidate_year) > 1)
    if subject_year and candidate_year:
        score += 20 if subject_year == candidate_year else 10 if abs(subject_year - candidate_year) == 1 else 0
    subject_category, candidate_category = subject.get("category"), candidate.get("category")
    if subject_category and subject_category == candidate_category:
        score += 10
    subject_season, candidate_season = int(subject.get("season") or 0), int(candidate.get("season") or 0)
    if subject_season == candidate_season:
        score += 10
    return min(score, 79) if hard_year_conflict else min(score, 100)


def quality_score(height):
    height = int(height or 0)
    return 100 if height >= 2160 else 85 if height >= 1080 else 65 if height >= 720 else 35 if height > 0 else 20


def latency_score(milliseconds):
    if milliseconds is None:
        return 0
    milliseconds = int(milliseconds)
    return 100 if milliseconds <= 1000 else 80 if milliseconds <= 3000 else 50 if milliseconds <= 6000 else 20


def rank_lines(lines, history=None):
    history = history or {}
    ranked = []
    for raw in lines:
        line = copy.deepcopy(raw)
        alive = line.get("alive") is not False
        reliability = history.get(str(line.get("id")), line.get("success_rate", 0.5))
        reliability = max(0.0, min(1.0, float(reliability)))
        line["available"] = alive
        line["score"] = round(
            0.50 * quality_score(line.get("height")) +
            0.30 * latency_score(line.get("latency_ms")) +
            0.20 * (reliability * 100), 2)
        ranked.append(line)
    ranked.sort(key=lambda item: (item["available"], item["score"]), reverse=True)
    return ranked


class LineHistory:
    def __init__(self, path):
        self.path = Path(path)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def attempts(self, line_id):
        return list(self.data.get(str(line_id)) or [])[-20:]

    def success_rate(self, line_id):
        values = self.attempts(line_id)
        return sum(bool(value) for value in values) / len(values) if values else 0.5

    def record(self, line_id, success):
        key = str(line_id)
        self.data[key] = (self.attempts(key) + [bool(success)])[-20:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


class SourceStats:
    """源质量画像：搜索成功率 + EMA 延迟 + 连续失败熔断，JSON 原子落盘。

    只统计「搜索通道」（源能否正常应答 + 多快）：熔断解决的是「弱源把并行批次
    拖到超时」；单条线路死活由 LineHistory（播放时记账）和探测排名负责，不混在这里。
    熔断到期自动放行试探，成功即 consec_fail 复位；再失败重新冷却。
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._dirty = False
        self.data = {}
        if self.path:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                self.data = {}

    def record_search(self, source_id, ok, latency_ms=None):
        now = time.time()
        with self._lock:
            entry = self.data.setdefault(str(source_id), {})
            entry["ok"] = int(entry.get("ok") or 0) + (1 if ok else 0)
            entry["fail"] = int(entry.get("fail") or 0) + (0 if ok else 1)
            entry["consec_fail"] = 0 if ok else int(entry.get("consec_fail") or 0) + 1
            if ok:
                entry.pop("cooldown_until", None)  # 试探成功即闭合熔断器
            elif entry["consec_fail"] >= max(1, BREAKER_CONSEC_FAILS):
                entry["cooldown_until"] = now + max(60, BREAKER_COOLDOWN_SECONDS)
            if ok and latency_ms is not None:
                ema = entry.get("ema_ms")
                if ema is None:
                    entry["ema_ms"] = float(latency_ms)
                else:
                    elapsed = max(0.0, now - float(entry.get("updated") or now))
                    alpha = max(0.2, 1.0 - 0.5 ** (elapsed / STATS_EMA_HALFLIFE_SECONDS))
                    entry["ema_ms"] = float(ema) + alpha * (float(latency_ms) - float(ema))
            entry["updated"] = now
            self._dirty = True

    def is_circuit_open(self, source_id, now=None):
        with self._lock:
            entry = self.data.get(str(source_id)) or {}
            until = float(entry.get("cooldown_until") or 0)
        return bool(until and (now if now is not None else time.time()) < until)

    def score(self, source_id):
        """排序分（越高越先发车）：成功率 0.6 + 延迟 0.4。新源两项都取中性 0.5。"""
        with self._lock:
            entry = self.data.get(str(source_id)) or {}
        ok = int(entry.get("ok") or 0)
        fail = int(entry.get("fail") or 0)
        success = (ok + 2) / (ok + fail + 4)  # 先验平滑：无数据 = 0.5，不被一两次失败打死
        ema = entry.get("ema_ms")
        if ema is None:
            speed = 0.5
        elif ema <= 1000:
            speed = 1.0
        elif ema <= 3000:
            speed = 0.8
        elif ema <= 6000:
            speed = 0.5
        else:
            speed = 0.2
        return 0.6 * success + 0.4 * speed

    def sort_sources(self, sources):
        # sorted 稳定：同分保持配置文件里的先后顺序
        return sorted(sources, key=lambda source: self.score(source.id), reverse=True)

    def flush(self):
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
            self._dirty = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


def _subject_cache_key(subject):
    return "%s|%s|%s|%s" % (
        normalize_title(subject.get("title")), subject.get("year") or "",
        subject.get("category") or "", int(subject.get("season") or 0))


def _pick_probe_lines(lines, limit):
    """按源轮转挑选要探测的线路：每台源先拿一条代表线，再补第二条……
    否则线路最多的那台（好好看常 30+ 条）会把探测预算占满，别的源永远排不上。
    未被挑中的线路保持未探测（alive=None）：排序垫后但不算判死。"""
    limit = int(limit or 0)
    if limit <= 0 or len(lines) <= limit:
        return list(lines)
    buckets = {}
    for line in lines:
        buckets.setdefault(str(line.get("source_id") or ""), []).append(line)
    picked = []
    while len(picked) < limit:
        progressed = False
        for key in list(buckets):
            bucket = buckets[key]
            if bucket and len(picked) < limit:
                picked.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break
    return picked


def _is_proxy_fake_ip(address):
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return False
    return any(ip in network for network in PROXY_FAKE_IP_RANGES)


def _validate_public_url(url, resolve_dns=False):
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeSourceUrl("影视源地址必须是 http/https 公网 URL")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in PRIVATE_HOSTS:
        raise UnsafeSourceUrl("影视源地址不能指向本机或内网")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        if resolve_dns:
            try:
                addresses = {row[4][0] for row in socket.getaddrinfo(hostname, parsed.port or 443)}
            except socket.gaierror as exc:
                raise UnsafeSourceUrl("影视源域名无法解析") from exc
            if not addresses:
                raise UnsafeSourceUrl("影视源域名无法解析")
            # 域名全部落在代理 fake-IP 段 = 本机 DNS 被透明代理接管，连出去的是真实站点，
            # 不是内网服务。全部命中才放行；混入其他非公网地址仍按内网拦截。
            if all(_is_proxy_fake_ip(address) for address in addresses):
                return url
            if any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise UnsafeSourceUrl("影视源域名解析到非公网地址")
    else:
        if not ip.is_global:
            raise UnsafeSourceUrl("影视源地址不能指向本机或内网")
    return url


def _default_fetch(url):
    _validate_public_url(url, resolve_dns=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json, application/xml, text/html"})
    with urllib.request.urlopen(request, timeout=8) as response:
        final_url = response.geturl()
        _validate_public_url(final_url, resolve_dns=True)
        return {"final_url": final_url, "content_type": response.headers.get_content_type(), "body": response.read(512 * 1024).decode("utf-8", "replace")}


def _probe_fetch(url):
    _validate_public_url(url, resolve_dns=True)
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.apple.mpegurl,*/*"})
    with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
        _validate_public_url(response.geturl(), resolve_dns=True)
        body = response.read(256 * 1024).decode("utf-8", "replace")
    return {"elapsed_ms": int((time.monotonic() - started) * 1000), "body": body}


def probe_stream(url, fetch=None):
    """Probe the first playlist response and derive the best advertised resolution."""
    if not str(url or "").startswith(("http://", "https://")):
        return {"alive": False, "latency_ms": None, "height": 0}
    try:
        result = (fetch or _probe_fetch)(url)
        heights = [int(value) for value in re.findall(r"RESOLUTION=\d+x(\d+)", str(result.get("body") or ""), re.I)]
        return {"alive": True, "latency_ms": int(result.get("elapsed_ms") or 0),
                "height": max(heights, default=0)}
    except Exception:
        return {"alive": False, "latency_ms": None, "height": 0}


def discover_source(site_url, fetch=None):
    site_url = str(site_url or "").strip().rstrip("/")
    _validate_public_url(site_url)
    fetch = fetch or _default_fetch
    source_id = normalize_title(urllib.parse.urlsplit(site_url).hostname).replace("_", "-")[:40] or "source"
    candidates = [site_url + "/api.php/provide/vod/", site_url + "/provide/vod/"]
    for api_url in candidates:
        result = fetch(api_url + "?ac=list")
        final_url = result.get("final_url") or api_url
        _validate_public_url(final_url)
        body = str(result.get("body") or "").lstrip()
        content_type = str(result.get("content_type") or "").lower()
        if "json" in content_type or body.startswith("{"):
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and ("code" in payload or "list" in payload or "class" in payload):
                    return VodSource(source_id, urllib.parse.urlsplit(site_url).hostname, site_url, api_url, "macms_json")
            except ValueError:
                pass
        if "xml" in content_type or body.startswith("<?xml") or body.startswith("<rss"):
            return VodSource(source_id, urllib.parse.urlsplit(site_url).hostname, site_url, api_url, "macms_xml")
    raise ValueError("未识别到兼容的 MacCMS 影视接口")


class MacCmsAdapter:
    def __init__(self, source, fetch=None):
        self.source = source
        self.fetch = fetch or _default_fetch

    def search(self, subject):
        # ac=detail 才稳定返回 vod_play_from/vod_play_url；videolist 在不少站点只给摘要。
        query = urllib.parse.urlencode({"ac": "detail", "wd": subject.get("title") or ""})
        result = self.fetch(self.source.api_url + "?" + query)
        payload = json.loads(result.get("body") or "{}")
        candidates = []
        for item in payload.get("list") or []:
            type_name = str(item.get("type_name") or "")
            category = ("anime" if any(x in type_name for x in ("动漫", "动画")) else
                        "variety" if any(x in type_name for x in ("综艺", "真人秀")) else
                        "tv" if any(x in type_name for x in ("剧", "连续")) else "movie")
            candidates.append({"id": str(item.get("vod_id") or ""), "title": item.get("vod_name") or "", "year": item.get("vod_year") or "", "category": category, "season": 0, "raw": item})
        return candidates

    def detail(self, candidate):
        raw = candidate.get("raw") or {}
        play_urls = str(raw.get("vod_play_url") or "").split("$$$")
        play_names = str(raw.get("vod_play_from") or "").split("$$$")
        lines = []
        for index, group in enumerate(play_urls):
            episodes = []
            for episode_index, value in enumerate(group.split("#")):
                value = value.strip()
                if not value:
                    continue
                if "$" in value:
                    name, url = value.rsplit("$", 1)
                else:
                    name, url = "第%d集" % (episode_index + 1), value
                url = url.strip()
                if not url.startswith(("http://", "https://")):
                    continue
                episodes.append({"index": len(episodes), "name": name.strip() or "第%d集" % (episode_index + 1), "url": url})
            if episodes:
                label = play_names[index].strip() if index < len(play_names) else "线路%d" % (index + 1)
                lines.append({
                    "id": "%s:%s:%d" % (self.source.id, candidate.get("id"), index),
                    "source_id": self.source.id, "source_name": self.source.name,
                    "name": "%s·%s" % (self.source.name, label), "episodes": episodes,
                    "height": 0, "latency_ms": None, "alive": True,
                })
        return lines

    def resolve(self, line, episode_index):
        episodes = line.get("episodes") or []
        if not episodes:
            raise LookupError("线路没有可播放集")
        index = max(0, min(int(episode_index), len(episodes) - 1))
        episode = episodes[index]
        return {"line_id": line.get("id"), "name": episode.get("name"),
                "url": episode.get("url"), "headers": {}}


class HhkanAdapter:
    def __init__(self, source, module=None):
        self.source = source
        if module is None:
            import hhkan as module
        self.module = module

    def search(self, subject):
        title = str(subject.get("title") or "").strip()
        token = self.module.get_search_token()
        path = "/search?t=%s&k=%s&p=1" % (
            urllib.parse.quote(token), urllib.parse.quote(title))
        html = self.module.get_page(path)
        candidates = []
        for match in re.finditer(r'href=["\']/detail/(\d+)\.html["\'][^>]*>(.*?)</a>', html, re.I | re.S):
            block = match.group(2)
            title_match = re.search(r'class=["\']title["\'][^>]*>([^<]+)', block, re.I)
            candidates.append({"id": match.group(1), "title": (title_match.group(1).strip() if title_match else title),
                               "year": subject.get("year") or "", "category": subject.get("category"),
                               "season": subject.get("season") or 0})
        return candidates

    def detail(self, candidate):
        vid = int(candidate.get("id") or 0)
        detail = self.module.parse_detail(self.module.get_page("/detail/%d.html" % vid), vid=vid)
        lines = []
        for index, raw in enumerate(detail.get("sources") or []):
            episodes = []
            for episode_index, episode in enumerate(raw.get("episodes") or []):
                episodes.append({"index": episode_index, "name": episode.get("ep") or "第%d集" % (episode_index + 1),
                                 "pid": int(episode.get("pid") or 0), "vid": int(episode.get("vid") or vid)})
            if episodes:
                pid = episodes[0]["pid"]
                lines.append({"id": "%s:%d:%d" % (self.source.id, vid, pid), "source_id": self.source.id,
                              "source_name": self.source.name, "name": "%s·%s" % (self.source.name, raw.get("name") or "线路%d" % (index + 1)),
                              "vod_id": vid, "episodes": episodes, "height": 0, "latency_ms": None, "alive": True})
        return lines

    def resolve(self, line, episode_index):
        episodes = line.get("episodes") or []
        if not episodes:
            raise LookupError("线路没有可播放集")
        episode = episodes[max(0, min(int(episode_index), len(episodes) - 1))]
        path = "/play/%d-%d-%d.html" % (int(line.get("vod_id") or 0), episode["pid"], episode["vid"])
        parsed = self.module.parse_play_page(self.module.get_page(path))
        streams = parsed.get("sources") or []
        if not streams:
            raise LookupError("播放线路不可用")
        selected = streams[-1]
        return {"line_id": line.get("id"), "name": episode.get("name"), "url": selected.get("url"), "headers": {}}
