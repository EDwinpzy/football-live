"""Application service behind the device-local ``/vod/*`` HTTP API."""

from __future__ import annotations

import copy


class VodService:
    def __init__(self, catalog, registry, resolve_sources=None, resolve_play=None):
        self.catalog = catalog
        self.registry = registry
        self._resolve_sources = resolve_sources or registry.resolve_subject
        self._resolve_sources_waits_for_all = resolve_sources is None
        self._resolve_play = resolve_play
        self._resolved_lines = {}

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
