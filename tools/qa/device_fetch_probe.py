"""Fetch a media URL from inside the device's embedded backend.

The emulator has no standalone Python and no curl, so the only way to learn
whether the *device* can pull media is to ask the app's own backend, which
already runs on the device network. This is pushed as a module and invoked
through the backend's own process by importing it there.

Prints one JSON line: {"host":..., "manifest_code":..., "is_m3u8":...,
"segment":..., "error":...}
"""
import json
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "ZuoLive Android"}


def get(url, timeout=15, headers=None):
    request = urllib.request.Request(url, headers=headers or UA)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ok": True, "code": getattr(response, "status", 200),
                    "body": response.read(400000).decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "code": exc.code, "err": "HTTP " + str(exc.code)}
    except Exception as exc:
        return {"ok": False, "err": type(exc).__name__ + ":" + str(exc)[:100]}


def probe(url):
    parts = urllib.parse.urlsplit(url)
    out = {"host": parts.netloc, "scheme": parts.scheme}
    media = get(url)
    out["manifest_code"] = media.get("code", media.get("err"))
    if not media["ok"]:
        return out
    body = media.get("body", "")
    out["is_m3u8"] = body.lstrip().startswith("#EXTM3U")
    out["bytes"] = len(body)
    if not out["is_m3u8"]:
        return out
    target = ""
    for row in body.splitlines():
        row = row.strip()
        if row and not row.startswith("#"):
            target = row
            break
    if not target:
        out["error"] = "no segment in manifest"
        return out
    if not target.startswith("http"):
        target = urllib.parse.urljoin(url, target)
    headers = {"User-Agent": "ZuoLive Android", "Referer": url}
    seg = get(target, headers=headers)
    out["segment"] = seg.get("code", seg.get("err"))
    out["segment_ok"] = bool(seg["ok"])
    return out


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(probe(target), ensure_ascii=False))


import sys

main()
