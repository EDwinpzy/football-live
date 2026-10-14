#!/usr/bin/env python3
"""影视点播「首开耗时 + 可播性」基准。

对同一批影片分别跑四条口径，输出可直接对比的表格：

===========  ============================================================
v62          旧策略：第一台出线的源立刻返回，不做任何播放地址探测
full         全等：等齐所有源 + 按源轮转探测前 18 条（最全、最慢）
fast         新策略：首条**实测可播**线路出现即返回（未探测线路标 alive=None）
disk         新策略 + 落盘缓存命中（模拟 App 冷启动后的首次打开）
===========  ============================================================

指标口径（都指「从发起请求到返回」的墙钟时间）：
  detail_ms    详情解析耗时
  lines        返回的线路条数
  playable     其中 alive is True（**真探测过**且可播）的条数
  first_url_ms 拿到第一条可播播放地址的耗时 = detail + 逐条 resolve_line + 探测

**每个组合都在独立子进程里跑**：早返回策略会把没等到的源留在后台线程里继续跑，
同一进程内连着测第二个口径会变成「两个好好看搜索互相抢」，实测会把 3s 的搜索
拖到 10s+，测出来的数不是策略的差异。子进程隔离 + 组合间停顿才能测准。

用法（必须在能直连片源站的网络下跑；本机装 Clash 时脚本会自动绕开系统代理）：

    python tools/qa/vod_load_bench.py
    python tools/qa/vod_load_bench.py --titles 3 --pause 3 --json out.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request


BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend-src"
sys.path.insert(0, str(BACKEND))

import vod_sources  # noqa: E402


DEFAULT_SUBJECTS = [
    {"title": "肖申克的救赎", "year": "1994", "category": "movie", "season": 0},
    {"title": "狂飙", "year": "2023", "category": "tv", "season": 0},
    {"title": "庆余年", "year": "2019", "category": "tv", "season": 0},
    {"title": "甄嬛传", "year": "2011", "category": "tv", "season": 0},
    {"title": "年会不能停！", "year": "2023", "category": "movie", "season": 0},
]
POLICIES = ("v62", "fast", "full", "disk")


def bypass_system_proxy():
    """本机装了 Clash 这类代理时 urllib 会继承系统代理并被 SSRF 防护拒绝。

    基准必须在「源站直连」的前提下测，否则测的是代理而不是源。
    """
    urllib.request.getproxies = lambda: {}
    import hhkan
    hhkan._safe_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        hhkan._SafeRedirectHandler, hhkan._SafeBoundedHTTPHandler, hhkan._SafeBoundedHTTPSHandler)


def fresh_registry(workdir, disk_path=None):
    registry = vod_sources.SourceRegistry.load_default()
    registry.history = vod_sources.LineHistory(pathlib.Path(workdir) / "history.json")
    registry.stats = vod_sources.SourceStats(pathlib.Path(workdir) / "stats.json")
    if disk_path is not None:
        registry.enable_disk_cache(disk_path)
    return registry


def resolve_once(registry, subject, policy):
    if policy == "v62":
        return registry.resolve_subject(
            dict(subject), wait_for_all=False, early_exit_source_count=1, probe_lines=False)
    if policy == "full":
        return registry.resolve_subject(dict(subject), wait_for_all=True, probe_lines=True)
    return registry.resolve_subject(dict(subject), first_playable=True)


def measure(subject, policy, workdir, disk_path=None):
    registry = fresh_registry(workdir, disk_path)
    started = time.monotonic()
    try:
        lines = resolve_once(registry, subject, policy)
    finally:
        detail_ms = int((time.monotonic() - started) * 1000)

    # 真实用户指标：拿到第一条可播播放地址要多久。找不到可播线路时记为 None。
    started = time.monotonic()
    first_url_ms = None
    first_url_source = None
    for line in lines:
        try:
            resolved = registry.resolve_line(line, 1)
        except Exception:
            continue
        if not resolved or not resolved.get("url"):
            continue
        if registry.probe(resolved["url"]).get("alive"):
            first_url_ms = int((time.monotonic() - started) * 1000)
            first_url_source = line.get("source_name")
            break

    row = {
        "policy": policy,
        "title": subject["title"],
        "detail_ms": detail_ms,
        "lines": len(lines),
        "playable": sum(1 for line in lines if line.get("alive") is True),
        "unknown": sum(1 for line in lines if line.get("alive") is None),
        "dead": sum(1 for line in lines if line.get("alive") is False),
        "first_url_ms": first_url_ms,
        "first_url_source": first_url_source,
        "sources": sorted({line.get("source_name") for line in lines if line.get("source_name")}),
    }
    registry.close()
    return row


def _disk_has_entry(disk_path, subject):
    try:
        payload = json.loads(pathlib.Path(disk_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return vod_sources._subject_cache_key(subject) in (payload.get("entries") or {})


def render(rows):
    out = ["| 口径 | 影片 | 详情耗时 | 线路数 | 实测可播 | 未探测 | 实测不可播 | 首条可播地址 | 来源 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        out.append("| %s | %s | %d ms | %d | %d | %d | %d | %s | %s |" % (
            row["policy"], row["title"], row["detail_ms"], row["lines"],
            row["playable"], row["unknown"], row["dead"],
            "-" if row["first_url_ms"] is None else "%d ms" % row["first_url_ms"],
            row["first_url_source"] or "-"))
    return "\n".join(out)


def summarize(rows):
    out = ["| 口径 | 平均详情耗时 | 平均线路数 | 平均实测可播 | 返回可播线路的影片 |",
           "|---|---|---|---|---|"]
    for policy in POLICIES:
        group = [row for row in rows if row["policy"] == policy]
        if not group:
            continue
        usable = sum(1 for row in group if row["playable"] > 0)
        out.append("| %s | %d ms | %.1f | %.1f | %d/%d |" % (
            policy,
            sum(row["detail_ms"] for row in group) / len(group),
            sum(row["lines"] for row in group) / len(group),
            sum(row["playable"] for row in group) / len(group),
            usable, len(group)))
    return "\n".join(out)


def run_child(policy, title_index, workdir, disk_path, warm=False):
    command = [sys.executable, str(pathlib.Path(__file__).resolve()),
               "--child", policy, str(title_index), "--workdir", workdir]
    if disk_path:
        command += ["--disk-path", disk_path]
    if warm:
        command.append("--warm")
    done = subprocess.run(command, capture_output=True, text=True, timeout=300)
    payload = [line for line in (done.stdout or "").splitlines() if line.startswith("{")]
    if not payload:
        raise RuntimeError("子进程没有输出结果（%s / %s）：%s" % (
            policy, title_index, (done.stderr or "")[-500:]))
    return json.loads(payload[-1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--titles", type=int, default=3, help="测前 N 部影片（默认 3）")
    parser.add_argument("--pause", type=float, default=3.0, help="组合之间的停顿秒数")
    parser.add_argument("--json", default="", help="把明细写到这个 JSON 文件")
    parser.add_argument("--policies", default=",".join(POLICIES))
    parser.add_argument("--keep-proxy", action="store_true", help="不绕开系统代理（默认绕开）")
    parser.add_argument("--child", nargs=2, metavar=("POLICY", "TITLE_INDEX"))
    parser.add_argument("--workdir", default="")
    parser.add_argument("--disk-path", default="")
    parser.add_argument("--warm", action="store_true", help="预热跑（允许写落盘缓存）")
    args = parser.parse_args()

    if not args.keep_proxy:
        bypass_system_proxy()

    if args.child:
        policy, title_index = args.child[0], int(args.child[1])
        subject = DEFAULT_SUBJECTS[title_index]
        workdir = args.workdir or tempfile.mkdtemp()
        # 只有 disk 口径（和预热跑）才允许读写落盘缓存，否则别的口径测的是缓存不是策略。
        disk_path = args.disk_path if (args.warm or policy == "disk") and args.disk_path else None
        row = measure(subject, policy, workdir, disk_path=disk_path)
        print(json.dumps(row, ensure_ascii=False))
        return

    subjects = DEFAULT_SUBJECTS[:max(1, args.titles)]
    policies = [item.strip() for item in args.policies.split(",") if item.strip()]
    rows = []
    with tempfile.TemporaryDirectory() as workdir:
        disk_path = str(pathlib.Path(workdir) / "vod_resolve_cache.json")
        for index, _subject in enumerate(subjects):
            for policy in policies:
                if policy == "disk":
                    # 预热一次新策略（允许写盘），再测冷实例命中耗时。
                    run_child("fast", index, workdir, disk_path, warm=True)
                    deadline = time.monotonic() + 30
                    while (time.monotonic() < deadline
                           and not _disk_has_entry(disk_path, subjects[index])):
                        time.sleep(0.5)
                rows.append(run_child(policy, index, workdir, disk_path))
                print(".", end="", flush=True)
                time.sleep(max(0.0, args.pause))
    print()
    print(summarize(rows))
    print()
    print(render(rows))
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n明细已写入 %s" % args.json)


if __name__ == "__main__":
    main()
