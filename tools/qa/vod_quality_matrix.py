#!/usr/bin/env python3
"""Broad, URL-safe search and playback smoke matrix for a local My TV backend.

By default the script only accepts a loopback backend. Pass --allow-remote to
probe another environment intentionally. Reports omit signed media URLs and
retain only item identity, source name, hostname, status, MIME type and timings.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_QUERIES = [
    "红楼梦", "西游记", "三国演义", "水浒传", "甄嬛传",
    "琅琊榜", "狂飙", "庆余年", "声生不息", "漫长的季节",
]
USER_AGENT = "MyTV-VOD-Quality-Matrix/1.0"
MAX_BODY = 2_000_000


def _read_url(url: str, timeout: float, referer: str = "") -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BODY)
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "body": body,
                "content_type": response.headers.get("Content-Type", ""),
                "final_url": response.geturl(),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "body": b"", "content_type": "",
                "final_url": url, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "body": b"", "content_type": "",
                "final_url": url, "elapsed_ms": round((time.monotonic() - started) * 1000)}


def _json_get(base: str, path: str, timeout: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    response = _read_url(urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/")), timeout)
    if not response["ok"]:
        return None, response
    try:
        payload = json.loads(response["body"].decode("utf-8"))
    except Exception:
        response["error"] = "InvalidJSON"
        return None, response
    if not isinstance(payload, dict):
        response["error"] = "InvalidShape"
        return None, response
    return payload, response


def _identity(item: dict[str, Any], provider: str) -> str:
    raw = item.get("id") or item.get("vid") or item.get("douban_id")
    if item.get("douban_id") is not None:
        return "douban:" + str(item["douban_id"]).removeprefix("douban:")
    raw = str(raw or "")
    if raw.startswith(("douban:", "hhkan:")):
        return raw
    return ("hhkan:" if provider == "hhkan" else "douban:") + raw


def _search_pages(base: str, query: str, provider: str, limit: int,
                  max_pages: int, timeout: float) -> dict[str, Any]:
    all_items: list[dict[str, Any]] = []
    pages = 0
    reported_total = None
    has_more = False
    error = None
    for page in range(1, max_pages + 1):
        params = {("k" if provider == "hhkan" else "q"): query, "page": page}
        if provider != "hhkan":
            params["limit"] = limit
        encoded = urllib.parse.urlencode(params)
        path = "/hhkan/search?" + encoded if provider == "hhkan" else "/api/search?" + encoded
        payload, response = _json_get(base, path, timeout)
        if payload is None:
            error = response.get("error", "HTTP_" + str(response.get("status", "error")))
            break
        rows = payload.get("items")
        if not isinstance(rows, list):
            error = "InvalidItems"
            break
        rows = [row for row in rows if isinstance(row, dict)]
        all_items.extend(rows)
        pages += 1
        total = payload.get("total")
        if isinstance(total, int):
            reported_total = total
        if provider == "hhkan":
            has_more = len(rows) >= 30
        else:
            has_more = bool(payload.get("has_more"))
        if not has_more:
            break
    unique: dict[str, dict[str, Any]] = {}
    for item in all_items:
        key = _identity(item, provider)
        if key and key not in unique:
            unique[key] = item
    return {
        "provider": provider,
        "pages": pages,
        "reported_total": reported_total,
        "raw_count": len(all_items),
        "unique_count": len(unique),
        "duplicate_count": len(all_items) - len(unique),
        "has_more_at_limit": has_more and pages >= max_pages,
        "error": error,
        "items": [{"id": key, "title": str(row.get("title") or "")[:120], "raw": row}
                  for key, row in unique.items()],
    }


def _episode_indexes(episodes: list[Any], count: int) -> list[int]:
    if not episodes:
        return []
    if count <= 1 or len(episodes) == 1:
        return [0]
    if count == 2:
        return sorted({0, len(episodes) - 1})
    picks = {round(i * (len(episodes) - 1) / (count - 1)) for i in range(count)}
    return sorted(picks)


def _line_indexes(lines: list[Any], count: int) -> list[int]:
    if not lines or count <= 0:
        return []
    if count == 1 or len(lines) == 1:
        return [0]
    if count >= len(lines):
        return list(range(len(lines)))
    return sorted({round(i * (len(lines) - 1) / (count - 1)) for i in range(count)})


def _source_url(result: dict[str, Any]) -> str:
    for key in ("url", "play_url", "src"):
        value = result.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    sources = result.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, str) and source.startswith(("http://", "https://")):
                return source
            if isinstance(source, dict):
                value = _source_url(source)
                if value:
                    return value
    return ""


def _probe_media(url: str, timeout: float) -> dict[str, Any]:
    manifest = _read_url(url, timeout, referer=url)
    parsed = urllib.parse.urlsplit(manifest.get("final_url") or url)
    result = {
        "ok": False,
        "status": manifest.get("status"),
        "host": parsed.hostname or "",
        "content_type": manifest.get("content_type", "").split(";", 1)[0][:80],
        "elapsed_ms": manifest.get("elapsed_ms", 0),
        "bytes": len(manifest.get("body", b"")),
        "kind": "unknown",
    }
    body = manifest.get("body", b"")
    if not manifest["ok"] or not body:
        result["error"] = manifest.get("error", "EmptyOrHTTPError")
        return result
    text = body.decode("utf-8", "replace").lstrip("\ufeff\r\n \t")
    if text.startswith("#EXTM3U"):
        result["kind"] = "hls"
        target = next((line.strip() for line in text.splitlines()
                       if line.strip() and not line.lstrip().startswith("#")), "")
        if not target:
            result["error"] = "EmptyPlaylist"
            return result
        segment_url = urllib.parse.urljoin(manifest.get("final_url") or url, target)
        segment = _read_url(segment_url, timeout, referer=manifest.get("final_url") or url)
        segment_body = segment.get("body", b"")
        if segment["ok"] and segment_body:
            nested = segment_body.decode("utf-8", "replace").lstrip("\ufeff\r\n \t")
            if nested.startswith("#EXTM3U"):
                row = next((line.strip() for line in nested.splitlines()
                            if line.strip() and not line.lstrip().startswith("#")), "")
                if row:
                    nested_url = urllib.parse.urljoin(segment.get("final_url") or segment_url, row)
                    segment = _read_url(nested_url, timeout,
                                        referer=segment.get("final_url") or segment_url)
                    segment_body = segment.get("body", b"")
        result["segment_status"] = segment.get("status")
        result["segment_bytes"] = len(segment_body)
        result["ok"] = bool(segment["ok"] and segment_body)
        if not result["ok"]:
            result["error"] = segment.get("error", "SegmentUnavailable")
        return result
    content_type = result["content_type"].lower()
    path = urllib.parse.urlsplit(manifest.get("final_url") or url).path.lower()
    is_html = "text/html" in content_type or text[:64].lower().startswith(("<!doctype html", "<html"))
    direct_ext = (".mp4", ".m4v", ".mkv", ".flv", ".ts", ".m4s", ".mp3", ".aac", ".mpd")
    direct_type = content_type.startswith(("video/", "audio/"))
    result["kind"] = "direct" if direct_type or path.endswith(direct_ext) else "unknown"
    result["ok"] = not is_html and bool(body) and (direct_type or path.endswith(direct_ext))
    if not result["ok"]:
        result["error"] = "HTMLOrUnknownMedia"
    return result


def _source_error_category(error: Any) -> str:
    message = str(error or "").lower()
    if "adapter not probeable" in message:
        return "NotProbeable"
    if "timeout" in message or "timed out" in message:
        return "Timeout"
    if "challenge" in message or "cloudflare" in message:
        return "ChallengePage"
    if "unexpected payload" in message:
        return "UnexpectedPayload"
    if "ssl" in message or "urlopen" in message or "connection" in message:
        return "NetworkError"
    return "ProbeFailed" if message else ""


def _probe_source_round(base: str, timeout: float) -> dict[str, Any]:
    payload, response = _json_get(base, "/vod/sources/probe", timeout)
    if payload is None:
        return {"error": response.get("error", "HTTP_" + str(response.get("status", "error"))),
                "results": [], "checked": 0, "healthy": 0, "failed": 0}
    items = payload.get("results") if isinstance(payload.get("results"), list) else []
    results = [{
        "name": str(item.get("name") or item.get("id") or "")[:100],
        "ok": bool(item.get("ok")),
        "latency_ms": int(item.get("latencyMs") or 0),
        "status": int(item.get("status") or 0),
        "error_category": _source_error_category(item.get("error")),
    } for item in items if isinstance(item, dict)]
    return {"checked": len(results), "healthy": sum(1 for item in results if item["ok"]),
            "failed": sum(1 for item in results if not item["ok"]
                           and item["error_category"] != "NotProbeable"),
            "results": results}


def _detail_and_play(base: str, item: dict[str, Any], max_lines: int,
                     episodes_per_line: int, timeout: float, probe_streams: bool) -> dict[str, Any]:
    ref = item["id"]
    kind, media_id = ref.split(":", 1)
    if kind == "hhkan":
        path = "/hhkan/detail/" + urllib.parse.quote(media_id, safe="") + "?enrich=1"
    else:
        path = "/vod/detail/" + urllib.parse.quote(media_id, safe="")
    detail, response = _json_get(base, path, timeout)
    result: dict[str, Any] = {
        "id": ref,
        "title": item.get("title", ""),
        "detail_ok": bool(detail and detail.get("title")),
        "source_count": 0,
        "lines": [],
    }
    if not result["detail_ok"]:
        result["detail_error"] = response.get("error", "HTTP_" + str(response.get("status", "error")))
        return result
    lines = detail.get("sources") if isinstance(detail.get("sources"), list) else []
    result["source_count"] = len(lines)
    if not lines:
        result["source_state"] = str(detail.get("source_state") or "no_sources")[:40]
        return result
    for line_index in _line_indexes(lines, max_lines):
        line = lines[line_index]
        if not isinstance(line, dict):
            continue
        episodes = line.get("episodes") if isinstance(line.get("episodes"), list) else []
        line_result = {"name": str(line.get("name") or line.get("source") or "")[:100],
                       "episode_count": len(episodes), "plays": []}
        for episode_index in _episode_indexes(episodes, episodes_per_line):
            episode = episodes[episode_index]
            if kind == "hhkan":
                pid = episode.get("pid") if isinstance(episode, dict) else None
                vid = episode.get("vid") if isinstance(episode, dict) else None
                if pid is None or vid is None:
                    play_path = ""
                else:
                    play_path = "/hhkan/play/{}/{}/{}".format(
                        urllib.parse.quote(media_id, safe=""),
                        urllib.parse.quote(str(pid), safe=""),
                        urllib.parse.quote(str(vid), safe=""))
            else:
                line_id = line.get("id")
                play_path = ("/vod/play/{}/{}/{}".format(
                    urllib.parse.quote(media_id, safe=""),
                    urllib.parse.quote(str(line_id), safe=""), episode_index)
                    if line_id is not None else "")
            if not play_path:
                line_result["plays"].append({"episode_index": episode_index, "ok": False,
                                             "error": "MissingPlayReference"})
                continue
            played, play_response = _json_get(base, play_path, timeout)
            stream_url = _source_url(played or {}) if played else ""
            play_result: dict[str, Any] = {
                "episode_index": episode_index,
                "ok": bool(played and stream_url),
                "status": play_response.get("status"),
            }
            if not stream_url:
                play_result["error"] = play_response.get("error", "NoPlayableURL")
                backend_error = str((played or {}).get("error") or "")
                if backend_error:
                    play_result["api_error"] = (
                        "SourceUnavailable" if "://" not in backend_error
                        and any(token in backend_error for token in
                                ("播放线路", "播放源", "补线", "解析失败", "不可达"))
                        else "BackendError")
            elif probe_streams:
                play_result["media"] = _probe_media(stream_url, timeout)
                play_result["ok"] = bool(play_result["media"]["ok"])
            line_result["plays"].append(play_result)
        result["lines"].append(line_result)
    return result


def run_matrix(base: str, queries: list[str], *, limit: int = 100, max_pages: int = 5,
               max_items: int = 12, max_lines: int = 4, episodes_per_line: int = 2,
               timeout: float = 12, probe_streams: bool = False,
               expected_min: dict[str, int] | None = None,
               source_probe_rounds: int = 0, source_probe_timeout: float = 45) -> dict[str, Any]:
    report: dict[str, Any] = {"base_host": urllib.parse.urlsplit(base).hostname or "",
                              "queries": [], "summary": {}}
    inspected: set[str] = set()
    for query in queries:
        api = _search_pages(base, query, "api", limit, max_pages, timeout)
        hhkan = _search_pages(base, query, "hhkan", limit, max_pages, timeout)
        query_result = {
            "query": query,
            "api_search": {key: value for key, value in api.items() if key != "items"},
            "hhkan_search": {key: value for key, value in hhkan.items() if key != "items"},
            "works": [],
        }
        minimum = (expected_min or {}).get(query, 2 if query == "红楼梦" else 0)
        if api["unique_count"] < minimum:
            query_result["api_search"]["expected_min_failure"] = minimum
        candidates = []
        for index in range(max(len(api["items"]), len(hhkan["items"]))):
            if index < len(api["items"]):
                candidates.append(api["items"][index])
            if index < len(hhkan["items"]):
                candidates.append(hhkan["items"][index])
        local_seen: set[str] = set()
        chosen = []
        for item in candidates:
            if item["id"] in local_seen:
                continue
            local_seen.add(item["id"])
            chosen.append(item)
        for item in chosen[:max_items]:
            if item["id"] in inspected:
                continue
            inspected.add(item["id"])
            query_result["works"].append(
                _detail_and_play(base, item, max_lines, episodes_per_line, timeout, probe_streams))
        report["queries"].append(query_result)

    if source_probe_rounds:
        report["source_probe_rounds"] = [
            _probe_source_round(base, source_probe_timeout)
            for _ in range(source_probe_rounds)
        ]

    counts = {"queries": len(report["queries"]), "works_checked": 0,
              "details_ok": 0, "source_lines": 0, "play_requests": 0,
              "play_urls_resolved": 0, "media_probes_ok": 0,
              "configured_sources_checked": 0, "configured_sources_healthy": 0,
              "configured_source_failures": 0, "failures": 0}
    for query in report["queries"]:
        counts["failures"] += int(bool(query["api_search"].get("error")))
        counts["failures"] += int(bool(query["hhkan_search"].get("error")))
        counts["failures"] += int("expected_min_failure" in query["api_search"])
        counts["failures"] += int(bool(query["api_search"].get("has_more_at_limit")))
        counts["failures"] += int(bool(query["hhkan_search"].get("has_more_at_limit")))
        for work in query["works"]:
            counts["works_checked"] += 1
            counts["details_ok"] += int(work["detail_ok"])
            counts["failures"] += int(not work["detail_ok"])
            counts["failures"] += int(work["detail_ok"] and work["source_count"] == 0)
            for line in work["lines"]:
                counts["source_lines"] += 1
                counts["failures"] += int(not line["plays"])
                for play in line["plays"]:
                    counts["play_requests"] += 1
                    counts["play_urls_resolved"] += int(bool(play["ok"]))
                    if "media" in play:
                        counts["media_probes_ok"] += int(bool(play["media"]["ok"]))
                    counts["failures"] += int(not play["ok"])
    for source_round in report.get("source_probe_rounds", []):
        counts["configured_sources_checked"] += source_round.get("checked", 0)
        counts["configured_sources_healthy"] += source_round.get("healthy", 0)
        counts["configured_source_failures"] += source_round.get("failed", 0)
        counts["failures"] += int(bool(source_round.get("error")))
        counts["failures"] += source_round.get("failed", 0)
    report["summary"] = counts
    report["ok"] = counts["failures"] == 0
    return report


def _parse_expectations(values: list[str]) -> dict[str, int]:
    result = {}
    for value in values:
        query, sep, amount = value.partition("=")
        if not sep or not query.strip():
            raise argparse.ArgumentTypeError("期望命中格式为 查询=数量")
        result[query.strip()] = max(0, int(amount))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8090", help="backend base URL")
    parser.add_argument("--query", action="append", dest="queries", help="repeatable search query")
    parser.add_argument("--limit", type=int, default=100, help="search page size (1..100)")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--max-items", type=int, default=12, help="detail checks per query")
    parser.add_argument("--max-lines", type=int, default=4, help="playback sources per work")
    parser.add_argument("--episodes-per-line", type=int, default=2,
                        help="sample first/last episodes (or evenly spaced) per source")
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--probe-streams", action="store_true",
                        help="fetch media and first HLS segment after play resolves")
    parser.add_argument("--source-probe-rounds", type=int, default=0,
                        help="repeat the backend device-network source health check")
    parser.add_argument("--source-probe-timeout", type=float, default=45,
                        help="timeout in seconds for each source health round")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a non-loopback backend base URL")
    parser.add_argument("--expect-min", action="append", default=[], metavar="QUERY=N",
                        help="minimum unique /api/search hits; 红楼梦 defaults to 2")
    parser.add_argument("--output", help="write sanitized JSON report to this path")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        parser.error("--base must be an http(s) URL")
    loopback = parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1")
    if not loopback and not args.allow_remote:
        parser.error("默认只测本机服务；远程环境请显式加 --allow-remote")
    if not (1 <= args.limit <= 100 and args.max_pages >= 1 and args.max_items >= 0
            and args.max_lines >= 0 and args.episodes_per_line >= 1 and args.timeout > 0
            and args.source_probe_rounds >= 0 and args.source_probe_timeout > 0):
        parser.error("limit、分页/采样上限或 timeout 参数无效")
    report = run_matrix(base, args.queries or DEFAULT_QUERIES, limit=args.limit,
                        max_pages=args.max_pages, max_items=args.max_items,
                        max_lines=args.max_lines, episodes_per_line=args.episodes_per_line,
                        timeout=args.timeout, probe_streams=args.probe_streams,
                        expected_min=_parse_expectations(args.expect_min),
                        source_probe_rounds=args.source_probe_rounds,
                        source_probe_timeout=args.source_probe_timeout)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    print(serialized)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
