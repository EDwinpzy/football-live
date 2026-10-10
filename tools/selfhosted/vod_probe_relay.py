"""Device-side playback-source probe relay.

The self-hosted VPS (磐石云) has no international egress: every Cloudflare /
GitHub / Fastly address times out on TCP connect, so the admin panel's own
``vod_source_test`` always reports "dead" for sources that work perfectly well
from a real phone.  This relay runs on a machine that *does* have egress and
answers the same question over the user's actual connection.

The VPS reaches it through an SSH reverse tunnel, so the probing machine needs
no public IP and no inbound port.

Security: this is deliberately NOT an open proxy.  It only accepts URLs whose
host matches a configured allowlist, and it refuses private/loopback targets
(SSRF guard).  The listener binds to loopback and is only reachable through the
tunnel.

Protocol (JSON over HTTP):
    GET  /health        -> {"ok": true, ...}
    POST /probe         -> {"urls": ["https://...", ...]}
                       -> {"results": [{"url", "ok", "status",
                                        "latency_ms", "error"}, ...]}
    POST /probe-source  -> {"api_url": "...", "title": "庆余年"}
                       -> search + first-line probe, mirroring
                          vod_sources.probe_source()

Run:  python tools/selfhosted/vod_probe_relay.py --port 8765 --allowlist hosts.json
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}
MAX_URLS_PER_REQUEST = 30
MAX_BODY_BYTES = 16 * 1024
PROBE_TIMEOUT_SECONDS = 10.0
SEARCH_TIMEOUT_SECONDS = 12.0
LINE_SAMPLE_SIZE = 8
"""How many of a source's lines to probe before judging it unplayable.

Source stations advertise far more lines than actually work.  Probing the first
N is not representative: a macCMS ``vod_play_url`` lists episodes in upload
order, so the head of the list is often a single expired day (measured: 量子's
first 8 lines were all 2025-09-23 and all 404 while later lines played).  Spread
the sample across the list instead.
"""

"""Hosts this relay may probe.  Filled from the deployed source registry so a
stray caller cannot turn the machine into a general-purpose open proxy."""
ALLOWED_HOST_SUFFIXES: tuple[str, ...] = ()

_PRIVATE_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost"}
_FAKE_IP_BAND = ipaddress.ip_network("198.18.0.0/15")
_CLOUDFLARE_CHALLENGE = re.compile(
    rb"just a moment|cf-browser-verification|challenge-platform|cf_chl_opt", re.I)


class ProbeRefused(ValueError):
    """Raised when a requested URL is outside the allowlist or targets LAN."""


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr in _FAKE_IP_BAND:
        # Clash / sing-box map every domain into 198.18.0.0/15 and the local
        # proxy performs the real connection.  Rejecting it would kill every
        # probe on a machine that runs one — and this relay is meant to run on
        # the user's own PC, which almost certainly does.
        return True
    if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
        return False
    return True


def validate_target(url: str) -> str:
    """Allowlist + SSRF check.  Returns the normalized URL or raises."""
    raw = str(url or "").strip()
    if not raw:
        raise ProbeRefused("empty url")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        raise ProbeRefused("only http/https allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ProbeRefused("missing host")
    if host in _PRIVATE_HOSTS:
        raise ProbeRefused("loopback host refused")
    if not (ALLOWED_HOST_SUFFIXES
            and any(host == suffix or host.endswith("." + suffix)
                    for suffix in ALLOWED_HOST_SUFFIXES)):
        raise ProbeRefused(f"host not allowlisted: {host}")
    for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                    proto=socket.IPPROTO_TCP):
        if not _ip_is_public(info[4][0]):
            raise ProbeRefused(f"resolves to non-public address: {info[4][0]}")
    return raw


def _fetch(url: str, *, timeout: float, max_bytes: int = 4096) -> tuple[int, bytes, float, str]:
    request = urllib.request.Request(url, headers={**BROWSER_HEADERS, "Accept": "*/*"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes)
        content_type = response.headers.get_content_type().lower()
        return response.status, body, (time.monotonic() - started) * 1000.0, content_type


def _summarize_error(exc: BaseException) -> str:
    """Strip query strings — upstream errors can echo signed CDN tokens."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return f"{type(reason).__name__ if reason else 'URLError'}: {reason}"[:120]
    return f"{type(exc).__name__}: {exc}"[:120]


def _search(base: str, title: str) -> dict:
    """Run one macCMS detail query and return the decoded payload."""
    query = base + ("&" if "?" in base else "?") + "ac=detail"
    if title:
        query += "&wd=" + urllib.parse.quote(title)
    request = urllib.request.Request(query, headers=BROWSER_HEADERS)
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    payload["_search_latency_ms"] = round((time.monotonic() - started) * 1000.0)
    return payload


def _playable_urls(payload: dict) -> list[str]:
    """Flatten a macCMS ``vod_play_url`` blob into playable line URLs."""
    urls: list[str] = []
    for item in payload.get("list") or []:
        if not isinstance(item, dict):
            continue
        for group in str(item.get("vod_play_url") or "").split("$$$"):
            for episode in group.split("#"):
                value = episode.rsplit("$", 1)[-1].strip()
                if value.startswith(("http://", "https://")):
                    urls.append(value)
    return urls


def _spread_sample(items: list[str], count: int) -> list[str]:
    """Pick ``count`` entries spread evenly across ``items``.

    Keeps the first and last entry and steps through the middle, so one stale
    batch at the head of the list cannot make a working source look dead.
    """
    total = len(items)
    if total <= count:
        return list(items)
    if count <= 1:
        return [items[-1]]
    step = (total - 1) / (count - 1)
    return [items[round(index * step)] for index in range(count)]


def _looks_like_html(body: bytes) -> bool:
    """Recognize common HTML document starts without rejecting HLS/media data."""
    prefix = body[:2048].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    return bool(re.match(
        rb"<(?:!doctype\s+html\b|html\b|head\b|body\b|title\b|script\b|meta\b|iframe\b)",
        prefix))


def _looks_like_media(body: bytes) -> bool:
    """Recognize common HLS and segment signatures if a server mislabels them."""
    if body.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").startswith(b"#EXTM3U"):
        return True
    if body.startswith(b"FLV") or body.startswith(b"\x1a\x45\xdf\xa3"):
        return True
    if len(body) >= 376 and body[0] == 0x47 and body[188] == 0x47:
        return True
    return len(body) >= 8 and body[4:8] in {b"ftyp", b"moof", b"styp"}


def _is_explicit_non_media_type(content_type: str) -> bool:
    """Reject declared document/text types unless their body has a media signature."""
    value = str(content_type or "").lower().split(";", 1)[0].strip()
    if value.startswith(("text/", "image/", "font/")):
        return True
    if value in {
        "application/json", "text/json", "application/xml", "text/xml",
        "application/xhtml+xml", "application/javascript", "text/javascript",
        "application/x-javascript", "application/pdf", "application/zip",
        "application/x-www-form-urlencoded",
    }:
        return True
    if value.endswith(("+json", "+xml")) and value != "application/dash+xml":
        return True
    return False


def _probe_validated(target: str) -> dict:
    try:
        status, body, elapsed, content_type = _fetch(target, timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return {"url": target, "ok": False, "status": None, "latency_ms": None,
                "error": _summarize_error(exc)}
    if _CLOUDFLARE_CHALLENGE.search(body):
        return {"url": target, "ok": False, "status": status,
                "latency_ms": round(elapsed), "error": "challenge page"}
    if not body.strip(b" \t\r\n"):
        return {"url": target, "ok": False, "status": status,
                "latency_ms": round(elapsed), "error": "empty response body"}
    media_signature = _looks_like_media(body)
    is_html_type = content_type in {"text/html", "application/xhtml+xml"}
    if (is_html_type or _looks_like_html(body)) and not media_signature:
        return {"url": target, "ok": False, "status": status,
                "latency_ms": round(elapsed), "error": "html page"}
    if _is_explicit_non_media_type(content_type) and not media_signature:
        return {"url": target, "ok": False, "status": status,
                "latency_ms": round(elapsed), "error": "non-media response"}
    return {"url": target, "ok": 200 <= status < 400, "status": status,
            "latency_ms": round(elapsed), "error": ""}


def probe_one(url: str) -> dict:
    try:
        target = validate_target(url)
    except ProbeRefused as exc:
        return {"url": url, "ok": False, "status": None, "latency_ms": None, "error": str(exc)}
    return _probe_validated(target)


def probe_source(api_url: str, title: str = "") -> dict:
    """Search one macCMS source, then probe its first playable line.

    Mirrors what the app does, so the admin panel shows the same verdict a user
    would get: interface reachable AND at least one line that actually plays.
    """
    result = {"api_url": api_url, "title": title, "ok": False,
              "search_latency_ms": None, "lines": 0, "direct": 0,
              "probed": 0, "preview_url": "", "error": ""}
    try:
        base = validate_target(api_url)
    except ProbeRefused as exc:
        result["inconclusive"] = True
        result["error"] = "source API URL rejected before HTTP probe"
        return result
    try:
        payload = _search(base, title)
    except Exception as exc:  # noqa: BLE001
        result["error"] = _summarize_error(exc)
        return result
    result["search_latency_ms"] = payload.get("_search_latency_ms")

    direct_urls = _playable_urls(payload)
    result["lines"] = len(direct_urls)
    if not direct_urls:
        result["error"] = "no playable line in search result"
        return result
    sample = _spread_sample(direct_urls, LINE_SAMPLE_SIZE)
    refused_count = 0
    probed = 0
    for candidate in sample:
        try:
            target = validate_target(candidate)
        except ProbeRefused:
            # The source returned a candidate the relay is not allowed to
            # fetch. This is a measurement gap, not evidence the source is dead.
            refused_count += 1
            continue
        probed += 1
        checked = _probe_validated(target)
        if checked["ok"]:
            result["ok"] = True
            result["direct"] = result["lines"]
            result["preview_url"] = candidate
            result["probed"] = probed
            return result
    result["probed"] = probed
    if probed == 0 and refused_count:
        result["inconclusive"] = True
        result["error"] = "all sampled stream URLs rejected before HTTP probe"
        return result
    result["error"] = f"interface ok, {probed} lines probed, none played"
    return result


def collect_cdn_hosts(api_url: str, title: str, limit: int = 60) -> list[str]:
    """List the CDN hostnames one source actually serves lines from.

    Used to build the relay allowlist: source API hosts are known from the
    registry, but line hosts are only discoverable by asking a live source.
    """
    try:
        base = validate_target(api_url)
    except ProbeRefused:
        return []
    try:
        payload = _search(base, title)
    except Exception:  # noqa: BLE001
        return []
    hosts: list[str] = []
    for value in _playable_urls(payload):
        host = (urllib.parse.urlsplit(value).hostname or "").lower()
        if host and host not in hosts:
            hosts.append(host)
        if len(hosts) >= limit:
            break
    return hosts


_EGRESS_CACHE: dict[str, object] = {}


def _egress_ip() -> str:
    """Best-effort public IP, cached — it never gates the probe itself."""
    cached = _EGRESS_CACHE.get("ip")
    if cached and time.monotonic() - float(_EGRESS_CACHE.get("at", 0)) < 3600:
        return str(cached)
    for endpoint in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(endpoint, timeout=5) as response:
                ip = response.read(64).decode("ascii", "replace").strip()
            if ip:
                _EGRESS_CACHE.update(ip=ip, at=time.monotonic())
                return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "OtvProbeRelay/1.0"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] == "/health":
            self._reply(200, {"ok": True, "allowed_hosts": len(ALLOWED_HOST_SUFFIXES),
                              "egress_ip": _egress_ip()})
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"error": "body too large"})
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "invalid json"})
            return
        if not isinstance(data, dict):
            self._reply(400, {"error": "object expected"})
            return

        if path == "/probe":
            urls = data.get("urls") or []
            if not isinstance(urls, list):
                self._reply(400, {"error": "urls must be a list"})
                return
            self._reply(200, {"results": [probe_one(u) for u in urls[:MAX_URLS_PER_REQUEST]]})
            return
        if path == "/probe-source":
            self._reply(200, probe_source(str(data.get("api_url") or ""),
                                          str(data.get("title") or "")))
            return
        if path == "/collect-cdn-hosts":
            hosts = collect_cdn_hosts(str(data.get("api_url") or ""),
                                      str(data.get("title") or ""))
            self._reply(200, {"hosts": hosts})
            return
        self._reply(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        return


def load_allowlist(path: str | None) -> tuple[str, ...]:
    """Read allowed host suffixes from a JSON file."""
    if not path:
        return ()
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return tuple(str(host).lower().strip() for host in payload if str(host).strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Device-side vod probe relay")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--allowlist", required=True,
                        help="JSON file listing allowed host suffixes")
    parser.add_argument("--collect-cdn-hosts", metavar="API_URL",
                        help="one-shot: print the CDN hosts this source uses, then exit")
    parser.add_argument("--collect-title", default="庆余年",
                        help="title used with --collect-cdn-hosts")
    args = parser.parse_args()

    global ALLOWED_HOST_SUFFIXES
    ALLOWED_HOST_SUFFIXES = load_allowlist(args.allowlist)
    if not ALLOWED_HOST_SUFFIXES:
        raise SystemExit("refusing to start with an empty allowlist")
    if args.collect_cdn_hosts:
        for host in collect_cdn_hosts(args.collect_cdn_hosts, args.collect_title):
            print(host)
        return
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"vod probe relay on http://{args.host}:{args.port} "
          f"allowlist={len(ALLOWED_HOST_SUFFIXES)} hosts", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
