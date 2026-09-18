#!/usr/bin/env python3
"""探测豆瓣 Explore 各分类真正支持哪些筛选维度，写入 douban_filter_matrix.json。

为什么需要它：`douban_catalog` 用「分类 × 维度」能力矩阵决定某个维度交给上游筛还是本地
兜底判。代码注释说「电视剧传 genres= 会回空页」，但 2026-09-18 线上记录里
`/vod/show/tv?genre=古装` 又返回了正常结果 —— 注释和实测互相矛盾，所以不能靠读代码下
结论，只能实测。矩阵是数据文件：豆瓣改了语义就重跑本脚本，不用改代码。

判定方式（不看上游是否「接受」参数，只看结果是否真的被筛过）：
  请求带该参数 → 取回若干行 → 逐行检查列表行自带的元数据（card_subtitle 解析出的
  年份/地区/类型）是否与该参数一致。全部一致才算「上游可信」；结果为空或行内元数据
  不足以确认，一律记 False（保守：本地兜底判，宁可少给不给错）。

用法: python tools/probe_douban_filter_matrix.py
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
OUT = BACKEND / "douban_filter_matrix.json"
sys.path.insert(0, str(BACKEND))

import douban_catalog  # noqa: E402


CATEGORIES = ("movie", "tv", "anime", "variety", "short")
# 每个维度用一个「一定能筛出东西」的取值；类型取各分类都有的常见值。
PROBE_VALUES = {
    "genre": {"movie": ("剧情", "喜剧", "动作"), "tv": ("剧情", "爱情", "古装"),
              "anime": ("动画", "奇幻", "冒险"), "variety": ("真人秀", "音乐"),
              "short": ("剧情", "爱情")},
    "region": ("日本", "美国"),
    "year": ("更早",),
}
PROBE_ROWS = 20


def query_url(category, send, genre="", region="", year=""):
    query = {"sort": "U", "range": "0,10", "tags": douban_catalog.EXPLORE_TAGS[category], "start": 0}
    if "genre" in send and genre:
        query["genres"] = genre
    if "region" in send and region:
        query["countries"] = region
    if "year" in send and year:
        span = douban_catalog.year_range_value(year)
        if span:
            query["year_range"] = span
    return douban_catalog.EXPLORE_API + "?" + urllib.parse.urlencode(query)


def fetch_rows(fetch, category, send, genre="", region="", year=""):
    payload = fetch(query_url(category, send, genre, region, year))
    rows = douban_catalog.explore_rows(payload)
    return [douban_catalog.normalize_explore_row(row, category)
            for row in rows if isinstance(row, dict)]


def confirms(rows, dimension, value):
    """行内元数据是否逐条确认了该筛选值（这才是「上游真的筛了」的证据）。"""
    if not rows:
        return False
    field = douban_catalog.LOCAL_FIELDS.get(dimension)
    if dimension == "year":
        cutoff = douban_catalog.early_year_cutoff()
        years = [str(row.get("year") or "")[:4] for row in rows]
        return all(year.isdigit() and int(year) <= cutoff for year in years)
    if not field:
        return False
    for row in rows:
        values = row.get(field) or []
        if value not in values:
            return False
    return True


def probe(fetch, sleep):
    matrix = {}
    for category in CATEGORIES:
        matrix[category] = {}
        for dimension in douban_catalog.FILTER_DIMENSIONS:
            if dimension == "genre":
                candidates = PROBE_VALUES["genre"].get(category, ("剧情",))
            else:
                candidates = PROBE_VALUES[dimension]
            honored = False
            for value in candidates:
                kwargs = {dimension: value}
                try:
                    rows = fetch_rows(fetch, category, douban_catalog.FILTER_DIMENSIONS, **kwargs)
                except douban_catalog.DoubanUnavailable as exc:
                    raise SystemExit("上游不可用（%s）：%s" % (category, exc))
                time.sleep(sleep)
                if confirms(rows, dimension, value):
                    honored = True
                    break
                if not rows:
                    # 该取值筛不出内容 —— 也许只是豆瓣没有这类条目，换个取值再试。
                    continue
            matrix[category][dimension] = honored
            print("PROBE %-8s %-7s -> %s" % (category, dimension, honored))
    return matrix


def main():
    parser = argparse.ArgumentParser(description="刷新 douban_filter_matrix.json")
    parser.add_argument("--sleep", type=float, default=0.6, help="请求间隔秒数（豆瓣会限流）")
    args = parser.parse_args()

    matrix = probe(douban_catalog.DoubanCatalog._fetch, args.sleep)
    payload = {
        "_note": "豆瓣 Explore 各分类的筛选能力实测结果；由 tools/probe_douban_filter_matrix.py 生成。"
                 "false = 该维度不可信上游，必须本地兜底判定。",
        "_probedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **matrix,
    }
    temp = OUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(OUT)
    print("WROTE", OUT, payload)


if __name__ == "__main__":
    main()
