"""Runs inside the device to prove media really pulls, not just resolves.

Pushed to the emulator and executed there, so every fetch originates from the
device network rather than the dev host. Prints one JSON line.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8090"
UA = {"User-Agent": "MyTV Android"}


def get(url, timeout=15, headers=None):
    request = urllib.request.Request(url, headers=headers or UA)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ok": True, "code": getattr(response, "status", 200),
                    "body": response.read(300000).decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "code": exc.code, "err": "HTTP " + str(exc.code)}
    except Exception as exc:
        return {"ok": False, "err": type(exc).__name__ + ":" + str(exc)[:80]}


def main():
    detail = get(BASE + "/vod/detail/1292052")
    report = {"detail_ok": detail["ok"], "tried": 0, "playable": 0, "lines": []}
    if not detail["ok"]:
        report["detail_error"] = detail.get("err")
        print(json.dumps(report, ensure_ascii=False))
        return
    try:
        payload = json.loads(detail["body"])
    except Exception as exc:
        report["parse_error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False))
        return
    for line in (payload.get("sources") or [])[:6]:
        entry = {"name": line.get("name", "")}
        report["tried"] += 1
        quoted = urllib.parse.quote(line.get("id", ""), safe="")
        played = get(BASE + "/vod/play/1292052/" + quoted + "/0")
        if not played["ok"]:
            entry["play_error"] = played.get("err") or played.get("code")
            report["lines"].append(entry)
            continue
        url = json.loads(played["body"]).get("url", "")
        if not url:
            entry["play_error"] = "empty url"
            report["lines"].append(entry)
            continue
        parts = urllib.parse.urlsplit(url)
        entry["scheme"] = parts.scheme
        entry["host"] = parts.netloc
        media = get(url)
        entry["manifest_code"] = media.get("code", media.get("err"))
        body = media.get("body", "")
        if not media["ok"]:
            report["lines"].append(entry)
            continue
        entry["is_m3u8"] = body.lstrip().startswith("#EXTM3U")
        entry["manifest_ok"] = True
        report["playable"] += 1
        if entry["is_m3u8"]:
            target = ""
            for row in body.splitlines():
                row = row.strip()
                if row and not row.startswith("#"):
                    target = row
                    break
            if target and not target.startswith("http"):
                target = urllib.parse.urljoin(url, target)
            if target:
                headers = {"User-Agent": "MyTV Android", "Referer": url}
                seg = get(target, headers=headers)
                entry["segment"] = seg.get("code", seg.get("err"))
                entry["segment_ok"] = bool(seg["ok"])
        report["lines"].append(entry)
    print(json.dumps(report, ensure_ascii=False))


main()
