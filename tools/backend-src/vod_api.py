"""Application service behind the device-local ``/vod/*`` HTTP API."""

from __future__ import annotations

import copy


class VodService:
    def __init__(self, catalog, registry, resolve_sources=None, resolve_play=None,
                 fallback_sources=()):
        self.catalog = catalog
        self.registry = registry
        self._resolve_sources = resolve_sources or registry.resolve_subject
        self._resolve_sources_waits_for_all = resolve_sources is None
        self._resolve_play = resolve_play
        # 片源兜底链：本地匹配为空时按顺序试这些 provider，第一个给出线路的就用。
        # 自建 VPS 出口到不了片源站（实测 Cloudflare/海外全超时），所以线上用的是
        # 「本机解析好、推到 VPS 的静态索引」；旧的云端回源链路保留为可选最后一环。
        # 任一 provider 返回空或抛异常都继续往下走，绝不改变本地匹配成功的既有行为。
        self._fallback_sources = tuple(fallback_sources)
        self._resolved_lines = {}

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

    def home(self, category, refresh=False):
        return self.catalog.refresh_home(category) if refresh else self.catalog.home(category)

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
        if self._resolve_sources_waits_for_all:
            resolver = self.registry.refresh_subject if refresh else self._resolve_sources
            sources = resolver(subject, wait_for_all=True) or []
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
        self._resolved_lines[str(douban_id)] = {str(line.get("id")): copy.deepcopy(line) for line in sources}
        subject["sources"] = sources
        subject["source_state"] = "ready" if sources else "unavailable"
        return subject

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
            raise
        if not result:
            if self.registry.history:
                self.registry.history.record(line_id, False)
            raise LookupError("播放线路不可用")
        if self.registry.history:
            self.registry.history.record(line_id, True)
        return result

    def source_status(self):
        return {"version": self.registry.version, "sources": [
            {"id": source.id, "name": source.name, "enabled": source.enabled,
             "adapter_type": source.adapter_type}
            for source in self.registry.sources
        ]}
