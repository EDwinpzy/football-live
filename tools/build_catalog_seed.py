#!/usr/bin/env python3
"""从随包豆瓣快照生成 App 首装离线目录种子（assets/catalog_seed.json）。

背景（2026-09-18 修）：种子还是退役的「好好看」格式（v:1 / s:"hhkan"），被
VodRepository.decodeSnapshot 的豆瓣来源校验整条拒绝——首装那一瞬没有离线目录可上屏，
用户进影视页看到「片库加载失败，请稍后重试」，日志打
「内置种子 已损坏或含非好好看卡片，跳过」。种子只在「磁盘快照不存在（首装）」时用，
网络目录一落地就被替换。

字段口径与 App 自己写盘时逐字对齐（VodRepository.writeSnapshot）：
  v=3（CatalogOriginPolicy.SNAPSHOT_VERSION）
  categories=[{id:"douban:<key>",name}]，items=[{id,s,t,c,y,ar,r,p,h,rm,d}]
  p = 海报：豆瓣图床走内置后端中继（与 VodApiSource.parseItems/relayPoster 同一规则）

快照刷新后必须重跑（release_all.py 已串进流程，避免种子再次过期）。
用法: python tools/build_catalog_seed.py
"""
import json
import pathlib
import re
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "tools" / "backend-src"
sys.path.insert(0, str(BACKEND))

import douban_catalog  # noqa: E402

CATEGORIES = (("movie", "电影"), ("tv", "电视剧"), ("anime", "3D国漫"),
              ("variety", "综艺"), ("short", "短剧"))
SNAPSHOT_VERSION = 3                      # = VodRepository.CatalogOriginPolicy.SNAPSHOT_VERSION
RELAY_BASE = "http://127.0.0.1:8090"      # App 默认内置后端（海报中继）
TARGETS = (
    ROOT / "android" / "app" / "src" / "main" / "assets" / "catalog_seed.json",
    ROOT / "internal" / "mobile" / "app" / "src" / "main" / "assets" / "catalog_seed.json",
)
DOUBAN_IMG_RE = re.compile(r"^https?://(?:img\d+\.doubanio\.com|asset\.doubanio\.com)/",
                           re.IGNORECASE)


def relay_poster(raw: str) -> str:
    """与客户端 relayPoster 同规则：豆瓣图床经内置后端中继（直连会被防盗链拦）。"""
    url = (raw or "").strip()
    if len(url) < 10 or not url.startswith("http"):
        return url
    if not DOUBAN_IMG_RE.match(url):
        return url
    return RELAY_BASE + "/hhkan/proxy?u=" + urllib.parse.quote(url, safe="")


class _NoCache:
    """空缓存：让 DoubanCatalog 走「冷缓存 → 立刻回随包快照」这条路径（不访问豆瓣）。"""

    @staticmethod
    def get(_key, _ttl):
        return None

    @staticmethod
    def get_stale(_key):
        return None


def build() -> dict:
    catalog = douban_catalog.DoubanCatalog(cache=_NoCache())
    items, seen = [], set()
    for key, _name in CATEGORIES:
        data = catalog.home(key)
        for section in (data.get("sections") or []):
            for card in (section.get("items") or []):
                if not isinstance(card, dict):
                    continue
                raw_id = str(card.get("id") or "").removeprefix("douban:")
                title = str(card.get("title") or "").strip()
                if not raw_id or not title:
                    continue
                vid = "douban:" + raw_id
                if vid in seen:
                    continue
                seen.add(vid)
                items.append({
                    "id": vid,
                    "s": "douban",
                    "t": title,
                    "c": "douban:" + key,
                    "y": str(card.get("year") or ""),
                    "ar": " / ".join(card.get("regions") or []),
                    "r": float(card.get("rating") or 0),
                    "p": relay_poster(str(card.get("poster_url") or "")),
                    "h": relay_poster(
                        douban_catalog.hero_poster_url(
                            str(card.get("hero_url") or card.get("poster_url") or "")
                        ) or str(card.get("hero_url") or card.get("poster_url") or "")
                    ),
                    "rm": "",
                    "d": raw_id,
                })
    return {
        "v": SNAPSHOT_VERSION,
        "ts": int(time.time() * 1000),
        "categories": [{"id": "douban:" + key, "name": name} for key, name in CATEGORIES],
        "items": items,
    }


def main() -> None:
    seed = build()
    if not seed["categories"] or not seed["items"]:
        raise SystemExit("生成结果为空，拒绝覆盖现有种子")

    # 内容没变就沿用旧的 ts，保证同样的输入产出同样的字节。
    # 否则每次跑构建都会刷新时间戳，即使 270 条目录一字未改，产物哈希也会变，
    # 于是"APK 内嵌种子 == 仓库种子"这条发布后自检永远对不上，只能靠人工解释。
    # ts 的用途只是告诉 App 种子有多新，不代表内容版本，所以这样复用是安全的。
    for target in TARGETS:
        if not target.exists():
            continue
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        old = {k: v for k, v in previous.items() if k != "ts"}
        new = {k: v for k, v in seed.items() if k != "ts"}
        if old == new and isinstance(previous.get("ts"), int):
            seed["ts"] = previous["ts"]
            break

    blob = json.dumps(seed, ensure_ascii=False, separators=(",", ":")) + "\n"
    for target in TARGETS:
        target.write_text(blob, encoding="utf-8")
        print("已写入 %s（%d 条 / %d 分类，%.1f KB）"
              % (target.relative_to(ROOT), len(seed["items"]), len(seed["categories"]),
                 len(blob.encode("utf-8")) / 1024))


if __name__ == "__main__":
    main()
