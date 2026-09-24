#!/usr/bin/env python
"""在本机解析影视线路，生成可推到服务器的静态片源索引。

为什么需要它：自建 VPS 的出口到不了片源站 API（Cloudflare / 海外 IP 全部连接超时，
实测 18 个采集站全 000），服务器侧匹配必然为空；而本机（家宽）能正常解析。
所以把「解析」放在能上网的这台机器上，把结果当静态索引推到 VPS，
VPS 的 `proxy.py` 在本地匹配为空时读这份索引（`OTV_VOD_LINE_INDEX_URL`），
从而彻底去掉对 CloudBase 回源的依赖。

用法（在本机仓库根执行）：

    python tools/build_vod_line_index.py --limit 40          # 先索引前 40 部（首页详情）
    python tools/build_vod_line_index.py --all               # 索引全部详情（耗时较久）
    python tools/build_vod_line_index.py --ids 36660844,1291546   # 追加指定豆瓣 id
    python tools/build_vod_line_index.py --workers 6 --out artifacts/vod-lines.json

结果默认写到 `artifacts/vod-lines.json`（本机、不入库）。上传到 VPS：

    scp -i <key> -P <port> artifacts/vod-lines.json root@<vps>:/srv/mytv/objects/hotupdate/vod-lines.json

上传后网页版影视页即可在服务器匹配为空时用这份索引出线路；索引过期（默认 30 天）
或没命中就继续回落「暂无片源」。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "tools" / "backend-src"
sys.path.insert(0, str(BACKEND))

import vod_sources  # noqa: E402

SNAPSHOT = BACKEND / "douban_snapshot.json"
DEFAULT_OUT = ROOT / "artifacts" / "vod-lines.json"
LINE_KEYS = (
    "id", "source_id", "source_name", "name", "episodes", "height", "alive",
    "latency_ms", "score", "match_score",
)


def load_subjects(snapshot_path: pathlib.Path) -> list[dict]:
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    # index 是完整目录（565 条，含标题/别名/年份/类型），details 只是其中已补详情的子集
    source = payload.get("index") or payload.get("details") or {}
    records = source.values() if isinstance(source, dict) else source
    subjects = []
    for item in records:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        subjects.append({
            "key": str(item.get("douban_id") or item.get("id") or "").removeprefix("douban:"),
            "title": item.get("title") or "",
            "aliases": item.get("aliases") or [],
            "year": item.get("year") or "",
            "category": item.get("category") or "",
            "season": item.get("season") or 0,
        })
    return [subject for subject in subjects if subject["key"]]


def trim_line(line: dict) -> dict:
    out = {key: line.get(key) for key in LINE_KEYS if key in line}
    episodes = []
    for episode in out.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        episode_index = episode.get("index")
        if isinstance(episode_index, bool) or not isinstance(episode_index, int) or episode_index < 0:
            continue
        episodes.append({
            "index": episode_index,
            "name": episode.get("name") or "第%d集" % (episode_index + 1),
            "url": episode.get("url") or "",
        })
    out["episodes"] = [episode for episode in episodes if episode["url"]]
    return out


def resolve_one(registry, subject: dict) -> tuple[str, dict]:
    started = time.time()
    try:
        lines = registry.resolve_subject(subject, wait_for_all=True) or []
    except Exception as exc:  # 单部失败不影响整批
        return subject["key"], {"error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    kept = []
    for line in lines:
        trimmed = trim_line(line)
        if trimmed.get("episodes"):
            kept.append(trimmed)
    return subject["key"], {
        "title": subject["title"],
        "year": subject["year"],
        "category": subject["category"],
        "season": subject["season"],
        "resolved_at": int(time.time()),
        "elapsed_ms": int((time.time() - started) * 1000),
        "lines": kept,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成静态片源索引（本机解析，推到服务器）")
    parser.add_argument("--limit", type=int, default=40, help="索引前 N 部详情（默认 40）")
    parser.add_argument("--all", action="store_true", help="索引全部详情，忽略 --limit")
    parser.add_argument("--ids", default="", help="额外索引的豆瓣 id，逗号分隔")
    parser.add_argument("--workers", type=int, default=4, help="并发解析数（默认 4）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 JSON 路径")
    parser.add_argument("--snapshot", default=str(SNAPSHOT), help="豆瓣快照路径")
    parser.add_argument("--force", action="store_true", help="忽略已有结果，全部重解析")
    parser.add_argument("--retry-empty", action="store_true", help="重试上次没解析出线路的条目")
    args = parser.parse_args()

    out_path = pathlib.Path(args.out)
    existing: dict = {}
    if out_path.exists() and not args.force:
        try:
            existing = (json.loads(out_path.read_text(encoding="utf-8")).get("entries") or {})
        except (OSError, ValueError):
            existing = {}

    subjects = load_subjects(pathlib.Path(args.snapshot))
    lookup = {subject["key"]: subject for subject in subjects}
    if not args.all:
        subjects = subjects[: max(0, args.limit)]
    by_key = {subject["key"]: subject for subject in subjects}
    for extra in filter(None, (item.strip() for item in args.ids.split(","))):
        # 目录里有就用目录里的标题/年份（matcher 靠标题搜索，光有 id 搜不出东西）
        by_key.setdefault(extra, lookup.get(extra) or {
            "key": extra, "title": "", "aliases": [], "year": "", "category": "", "season": 0})
    todo = []
    for key, subject in by_key.items():
        cached = existing.get(key)
        if isinstance(cached, dict):
            if cached.get("lines"):
                continue
            if not args.retry_empty:
                continue
        todo.append(subject)

    print("待解析 %d 部（已有 %d 条，输出 %s）" % (len(todo), len(existing), out_path), flush=True)
    if not todo:
        return 0

    registry = vod_sources.SourceRegistry.load_current()
    entries = dict(existing)
    done = 0
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(resolve_one, registry, subject): subject for subject in todo}
        for future in concurrent.futures.as_completed(futures):
            key, entry = future.result()
            done += 1
            if entry.get("error"):
                print("[%d/%d] %s 失败：%s" % (done, len(todo), key, entry["error"]), flush=True)
                continue
            entries[key] = entry
            print("[%d/%d] %s %s 线路=%d 集数=%d 用时=%.1fs" % (
                done, len(todo), key, entry["title"], len(entry["lines"]),
                sum(len(line["episodes"]) for line in entry["lines"]),
                entry["elapsed_ms"] / 1000.0), flush=True)
            payload = {
                "schema": 1,
                "generator": "build_vod_line_index",
                "generated_at": int(time.time()),
                "snapshot": pathlib.Path(args.snapshot).name,
                "entries": entries,
            }
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # 原子替换：边跑边写时，外部（scp / 读取方）不会看到半截 JSON
            tmp_path = out_path.with_name(out_path.name + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, out_path)

    hit = sum(1 for entry in entries.values() if isinstance(entry, dict) and entry.get("lines"))
    print("完成：条目 %d（其中有线路 %d），总用时 %.1f 分钟，输出 %s" % (
        len(entries), hit, (time.time() - started) / 60.0, out_path), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
