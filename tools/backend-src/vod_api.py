"""Application service behind the device-local ``/vod/*`` HTTP API."""

from __future__ import annotations

import copy
import os
import queue
import threading
import time

import vod_resolver
import vod_sources


"""首页预热（2026-10-14）：用户从首页点到详情通常要 3~10 秒，这段时间后台把首屏那几部
影片的线路先解析好，等他真点进去时命中缓存 → 首次打开接近 0ms。

边界（都要守住）：
- 只在首屏第一段里取前 PREFETCH_MAX_TITLES 部，串行解析，每部之间停 PREFETCH_GAP_SECONDS；
- 已经缓存过的跳过；同一部在 PREFETCH_COOLDOWN_SECONDS 内只排一次；
- 连续失败（上游限流/失联）就退避 PREFETCH_FAILURE_BACKOFF_SECONDS，不硬刚；
- 请求线程只做入队，绝不等后台；
- ``OTV_VOD_PREFETCH=0`` 可整体关掉（排障/省流）。
"""
PREFETCH_MAX_TITLES = 4
PREFETCH_COOLDOWN_SECONDS = 900
PREFETCH_GAP_SECONDS = 1.5
PREFETCH_FAILURE_BACKOFF_SECONDS = 60


class VodService:
    def __init__(self, catalog, registry, resolve_sources=None, resolve_play=None,
                 fallback_sources=(), resolver=None):
        self.catalog = catalog
        self.registry = registry
        self.resolver = resolver
        self._resolve_sources = resolve_sources or registry.resolve_subject
        self._uses_registry_resolver = resolve_sources is None
        self._resolve_play = resolve_play
        # 片源兜底链：本地匹配为空时按顺序试这些 provider，第一个给出线路的就用。
        # 自建 VPS 出口到不了片源站（实测 Cloudflare/海外全超时），所以线上用的是
        # 「本机解析好、推到 VPS 的静态索引」；旧的云端回源链路保留为可选最后一环。
        # 任一 provider 返回空或抛异常都继续往下走，绝不改变本地匹配成功的既有行为。
        self._fallback_sources = tuple(fallback_sources)
        self._resolved_lines = {}
        self._prefetch_queue = queue.Queue()
        self._prefetch_lock = threading.Lock()
        self._prefetch_seen = {}
        self._prefetch_thread = None
        self._prefetch_enabled = os.environ.get("OTV_VOD_PREFETCH", "1").strip() != "0"
        self.prefetch_calls = 0

    # ------------------------------------------------------------ 首页预热

    def prefetch_candidates(self, payload, limit=PREFETCH_MAX_TITLES):
        """从首页载荷里挑出要预热的 subject（首屏第一段，最多 limit 部）。纯函数。"""
        sections = (payload or {}).get("sections") or []
        for section in sections:
            items = (section or {}).get("items") or []
            candidates = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                candidates.append({
                    "title": title,
                    "year": str(item.get("year") or ""),
                    "category": str(item.get("category") or ""),
                    "season": int(item.get("season") or 0),
                })
                if len(candidates) >= max(0, int(limit)):
                    break
            if candidates:
                return candidates
        return []

    def prefetch_home(self, payload):
        """请求线程只入队；缓存已命中的、刚排过的、关掉开关的都不排。"""
        if not self._prefetch_enabled or not self._uses_registry_resolver:
            return 0
        enqueued = 0
        now = time.time()
        for subject in self.prefetch_candidates(payload):
            key = vod_sources._subject_cache_key(subject)
            if not key:
                continue
            with self._prefetch_lock:
                last = float(self._prefetch_seen.get(key) or 0)
                if now - last < PREFETCH_COOLDOWN_SECONDS:
                    continue
                self._prefetch_seen[key] = now
            if self.registry.has_cached_subject(subject):
                continue
            self._prefetch_queue.put(subject)
            enqueued += 1
        if enqueued:
            self._ensure_prefetch_worker()
        return enqueued

    def _ensure_prefetch_worker(self):
        with self._prefetch_lock:
            if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
                return
            self._prefetch_thread = threading.Thread(
                target=self._prefetch_loop, name="vod-prefetch", daemon=True)
            self._prefetch_thread.start()

    def _prefetch_loop(self):
        failures = 0
        while True:
            try:
                subject = self._prefetch_queue.get(timeout=PREFETCH_GAP_SECONDS)
            except queue.Empty:
                return  # 闲下来就退出线程，下次入队再起
            if subject is None:
                return
            try:
                lines = self._resolve_sources(subject, first_playable=True) or []
            except Exception:  # noqa: BLE001 - 预热失败只退避，绝不冒到请求线程
                lines = []
            self.prefetch_calls += 1
            if lines:
                failures = 0
            else:
                failures += 1
            if failures:
                # 上游限流/失联：退避一段再继续，别把源站打毛。
                time.sleep(min(PREFETCH_FAILURE_BACKOFF_SECONDS * failures, 300))
            else:
                time.sleep(PREFETCH_GAP_SECONDS)

    def home(self, category, refresh=False):
        payload = self.catalog.refresh_home(category) if refresh else self.catalog.home(category)
        self.prefetch_home(payload)
        return payload

    def _adopt_external_lines(self, sources):
        """把外部兜底线路翻译成本机 registry 的 source_id。

        2026-09-24 自建 VPS 实测：云端（CloudBase）的影视源 id 是 ``hongniu`` 这类
        slug，而自建后台管的是数据库里的 UUID 主键，两边名字一样、id 不同。直接合并
        的话 ``/vod/play`` 会以「影视源不存在」失败（实测 400）。这里按源名把外部
        线路落到本机同名源上；本机没有的源（例如没配的「好好看」）直接丢掉，避免
        播放器反复撞上必然失败的线路。本机 registry 为空时不做翻译，原样保留。
        """
        local = [(str(getattr(item, "id", "")), str(getattr(item, "name", "")).strip())
                 for item in self.registry.sources]
        known_ids = {source_id for source_id, _ in local if source_id}
        by_name = {name: source_id for source_id, name in local if name}
        lines = [line for line in (sources or []) if isinstance(line, dict)]
        if not known_ids or not lines:
            return lines
        adopted = []
        for line in lines:
            source_id = str(line.get("source_id") or "")
            if source_id in known_ids:
                adopted.append(line)
                continue
            target = by_name.get(str(line.get("source_name") or "").strip())
            if target:
                adopted.append(dict(line, source_id=target))
        return adopted

    def filters(self, category):
        return self.catalog.filters(category)

    def show(self, category, query):
        value = lambda name, default="": (query.get(name) or [default])[0]
        return self.catalog.show(
            category=category, genre=value("genre"), region=value("region"),
            year=value("year"), rating=value("rating"), sort=value("sort", "hot"),
            page=int(value("page", "1") or 1),
        )

    def search(self, query, page=1, limit=30):
        return self.catalog.search(query, page=page, limit=limit)

    def detail(self, douban_id, defer_sources=False, refresh=False):
        catalog_detail = self.catalog.refresh_detail if refresh else self.catalog.detail
        subject = copy.deepcopy(catalog_detail(douban_id))
        subject["id"] = "douban:" + str(douban_id).removeprefix("douban:")
        if defer_sources:
            subject["source_state"] = "matching"
            subject["sources"] = []
            return subject
        if self._uses_registry_resolver:
            resolver = self.registry.refresh_subject if refresh else self._resolve_sources
            # 首条「实测可播」线路出现即返回（2026-10-14）：
            # - 不再按「谁先出线」收队——快源给的死链会把详情页钉死在 2 条不可播线路上；
            # - 源一出线就并行探测它的前几条线路，探测到可播立刻返回，首开 ≈ 0.9s；
            # - 首源全死则继续等下一台源，未探测的线路保持 alive=None（不冒充可播）；
            # - 被甩下的源/线路后台补完并进缓存，再打开详情页就是全量线路。
            sources = resolver(subject, first_playable=True) or []
        else:
            if refresh:
                self.registry.invalidate_subject(subject)
            sources = self._resolve_sources(subject) or []
        if not sources:
            for provider in self._fallback_sources:
                try:
                    candidate = provider(subject, douban_id)
                except Exception:
                    candidate = None
                candidate = self._adopt_external_lines(candidate)
                if candidate:
                    sources = candidate
                    break
        # 按实测可用性排序：LineHistory 一直在记录每条线路的成功/失败，
        # 但此前没有用于排序，导致起播从 line 0 逐条试错（实测 78.8s 才
        # 出画）。验证过的线排前面，用户不必每次先撞一遍坏线。
        sources = vod_sources.rank_lines_by_health(sources, self.registry.history)
        self._resolved_lines[str(douban_id)] = {str(line.get("id")): copy.deepcopy(line) for line in sources}
        subject["sources"] = sources
        subject["source_state"] = "ready" if sources else "unavailable"
        return subject

    def _record_label_outcome(self, douban_id, line_id, ok):
        """播放成功/失败也喂标签历史（探测只覆盖前几条，播放是全量线路的判决来源）。"""
        label_history = getattr(self.registry, "label_history", None)
        if label_history is None:
            return
        line = (self._resolved_lines.get(str(douban_id)) or {}).get(str(line_id)) or {}
        name = str(line.get("name") or "")
        if name:
            label_history.record(name, bool(ok))

    def play(self, douban_id, line_id, episode):
        try:
            if self._resolve_play:
                result = self._resolve_play(douban_id, line_id, episode)
            else:
                line = self._resolved_lines.get(str(douban_id), {}).get(str(line_id))
                result = self.registry.resolve_line(line, episode) if line else None
        except Exception:
            if self.registry.history:
                self.registry.history.record(line_id, False)
            self._record_label_outcome(douban_id, line_id, False)
            raise
        if not result:
            if self.registry.history:
                self.registry.history.record(line_id, False)
            self._record_label_outcome(douban_id, line_id, False)
            raise LookupError("播放线路不可用")
        if self.registry.history:
            self.registry.history.record(line_id, True)
        self._record_label_outcome(douban_id, line_id, True)
        return result

    def resolve(self, mytv_id, episode=1):
        """Return the URL-free resolver contract for a canonical My TV ID."""
        if self.resolver is None:
            return vod_resolver.ResolveResult(
                status="TEMPORARILY_UNAVAILABLE",
                mytv_id=int(mytv_id),
                episode=int(episode),
                resolve_version=None,
                preferred=None,
                backups=(),
                cache={"kind": "miss", "age_ms": 0},
            ).to_public_dict()
        return self.resolver.resolve(mytv_id=mytv_id, episode=episode).to_public_dict()

    def source_status(self):
        return {"version": self.registry.version, "sources": [
            {"id": source.id, "name": source.name, "enabled": source.enabled,
             "adapter_type": source.adapter_type}
            for source in self.registry.sources
        ]}
