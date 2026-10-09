#!/usr/bin/env python3
"""生成豆瓣目录快照（首页三段 + 搜索池 + 详情），供离线/云端环境兜底。

为什么必须有这个文件：云端 otv-web 的出口到不了豆瓣，douban_catalog 只能回落
到随包快照；快照为空时影视首页、详情与本地搜索全都会是空的（v1.26 线上现象）。

首页直接复用 douban_catalog 的列表与分类规则；搜索池继续从 Explore 分页收集。
详情按优先级给限定条数补齐，也是云端详情页的唯一数据来源。

用法: python tools/build_douban_snapshot.py [--home-only]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "tools" / "backend-src"
OUT = BACKEND / "douban_snapshot.json"
sys.path.insert(0, str(BACKEND))

import douban_catalog  # noqa: E402


CATEGORIES = (("movie", "电影"), ("tv", "电视剧"), ("anime", "动漫"),
              ("variety", "综艺"), ("short", "短剧"))
SORTS = (("U", "最近热门"), ("R", "最新上映"), ("S", "豆瓣高分"))
PAGES = {"movie": {"U": 4, "R": 3, "S": 3}, "tv": {"U": 4, "R": 3, "S": 3},
         "anime": {"U": 6, "R": 6, "S": 6}, "variety": {"U": 2, "R": 1, "S": 1},
         "short": {"U": 2, "R": 1, "S": 1}}
DETAIL_API = "https://m.douban.com/rexxar/api/v2/subject/%s"


def get(url, sleep, attempts=3):
    """带退避重试地取数；彻底失败返回 None（单页失败不该毁掉整次构建）。"""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            value = douban_catalog.DoubanCatalog._fetch(url)
        except Exception as exc:
            last = exc
            time.sleep(min(8.0, attempt * 2.0))
            continue
        time.sleep(sleep)
        return value
    print("SKIP %s (%s)" % (url[:100], type(last).__name__))
    return None


def explore_page(category, tag, sort_code, page, sleep):
    query = {"sort": sort_code, "range": "0,10", "tags": tag,
             "start": max(0, page - 1) * douban_catalog.EXPLORE_PAGE_SIZE}
    url = douban_catalog.EXPLORE_API + "?" + urllib.parse.urlencode(query)
    for attempt in range(1, 5):
        payload = get(url, sleep, attempts=1)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if rows:
            return [douban_catalog.normalize_explore_row(row, category)
                    for row in rows if isinstance(row, dict)]
        # 豆瓣被限流时会回一个空 data 数组：冷却后重试，别把空页当成「该分类没内容」。
        wait = min(30.0, 8.0 * attempt)
        print("THROTTLED %s p%d, wait %.0fs" % (category, page, wait))
        time.sleep(wait)
    print("EMPTY %s" % url[:110])
    return None


def _previous_section_items(previous, category, title):
    category_value = ((previous or {}).get("home") or {}).get(category) or {}
    for section in category_value.get("sections") or []:
        if isinstance(section, dict) and section.get("title") == title:
            return list(section.get("items") or [])
    return []


def collect_home(sleep, previous=None, details_out=None, source_out=None,
                 require_fresh_movie_tv=False):
    """用在线目录相同的取数路径生成首页；过滤/补页规则由 douban_catalog 维护。"""
    previous = previous if isinstance(previous, dict) else {}
    catalog = douban_catalog.DoubanCatalog(
        fetch=lambda url: get(url, sleep, attempts=1), snapshot=previous)
    previous_details = {}
    for key, item in (previous.get("details") or {}).items():
        normalized_key = str(key or "").removeprefix("douban:")
        if normalized_key and isinstance(item, dict) and item.get("title"):
            previous_details[normalized_key] = item
    fetch_detail = catalog.detail

    def detail_from_snapshot_first(douban_id, force_refresh=False):
        key = str(douban_id or "").removeprefix("douban:")
        cached = previous_details.get(key)
        if cached is not None:
            return cached
        detail = fetch_detail(douban_id, force_refresh=force_refresh)
        if details_out is not None and isinstance(detail, dict) and detail.get("title"):
            details_out[key] = {field: value for field, value in detail.items()
                                if field not in ("source", "stale")}
        return detail

    # 首页规则需要详情判断演出/短剧；3D 动画使用系列目录并仅在列表行缺少产地/类型时补详情。
    catalog.detail = detail_from_snapshot_first
    home = {}
    for category, _tag in CATEGORIES:
        sections = []
        section_sources = {}
        for sort_code, title in SORTS:
            source = ("douban-home-api" if category in ("movie", "tv", "variety")
                      else "douban-explore")
            try:
                items = catalog._section_items(category, sort_code)
            except (douban_catalog.DoubanThrottled, douban_catalog.DoubanUnavailable) as exc:
                if require_fresh_movie_tv and category in ("movie", "tv"):
                    raise SystemExit("首页 %s/%s 未能从豆瓣首页接口刷新，拒绝使用旧栏" %
                                     (category, title))
                items = _previous_section_items(previous, category, title)
                if not items:
                    raise SystemExit("首页 %s/%s 抓取失败，且没有可用的旧条目: %s" %
                                     (category, title, type(exc).__name__))
                source = "snapshot-fallback"
                print("HOME_FALLBACK %s/%s 保留旧条目 %d 条" %
                      (category, title, len(items)))
            sections.append({"title": title, "items": items})
            section_sources[title] = source
        home[category] = {"category": category, "sections": sections}
        if category == "anime":
            ids_before = {section["title"]: [str(item.get("douban_id") or item.get("id") or "")
                                              for item in section["items"]]
                          for section in sections}
            home[category] = catalog._fill_cn3d_home(home[category])
            sections = home[category]["sections"]
            for section in sections:
                ids_after = [str(item.get("douban_id") or item.get("id") or "")
                             for item in section["items"]]
                if ids_after != ids_before.get(section["title"], []):
                    section_sources[section["title"]] = "douban-explore+snapshot-index"
        if source_out is not None:
            source_out[category] = section_sources
        print("CATEGORY %-8s %s" % (category, [len(section["items"]) for section in sections]))
    return home


def collect(sleep, previous=None, home_source_out=None):
    """刷新首页与 Explore 搜索池；首页请求和过滤复用在线目录实现。"""
    home = collect_home(sleep, previous, source_out=home_source_out)
    pool = []
    for category, tag in CATEGORIES:
        for sort_code, title in SORTS:
            for page in range(1, PAGES[category][sort_code] + 1):
                items = explore_page(category, tag, sort_code, page, sleep)
                if items is None:
                    break
                pool.extend(items)
                if len(items) < douban_catalog.EXPLORE_PAGE_SIZE:
                    break
    return home, pool


def dedupe(items):
    unique, seen = [], set()
    for item in items:
        key = str(item.get("douban_id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def priority_ids(home, pool):
    """详情抓取优先级：**首页每一段都先各拿一份配额**（用户直接看得到），再按取数顺序补齐。

    2026-10-01：旧实现按 `CATEGORIES` 顺序把整段列表顺序拼接，movie 三个段（4+3+3=10 页）
    就把 `--details 160` 的预算吃光，tv/anime 之后每段只能拿到个位数，variety/short 直接为 0。
    后果实测（见 douban_catalog.is_placeholder_poster 注释同批排查）：
      - variety/short 的条目没补到详情 → genres/tags 判不出来 → 全被归成 movie；
      - 「最新上映」多为本年度新片，补详情优先级又排在最后 → poster 留豆瓣默认占位图；
      - 元数据残缺（year/aliases 空）→ 详情页 match_score 拿不到标题交集 → 0 条片源。
    现在改成轮转发车：第 1 轮每段各 1 条，第 2 轮每段各 1 条……保证 5 分类 × 3 段
    = 15 段都拿到基础覆盖，预算有余再回头加厚。
    """
    ordered = []
    sections = [section
                for category, _tag in CATEGORIES
                for section in home[category]["sections"]]
    depth = max((len(section["items"]) for section in sections), default=0)
    for rank in range(depth):
        for section in sections:
            items = section["items"]
            if rank < len(items):
                ordered.append(str(items[rank].get("douban_id") or ""))
    ordered.extend(str(item.get("douban_id") or "") for item in pool)
    seen, unique = set(), []
    for key in ordered:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def fetch_details(ids, limit, sleep, category_of):
    details = {}
    target = ids[:max(0, limit)]
    for position, key in enumerate(target, 1):
        payload = get(DETAIL_API % key, sleep, attempts=2)
        if isinstance(payload, dict) and payload.get("title"):
            item = douban_catalog.normalize_subject(payload, category_of.get(key, "movie"))
            if item.get("title"):
                details[key] = item
        if position % 25 == 0:
            print("DETAIL_PROGRESS %d/%d" % (position, len(target)))
    return details


def apply_details(items, details):
    """用详情补齐列表行，同时保留列表接口刚返回的标题、评分与封面。"""
    merged = []
    for item in items:
        detail = details.get(str(item.get("douban_id") or ""))
        if not isinstance(detail, dict):
            merged.append(item)
            continue
        value = dict(detail)
        # 列表接口反映首页当前展示值；详情接口可能暂时滞后，不能把新标题、评分、
        # 或封面回滚。空字段仍由详情补齐（例如没有新评分/剧照的条目）。
        for key in ("title", "year", "rating", "rating_count",
                    "poster_url", "hero_url", "backdrop_url"):
            fresh = item.get(key)
            if key in ("rating", "rating_count"):
                has_fresh = fresh not in (None, "", 0, 0.0)
            elif key in ("poster_url", "hero_url", "backdrop_url"):
                has_fresh = bool(fresh) and not douban_catalog.is_placeholder_poster(fresh)
            else:
                has_fresh = bool(str(fresh or "").strip())
            if has_fresh:
                value[key] = fresh
        merged.append(value)
    return merged


def _douban_id_of(item):
    if not isinstance(item, dict):
        return ""
    return str(item.get("douban_id") or "").removeprefix("douban:") or str(
        item.get("id") or "").removeprefix("douban:")


def append_home_to_index(index, home, details):
    """把首页新条目追加到索引末尾；历史行与顺序不动，跨栏 ID 只存一次。"""
    home_items = []
    for category, _tag in CATEGORIES:
        for section in ((home.get(category) or {}).get("sections") or []):
            if isinstance(section, dict):
                home_items.extend(item for item in (section.get("items") or [])
                                  if isinstance(item, dict))
    details_by_id = {str(key or "").removeprefix("douban:"): item
                     for key, item in (details or {}).items() if isinstance(item, dict)}
    home_items = apply_details(home_items, details_by_id)
    for item in home_items:
        item.pop("detail_meta", None)

    merged, seen = [], set()
    for item in list(index or []) + home_items:
        key = _douban_id_of(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def preserve_history(index, details, previous):
    """保留未出现在本次榜单的历史搜索条目及详情；本次抓取优先。"""
    if not isinstance(previous, dict):
        return list(index or []), dict(details or {})
    merged = list(index or [])
    seen = {_douban_id_of(item) for item in merged}
    seen.discard("")
    fresh_ids = set(seen)
    kept = 0
    for item in previous.get("index") or []:
        key = _douban_id_of(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
        kept += 1
    merged_details = dict(details or {})
    stale_details = previous.get("details")
    if isinstance(stale_details, dict):
        for key, item in stale_details.items():
            skey = str(key or "").removeprefix("douban:")
            if skey and skey not in merged_details and isinstance(item, dict):
                merged_details[skey] = item
    # 仅补历史条目的详情，旧评分/年份不能覆盖刚抓到的列表数据。
    details_by_id = {_douban_id_of(item): item for item in merged_details.values()
                     if isinstance(item, dict)}
    merged = [item if _douban_id_of(item) in fresh_ids else
              details_by_id.get(_douban_id_of(item)) or item for item in merged]
    if kept:
        print("HISTORY 保留上一版搜索池条目 %d 条" % kept)
    return merged, merged_details


def finalize_search_index(index, details, home, previous):
    """保留搜索历史，并保证本次首页条目也进入离线搜索索引。"""
    index, details = preserve_history(index, details, previous)
    return append_home_to_index(index, home, details), details


def load_previous():
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def write_snapshot(payload):
    temp = OUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp.replace(OUT)


def scrub_placeholder_posters(payload):
    """把已入库的豆瓣默认占位海报清成空串。

    `normalize_subject` 已在写入时拦住占位图，但库里可能还留着修复前抓到的旧数据
    （且 `--max-age` 让本脚本在 24h 内直接跳过重建）。占位图会一路下发到所有端，
    所以每次构建都无条件扫一遍，成本只是一次本地遍历。
    """
    cleared = 0

    def scrub(item):
        nonlocal cleared
        if not isinstance(item, dict):
            return
        for key in ("poster_url", "hero_url", "backdrop_url"):
            value = item.get(key)
            if value and douban_catalog.is_placeholder_poster(value):
                item[key] = ""
                cleared += 1

    for category in (payload.get("home") or {}).values():
        for section in (category or {}).get("sections") or []:
            for item in section.get("items") or []:
                scrub(item)
    for item in payload.get("index") or []:
        scrub(item)
    for item in (payload.get("details") or {}).values():
        scrub(item)
    if cleared:
        print("SCRUB 清理豆瓣默认占位海报 %d 处" % cleared)
    return cleared


def previous_index_size():
    return len(load_previous().get("index") or [])


def snapshot_age_seconds():
    try:
        stamped = datetime.fromisoformat(json.loads(OUT.read_text(encoding="utf-8"))["generatedAt"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return (datetime.now(timezone.utc) - stamped).total_seconds()


def home_stats(home):
    return {category: sum(len(section.get("items") or [])
                          for section in ((home.get(category) or {}).get("sections") or []))
            for category, _tag in CATEGORIES}


def refresh_home_only(previous, sleep):
    """只刷新首页栏，index/details 保持上一份快照内容。"""
    if not isinstance(previous, dict) or not previous:
        raise SystemExit("--home-only 需要一份现有快照")
    payload = dict(previous)
    fetched_details = {}
    home_sources = {}
    payload["home"] = collect_home(sleep, previous, details_out=fetched_details,
                                   source_out=home_sources, require_fresh_movie_tv=True)
    payload["homeSource"] = home_sources
    details = dict(payload.get("details") or {})
    for key, item in fetched_details.items():
        details.setdefault(key, item)
    payload["details"] = details
    payload["index"] = append_home_to_index(payload.get("index"), payload["home"], details)
    payload["homeGeneratedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = dict(payload.get("stats") or {})
    stats.update({"home": home_stats(payload["home"]),
                  "index": len(payload.get("index") or []),
                  "details": len(payload.get("details") or {})})
    payload["stats"] = stats
    scrub_placeholder_posters(payload)
    write_snapshot(payload)
    print("WROTE HOME", OUT, OUT.stat().st_size, payload["stats"])
    return payload


def main():
    parser = argparse.ArgumentParser(description="刷新 douban_snapshot.json")
    parser.add_argument("--sleep", type=float, default=0.4, help="请求间隔秒数（豆瓣会限流）")
    # 300 = 5 分类 × 3 段 × 20 条，正好把 15 段全部首页条目补满（轮转发车，见 priority_ids）。
    # 旧默认 160 在顺序发车下只够 movie+tv，variety/short 一条都补不到。
    parser.add_argument("--details", type=int, default=300,
                        help="补详情的条数上限（首页 15 段轮转发车，每段先各拿一份配额）")
    parser.add_argument("--max-age", type=float, default=24 * 3600.0,
                        help="快照比这还新就跳过抓取（0 = 每次强制重建）")
    parser.add_argument("--home-only", action="store_true",
                        help="只刷新首页三段，保留现有 index 和 details")
    parser.add_argument("--force", action="store_true", help="即使结果明显缩水也写入")
    args = parser.parse_args()

    previous_snapshot = load_previous()
    if args.home_only:
        refresh_home_only(previous_snapshot, args.sleep)
        return

    age = snapshot_age_seconds()
    if args.max_age > 0 and age is not None and age < args.max_age:
        print("SKIP 现有快照 %.1f 小时前生成，加 --max-age 0 可强制重建" % (age / 3600.0))
        # 跳过重抓 ≠ 跳过修复：占位图是本地数据问题，不该等下次豆瓣抓取才清掉。
        if scrub_placeholder_posters(previous_snapshot):
            write_snapshot(previous_snapshot)
        return

    home_sources = {}
    home, pool = collect(args.sleep, previous_snapshot, home_source_out=home_sources)
    index = dedupe(pool)
    if not index:
        # 宁可保留旧快照让发布继续，也不要写入空快照把线上影视搬空。
        raise SystemExit("本次未抓到任何豆瓣条目，拒绝写入空快照")

    category_of = {str(item.get("douban_id") or ""): item.get("category") or "movie" for item in index}
    details = fetch_details(priority_ids(home, pool), args.details, args.sleep, category_of)
    for category, _tag in CATEGORIES:
        for section in home[category]["sections"]:
            section["items"] = apply_details(section["items"], details)
    index = apply_details(index, details)
    stats = home_stats(home)
    print("首页三段条数:", stats, "搜索池:", len(index), "详情:", len(details))
    previous = len(previous_snapshot.get("index") or [])
    if previous and len(index) < previous * 0.3 and not args.force:
        raise SystemExit("本次只抓到 %d 条（上次 %d 条），疑似被限流；确认无误可加 --force"
                         % (len(index), previous))
    index, details = finalize_search_index(index, details, home, previous_snapshot)

    payload = {
        "schema": 1,
        "source": "douban",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "homeGeneratedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "home": home,
        "homeSource": home_sources,
        "index": index,
        "details": details,
        "stats": {"home": stats, "index": len(index), "details": len(details)},
    }
    scrub_placeholder_posters(payload)
    write_snapshot(payload)
    print("WROTE", OUT, OUT.stat().st_size, payload["stats"])


if __name__ == "__main__":
    main()
