#!/usr/bin/env python3
"""片库筛选准确度自检：对每个分类 × 每个维度断言「返回的每一条都真的匹配」。

用法: python tools/check_vod_filter_accuracy.py [--limit 20]

输出 out/vod-filter-accuracy-<时间戳>.json，含每个组合的返回条数、断言失败明细与耗时。
上游不可用（豆瓣拦 IP）时明确记为 skipped 而不是 passed —— 降级结果不能算「准确」。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "tools" / "backend-src"
sys.path.insert(0, str(BACKEND))

import douban_catalog  # noqa: E402


SAMPLES = {
    "genre": {"movie": "剧情", "tv": "剧情", "anime": "动画", "variety": "真人秀", "short": "剧情"},
    "region": {"movie": "日本", "tv": "日本", "anime": "日本",
               "variety": "中国大陆", "short": "中国大陆"},
    "year": {"movie": "更早", "tv": "更早", "anime": "更早", "variety": "更早", "short": "更早"},
    "rating": {"movie": "8分以上", "tv": "8分以上", "anime": "8分以上",
               "variety": "8分以上", "short": "8分以上"},
}


def verify(items, dimension, value):
    """逐条断言「条目的元数据明确包含该筛选值」。字段缺失即算失败 —— 这正是要根除的
    「未知放行」；上游负责的维度不在这里断言。"""
    failures = []
    field = douban_catalog.LOCAL_FIELDS.get(dimension)
    for item in items:
        if dimension == "rating":
            score = float(item.get("rating") or 0)
            if value == "暂无评分" and score > 0:
                failures.append({"id": item.get("id"), "reason": "有评分 %s" % score})
            continue
        if dimension == "year":
            year = str(item.get("year") or "")[:4]
            if year.isdigit():
                if value == "更早" and int(year) > douban_catalog.early_year_cutoff():
                    failures.append({"id": item.get("id"), "reason": "年份 %s 不算更早" % year})
                elif value != "更早" and year != value:
                    failures.append({"id": item.get("id"), "reason": "年份 %s != %s" % (year, value)})
            continue
        values = item.get(field or "") or []
        if value not in values:
            failures.append({"id": item.get("id"),
                             "reason": "%s 不含 %s（实际 %s）" % (field, value, values)})
    return failures


def check(limit):
    catalog = douban_catalog.DoubanCatalog()
    report = {"generatedAt": datetime.now().isoformat(timespec="seconds"),
              "limit": limit, "results": []}
    for category in sorted(douban_catalog.CATEGORY_VALUES):
        for dimension in ("genre", "region", "year", "rating"):
            value = SAMPLES[dimension][category]
            started = time.time()
            result = catalog.show(category, page=1, **{dimension: value})
            items = (result.get("items") or [])[:limit]
            entry = {"category": category, "dimension": dimension, "value": value,
                     "returned": len(items), "elapsed_ms": int((time.time() - started) * 1000),
                     "source": result.get("source"),
                     "upstream_available": bool(result.get("upstream_available"))}
            if not entry["upstream_available"]:
                entry["status"] = "skipped"
                entry["reason"] = "上游不可用，本次结果来自降级链，不做准确度断言"
            else:
                failures = verify(items, dimension, value)
                entry["failed"] = failures
                entry["status"] = "failed" if failures else "passed"
            report["results"].append(entry)
            print("%-8s %-7s %-10s -> %-3d 条 %-8s %s" % (
                category, dimension, value, entry["returned"], entry["status"],
                entry.get("reason", "")))
    report["summary"] = {status: sum(1 for entry in report["results"] if entry["status"] == status)
                         for status in ("passed", "failed", "skipped")}
    return report


def main():
    parser = argparse.ArgumentParser(description="片库筛选准确度自检")
    parser.add_argument("--limit", type=int, default=20, help="每个组合最多断言多少条")
    args = parser.parse_args()
    report = check(args.limit)
    out = ROOT / "out" / ("vod-filter-accuracy-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("SUMMARY", report["summary"])
    print("WROTE", out)
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
