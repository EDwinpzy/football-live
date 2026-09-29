#!/usr/bin/env python3
"""My TV self-hosted admin dashboard for licenses, APK updates, IPTV, and announcements.

Run locally with `python tools/admin_server.py`, or in the self-hosted Compose service.
Container deployment must provide admin/API credentials and a session secret through
runtime environment variables or mounted secret files.

登录门禁：
  - GET / 页面内置登录浮层；POST /api/login 校验账号密码 → 下发 HMAC 签名的
    HttpOnly 会话 Cookie（12h 有效；密钥固定，多实例/重启后仍有效）
  - 除 /api/login、静态资源外全部接口须带有效 Cookie；登录失败限速（5 次/60s 锁定）

功能（v1.17 → v1.20）:
  总览    各套餐库存 + 近 24h 激活动作 + 通道状态
  卡密    搜索/筛选、出售/封禁/解绑、复制
  日志    activate_log 审计记录按码过滤
  补货    license_gen.py 私钥签发 → 自建 PostgreSQL 入库 → cards.csv
  热更新  发布/上下线/删除热更包、设备上报日志（v1.18）
  APK 更新  TV/移动版 APK 分片上传、SHA-256 校验、稳定 update.json 发布
  公告    运营公告发布/下线（v1.20）
  电视源  频道 CRUD、M3U 导入（文件/文本/URL）、上游 API 同步、版本发布、
          回滚、排序/批量启停、操作日志导出、自动/托管模式切换（v1.20 后台#8）
          发布产物写 pgstore 公开桶 iptv/iptv-v{n}.m3u + iptv-latest.m3u，
          客户端 SOURCES 首位 ≤30 分钟自动拉取生效（客户端自动更新）；
          发布即切「托管模式」（每日 iptv-rebuild 自动推流暂停，防覆盖）。

安全边界：
  - 本地形态仅绑定 127.0.0.1；自建容器只监听 Compose 内网，由 Nginx/HTTPS 入口暴露；
  - 自建管理 API 使用短期访问票据，数据库 RPC 按 auth.uid() 白名单校验；
  - M3U 导入 URL / 上游同步出站走 _safe_fetch（仅 https + 公网 IP + ≤3 跳重定向）；
  - 卡密私钥只从运行时文件或环境变量读取，不写入仓库或镜像。
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend-src"))
from selfhosted import runtime_client  # noqa: E402
from selfhosted.admin_config import load_admin_credentials, load_session_secret  # noqa: E402
import hhkan  # noqa: E402
import vod_sources  # noqa: E402
from apk_update import (  # noqa: E402
    CHANNELS as APK_CHANNELS,
    build_manifest,
    normalize_certificates,
    validate_apk_bytes,
    validate_manifest_for_channel,
    safe_apk_filename,
)

PORT = int(os.environ.get("PORT") or 8791)
CONTAINER_MODE = bool(os.environ.get("PORT")) or os.environ.get("OTV_CONTAINER_MODE") == "1"
SELFHOSTED = os.environ.get("OTV_SELFHOSTED_MODE") == "1"
HTML_PATH = ROOT / "admin.html"
# 品牌字标：优先读取同目录资源，本地开发时回退到 Android 工程文件。
_LOGO_LOCAL = ROOT / "my_tv_logo.png"
_LOGO_REPO = ROOT.parent / "android" / "app" / "src" / "main" / "res" / "drawable" / "my_tv_logo.png"
LOGO_PATH = _LOGO_LOCAL if _LOGO_LOCAL.exists() else _LOGO_REPO

# ---------------- 登录门禁（v1.20） ----------------
# Nginx keeps the direct `/` entry and also strips `/admin/` before proxying it here.

_ADMIN_CREDENTIALS = load_admin_credentials(os.environ)
ADMIN_USER = (_ADMIN_CREDENTIALS[0] if _ADMIN_CREDENTIALS else None) or os.environ.get("OTV_ADMIN_USER") or "admin"
# 密码优先级：env > keys/admin_pass.txt（首启生成一次）> 本地随机（每次启动变化并打印）
_admin_pass_file = ROOT / "keys" / "admin_pass.txt"


def _resolve_admin_pass() -> str:
    env = os.environ.get("OTV_ADMIN_PASS")
    if env:
        return env
    try:
        if _admin_pass_file.exists():
            v = _admin_pass_file.read_text(encoding="utf-8").strip()
            if v:
                return v
        _admin_pass_file.parent.mkdir(parents=True, exist_ok=True)
        v = secrets.token_urlsafe(6)
        _admin_pass_file.write_text(v, encoding="utf-8")
        return v
    except OSError:
        return secrets.token_urlsafe(6)


ADMIN_PASS = _ADMIN_CREDENTIALS[1] if _ADMIN_CREDENTIALS else _resolve_admin_pass()
# 补货产物目录：容器使用临时目录，本地开发默认写入 tools/out。
OUT_ROOT = Path(os.environ.get("OTV_OUT_DIR") or (ROOT / "out"))
# 会话签名密钥：优先 env；缺省时由登录凭据派生**固定**值。
# 不能退回随机值——云函数多实例/冷启动各自随机会让 Cookie 时验时不验
# （2026-09-16 线上实测：登录成功后再连发 8 次带 Cookie 请求，7 次 401）。
_CONFIGURED_SESSION_SECRET = load_session_secret(os.environ)
SESSION_SECRET = _CONFIGURED_SESSION_SECRET or hashlib.sha256(
    f"otv-admin-session:{ADMIN_USER}:{ADMIN_PASS}".encode("utf-8")).hexdigest()
SESSION_TTL = 12 * 3600
COOKIE = "otv_admin_sess"
# 免登录开关（用户可要求；默认关）：OTV_ADMIN_NOAUTH=1 时网关匿名直达后台，
# 谁拿到 /admin/ 地址都能改卡密、推热更、发公告。改 env 即时生效，不用重新部署代码。
NOAUTH = os.environ.get("OTV_ADMIN_NOAUTH") == "1"
_IPTV_PUBLISH_LEASE = ContextVar("mytv_iptv_publish_lease", default=None)


def _assert_iptv_publish_lease() -> None:
    lease = _IPTV_PUBLISH_LEASE.get()
    if lease is not None:
        lease.assert_valid()


def _sign(expires: int) -> str:
    return hmac.new(SESSION_SECRET.encode(), str(expires).encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    exp = int(time.time()) + SESSION_TTL
    return f"{exp}.{_sign(exp)}"


def check_session(tok: str) -> bool:
    try:
        exp, sig = tok.split(".", 1)
        exp = int(exp)
    except (ValueError, AttributeError):
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(exp))


# 登录限速：IP → (失败次数, 锁定到)
_login_guard: dict = {}
_login_lock = threading.Lock()


def login_too_busy(peer: str) -> bool:
    with _login_lock:
        st = _login_guard.get(peer)
        return bool(st and st[1] > time.time())


def note_login(peer: str, ok: bool) -> None:
    with _login_lock:
        if ok:
            _login_guard.pop(peer, None)
            return
        fails, _, = _login_guard.get(peer, (0, 0.0))
        fails += 1
        lock_until = time.time() + 60 if fails >= 5 else 0.0
        _login_guard[peer] = (fails, lock_until)


_tok = {"v": None, "exp": 0.0}
_tok_lock = threading.Lock()


def api_token() -> str:
    with _tok_lock:
        if _tok["v"] and time.time() < _tok["exp"] - 120:
            return _tok["v"]
        t = runtime_client.signin()
        _tok["v"] = t
        _tok["exp"] = time.time() + 6600  # token 有效 2h，提前 10 分钟续
        return _tok["v"]


def rpc(name: str, payload: dict) -> dict:
    _assert_iptv_publish_lease()
    url = runtime_client.REST_BASE + "/rpc/" + name
    runtime_client.check_url(url)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_token()})
    try:
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # PostgREST 错误形态是 {"code","message",...}（无 "error" 键）——归一化，
        # 否则调用方 out.get("error") 判空会把 RPC 失败误判成成功
        try:
            out = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"error": f"自建服务返回 HTTP {e.code}"}
        if isinstance(out, dict) and "error" not in out:
            return {"error": out.get("message") or out.get("hint") or f"HTTP {e.code} {out.get('code', '')}"}
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": f"请求失败: {e}"}


# ---------------- v1.18 热更新管理（公测版） ----------------

HU_BUCKET = "hotupdate"
HU_ENDPOINT = runtime_client.HOTUPDATE_ENDPOINT
# 与 APK HotUpdateManager / hotupdate_admin_upsert RPC 的校验口径一致
HU_REQUIRED_ENTRIES = (
    "proxy.py", "hhkan.py", "scraper.py", "team_backdrop.py", "decrypt_stream.js",
    "douban_catalog.py", "douban_snapshot.json", "media_index.py", "pinyin_map.json", "vod_api.py",
    "vod_sources.py", "vod_sources.default.json", "metadata_client.py", "vod_resolver.py",
)
HU_MAX_BYTES = 220 * 1024 * 1024
# 显式指定热更版本号的上限（防手滑输入天文数字把设备端比较逻辑顶掉）
HU_MAX_VERSION = 10_000_000

# ---------------- APK 应用内更新（与 APK_UPDATE_URL 对接） ----------------
# APK 与 update.json 保存在自建对象存储的独立路径中；清单由本地 Web API 提供。
APK_MAX_BYTES = 300 * 1024 * 1024
# 单对象控制在 100 MiB 内，超出时使用分片发布，避免单次上传/下载占用过多内存。
APK_OBJECT_MAX_BYTES = 100 * 1024 * 1024
APK_OBJECT_HINT = ("单对象上限 100 MiB：请改用 python tools/publish_apk_update.py "
                   "分片发布（客户端按序下载各分片后仍校验整包 SHA-256 与签名）")
APK_UPLOAD_CHUNK_MAX = 4 * 1024 * 1024
APK_UPLOAD_DIRECT_MAX = 5 * 1024 * 1024
APK_UPLOAD_UID_RE = re.compile(r"^[0-9a-f]{8,32}$")
APK_MANIFEST_PREFIX = "apk-update"
APK_CERT_ENV = "OTV_APK_CERT_SHA256"


def obj_url(key: str) -> str:
    return runtime_client.ENDPOINTS.public_object_url(HU_BUCKET, key)


def _apk_obj_url(key: str) -> str:
    """APK 下载链接指向公网 HTTPS 对象地址；上传使用私有 storage 服务地址。"""
    return runtime_client.ENDPOINTS.public_object_url(HU_BUCKET, key)


def pg_put(key: str, raw: bytes, ctype: str) -> None:
    _assert_iptv_publish_lease()
    url = runtime_client.ENDPOINTS.storage_object_url(key) + "?x-upsert=true"
    runtime_client.check_url(url)
    req = urllib.request.Request(url, data=raw, method=runtime_client.ENDPOINTS.storage_upload_method, headers={
        "Content-Type": ctype,
        "Authorization": "Bearer " + api_token(),
        "x-upsert": "true",
    })
    with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=600) as r:
        r.read()


def pg_get_public(key: str, max_bytes: int = 32 * 1024 * 1024):
    """读公开桶对象（匿名 GET，与客户端拉取同一路径）"""
    url = runtime_client.ENDPOINTS.storage_object_url(key)
    runtime_client.check_url(url)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=120) as r:
        return r.read(max_bytes)


def _apk_manifest_key(channel: str) -> str:
    if channel not in APK_CHANNELS:
        raise ValueError("渠道必须是 tv 或 mobile")
    return f"{APK_MANIFEST_PREFIX}/{channel}.json"


def _apk_read_manifest(channel: str) -> dict | None:
    """读取当前公开清单；首次发布时对象不存在视为 None。"""
    try:
        raw = pg_get_public(_apk_manifest_key(channel), max_bytes=512 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("已有 update.json 不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("已有 update.json 根节点必须是对象")
    validate_manifest_for_channel(data, channel)
    return data


def _apk_meta(qs: dict) -> dict:
    """解析 APK 发布表单的 query string。"""
    def q(name: str, default: str = "") -> str:
        value = qs.get(name)
        return str(value[0] if value else default).strip()

    channel = q("channel").lower()
    if channel not in APK_CHANNELS:
        raise ValueError("渠道必须是 tv 或 mobile")
    try:
        version_code = int(q("versionCode", "0"))
        min_supported = int(q("minSupportedVersionCode", "0"))
    except ValueError as exc:
        raise ValueError("versionCode 和最低支持版本必须是数字") from exc
    status = q("status", "published")
    if status not in ("published", "offline"):
        raise ValueError("发布状态只能是 published 或 offline")
    certs = q("certificateSha256")
    if not certs:
        certs = os.environ.get(APK_CERT_ENV, "")
    if not certs:
        raise ValueError(f"请填写签名证书 SHA-256（或配置 {APK_CERT_ENV}）")
    # 先校验证书摘要，避免上传完才发现清单不可解析。
    normalize_certificates(certs)
    filename = safe_apk_filename(q("fileName", "update.apk"))
    return {
        "channel": channel,
        "version_code": version_code,
        "version_name": q("versionName"),
        "min_supported": min_supported,
        "force_update": q("forceUpdate").lower() in ("1", "true", "yes", "on"),
        "title": q("title", "发现新版本"),
        "changelog": q("changelog"),
        "certificate_sha256": certs,
        "status": status,
        "file_name": filename,
    }


def _apk_publish(raw: bytes, meta: dict) -> dict:
    """校验 APK、写入对象，并在 published 状态原子覆盖渠道清单。"""
    if not 1 <= len(raw) <= APK_MAX_BYTES:
        return {"error": f"APK 大小不合法（1B–{APK_MAX_BYTES // 1048576}MB）"}
    if len(raw) > APK_OBJECT_MAX_BYTES:
        return {"error": APK_OBJECT_HINT}
    try:
        validate_apk_bytes(raw)
        current = _apk_read_manifest(meta["channel"])
        if current and meta["version_code"] <= int(current.get("versionCode") or 0):
            return {"error": f"versionCode 必须大于当前已发布版本 v{current['versionCode']}"}
        sha = hashlib.sha256(raw).hexdigest()
        key = (f"{APK_MANIFEST_PREFIX}/{meta['channel']}/v{meta['version_code']}/"
               f"{meta['file_name']}")
        apk_url = _apk_obj_url(key)
        manifest = build_manifest(
            channel=meta["channel"],
            version_code=meta["version_code"],
            version_name=meta["version_name"],
            min_supported_version_code=meta["min_supported"],
            force_update=meta["force_update"],
            title=meta["title"],
            changelog=meta["changelog"],
            file_name=meta["file_name"],
            apk_url=apk_url,
            size_bytes=len(raw),
            sha256=sha,
            certificate_sha256=meta["certificate_sha256"],
        )
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        pg_put(key, raw, "application/vnd.android.package-archive")
        if meta["status"] == "published":
            manifest_raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), indent=2).encode("utf-8")
            pg_put(_apk_manifest_key(meta["channel"]), manifest_raw, "application/json; charset=utf-8")
    except urllib.error.HTTPError as exc:
        return {"error": f"APK/清单上传失败 HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"APK/清单上传失败: {exc}"}
    return {
        "ok": True,
        "status": meta["status"],
        "channel": meta["channel"],
        "versionCode": meta["version_code"],
        "versionName": meta["version_name"],
        "size": len(raw),
        "sha256": sha,
        "key": key,
        "apkUrl": apk_url,
        "manifestUrl": runtime_client.ENDPOINTS.public_web_url(f"apk-update/{meta['channel']}.json"),
        "manifest": manifest,
    }


def h_apk_update_get(d: dict) -> dict:
    """后台读取 TV/移动版当前公开清单。"""
    channel = str(d.get("channel") or "").strip().lower()
    if channel not in APK_CHANNELS:
        return {"error": "渠道必须是 tv 或 mobile"}
    try:
        manifest = _apk_read_manifest(channel)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取清单失败: {exc}"}
    return {"ok": True, "channel": channel, "manifest": manifest}


def h_apk_update_upload(qs: dict, raw: bytes) -> dict:
    try:
        meta = _apk_meta(qs)
    except ValueError as exc:
        return {"error": str(exc)}
    if len(raw) > APK_UPLOAD_DIRECT_MAX:
        return {"error": f"APK 超过直传上限（约 {APK_UPLOAD_DIRECT_MAX // 1048576}MB），请使用分片上传"}
    return _apk_publish(raw, meta)


def h_apk_update_chunk(qs: dict, raw: bytes) -> dict:
    uid = (qs.get("uid") or [""])[0]
    try:
        seq = int((qs.get("seq") or ["-1"])[0])
    except ValueError:
        seq = -1
    if not APK_UPLOAD_UID_RE.fullmatch(uid):
        return {"error": "bad_uid"}
    if not 0 <= seq <= 2047:
        return {"error": "bad_seq"}
    if not 1 <= len(raw) <= APK_UPLOAD_CHUNK_MAX:
        return {"error": f"分片大小不合法（1B–{APK_UPLOAD_CHUNK_MAX // 1048576}MB）"}
    try:
        pg_put(f"{APK_MANIFEST_PREFIX}/uploads/{uid}/part-{seq:04d}", raw, "application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"APK 分片暂存失败: {exc}"}
    return {"ok": True, "seq": seq, "size": len(raw)}


def h_apk_update_commit(qs: dict) -> dict:
    uid = (qs.get("uid") or [""])[0]
    try:
        total = int((qs.get("total") or ["0"])[0])
        size = int((qs.get("size") or ["0"])[0])
    except ValueError:
        return {"error": "bad_total_or_size"}
    if not APK_UPLOAD_UID_RE.fullmatch(uid):
        return {"error": "bad_uid"}
    if not 1 <= total <= 2048 or not 1 <= size <= APK_MAX_BYTES:
        return {"error": "bad_total_or_size"}
    # 提交要在一次请求里把整包回传成单个对象，平台单对象上限先拦下，别白读一遍分片。
    if size > APK_OBJECT_MAX_BYTES:
        return {"error": APK_OBJECT_HINT}
    keys = [f"{APK_MANIFEST_PREFIX}/uploads/{uid}/part-{i:04d}" for i in range(total)]
    buf = bytearray()
    for key in keys:
        try:
            part = pg_get_public(key, max_bytes=APK_UPLOAD_CHUNK_MAX + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"error": "分片不完整，请重新上传"}
            return {"error": f"读取分片失败 HTTP {exc.code}"}
        if not part:
            return {"error": "分片不完整，请重新上传"}
        buf += part
    if len(buf) != size:
        return {"error": f"重组尺寸不符（期望 {size}，实得 {len(buf)}）"}
    try:
        meta = _apk_meta(qs)
    except ValueError as exc:
        return {"error": str(exc)}
    result = _apk_publish(bytes(buf), meta)
    for key in keys:
        _hu_obj_delete(key)
    return result


# ---------------- 还原（按页面清库；破坏性操作，双闸 + 强制备份） ----------------
#
# 前端每个页面一个「还原」按钮：把该页维护的版本 / 记录清回初始状态。
# 三条硬约束（写在代码里，任何调用方都绕不过）：
#   1) 必须显式提供后台密码 + 逐页确认短语——后台当前可能是免登录形态
#      （OTV_ADMIN_NOAUTH=1），只有会话不足以授权销毁数据；
#   2) 删除前把将删的行**完整导出**并随响应返回，前端自动落本地 JSON 备份；
#   3) 电视源 / 影视源只清历史，保留当前生效的那一版：客户端配置与正在分发的
#      m3u 都来自它（vod_source_public_check 读 vod_source_versions 最新一行），
#      删掉等于线上断源。

RESET_SCOPES = {
    "overview": {
        "label": "总览",
        "confirm": "还原总览",
        "desc": "清空卡密、激活流水、热更包（含桶对象与设备上报日志）、公告；电视源 / 影视源不动",
        "tables": (("activate_log", "激活流水"), ("licenses", "卡密"),
                   ("hotupdate_log", "热更设备日志"), ("hotupdate_pkg", "热更包记录"),
                   ("announcements", "公告")),
        "hu_objects": True,
    },
    "licenses": {
        "label": "卡密",
        "confirm": "还原卡密",
        "desc": "清空卡密表（激活流水保留，可在「激活流水」页单独还原）",
        "tables": (("licenses", "卡密"),),
    },
    "logs": {
        "label": "激活流水",
        "confirm": "还原激活流水",
        "desc": "清空激活日志",
        "tables": (("activate_log", "激活流水"),),
    },
    "hu": {
        "label": "热更新",
        "confirm": "还原热更新",
        "desc": "删除全部热更包（含桶内 zip）+ 设备上报日志；下次发布即 v1",
        "tables": (("hotupdate_log", "热更设备日志"), ("hotupdate_pkg", "热更包记录")),
        "hu_objects": True,
    },
    "ann": {
        "label": "运营公告",
        "confirm": "还原公告",
        "desc": "清空公告（客户端回到无公告状态）",
        "tables": (("announcements", "公告"),),
    },
    "iptv": {
        "label": "电视源",
        "confirm": "还原电视源",
        "desc": "清空版本历史与管理日志，**保留当前生效的那一版**（频道表与正在分发的 m3u 不动）",
        "tables": (("iptv_admin_log", "电视源管理日志"),),
        "keep_latest": (("iptv_version", "电视源版本历史", "version"),),
    },
    "vod": {
        "label": "影视源",
        "confirm": "",
        "desc": "影视源的版本表只开放给专用数据库函数",
        "tables": (),
        # 2026-09-18 实测：vod_source_versions / vod_source_drafts 对服务账号是 403
        # （permission denied，只有 SECURITY DEFINER 函数能碰）。REST 通道删不了，
        # 又不想为此加一条新的破坏性 SQL 函数，所以这一页如实说明、不给可点的按钮。
        "unsupported": "影视源的两张表（vod_source_versions / vod_source_drafts）只对数据库内部函数开放，"
                       "后台服务账号无权读写，因此无法从后台清空这一页的版本记录。"
                       "该历史表只追加审计记录；此页面不提供清理入口。",
    },
}

RESET_BACKUP_MAX_BYTES = 10 * 1024 * 1024
RESET_BACKUP_PAGE = 1000


def _rest(table: str, query: str = "", method: str = "GET", body: bytes | None = None,
          headers: dict | None = None, timeout: int = 120):
    """PostgREST 直连（表级）。返回 (status, bytes, headers)。"""
    url = runtime_client.REST_BASE + "/" + table + (("?" + query) if query else "")
    runtime_client.check_url(url)
    h = {"Accept": "application/json", "Authorization": "Bearer " + api_token()}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=timeout) as r:
            return r.status, r.read(RESET_BACKUP_MAX_BYTES + 1), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(2048), dict(e.headers or {})


def _hdr(headers: dict, name: str, default: str = "") -> str:
    """Return a response header without depending on the client's header casing."""
    want = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == want:
            return str(v)
    return default


def _rest_count(table: str) -> int:
    st, body, hdr = _rest(table, "select=*&limit=1", headers={"Prefer": "count=exact"})
    if st >= 300:
        raise RuntimeError(f"{table} 计数失败 HTTP {st}: {body[:200].decode('utf-8', 'replace')}")
    cr = _hdr(hdr, "Content-Range")
    total = cr.rsplit("/", 1)[-1]
    if not total.isdigit():
        raise RuntimeError(f"{table} 计数响应缺少 Content-Range: {cr or hdr}")
    return int(total)


def _rest_newest(table: str, column: str) -> int:
    st, body, _h = _rest(table, f"select={column}&order={column}.desc&limit=1")
    if st >= 300:
        raise RuntimeError(f"{table} 读最新版本失败 HTTP {st}: {body[:200].decode('utf-8', 'replace')}")
    rows = json.loads(body or b"[]")
    return int(rows[0][column]) if rows else 0


def _rest_dump(table: str) -> list:
    """整表导出（备份用）：分页拉全量行。"""
    rows, offset = [], 0
    while True:
        st, body, _h = _rest(table, f"select=*&limit={RESET_BACKUP_PAGE}&offset={offset}")
        if st >= 300:
            raise RuntimeError(f"{table} 导出失败 HTTP {st}: {body[:200].decode('utf-8', 'replace')}")
        page = json.loads(body or b"[]")
        rows.extend(page)
        if len(page) < RESET_BACKUP_PAGE:
            return rows
        offset += len(page)
        if offset > 500_000:
            raise RuntimeError(f"{table} 行数超过 50 万，请分批还原")


def _rest_delete(table: str, query: str = "") -> int:
    st, body, _h = _rest(table, query, method="DELETE")
    if st >= 300:
        raise RuntimeError(f"{table} 删除失败 HTTP {st}: {body[:200].decode('utf-8', 'replace')}")
    return st


def _reset_scope(scope: str) -> dict:
    spec = RESET_SCOPES.get(scope)
    if not spec:
        raise ValueError("未知还原范围: " + str(scope))
    return spec


def reset_preview(scope: str) -> dict:
    """干跑：返回该页将被清空的内容与条数（不写任何数据）。"""
    try:
        spec = _reset_scope(scope)
    except ValueError as e:  # noqa: BLE001
        return {"error": str(e)}
    if spec.get("unsupported"):
        return {"scope": scope, "label": spec["label"], "desc": spec["desc"],
                "unsupported": True, "reason": spec["unsupported"], "items": [], "total": 0}
    items = []
    try:
        for table, label in spec["tables"]:
            items.append({"table": table, "label": label, "count": _rest_count(table), "kept": False})
        for table, label, col in spec.get("keep_latest", ()):
            total = _rest_count(table)
            newest = _rest_newest(table, col)
            kept = 1 if newest > 0 else 0
            items.append({"table": table, "label": label, "count": max(total - kept, 0),
                          "kept": True, "keepNote": f"保留当前生效 v{newest}" if kept else "无版本"})
    except Exception as e:  # noqa: BLE001
        return {"error": f"统计失败: {e}"}
    return {"scope": scope, "label": spec["label"], "desc": spec["desc"],
            "confirm": spec["confirm"], "items": items,
            "total": sum(i["count"] for i in items)}


def reset_apply(scope: str, password: str, confirm: str) -> dict:
    """执行还原：密码 + 确认短语双闸 → 全量备份 → 删除 → 复核。"""
    try:
        spec = _reset_scope(scope)
    except ValueError as e:  # noqa: BLE001
        return {"error": str(e)}
    if spec.get("unsupported"):
        return {"error": spec["unsupported"]}
    if not password or not hmac.compare_digest(str(password).encode("utf-8"), ADMIN_PASS.encode("utf-8")):
        return {"error": "后台密码不正确，已拒绝还原"}
    if (confirm or "").strip() != spec["confirm"]:
        return {"error": f"确认短语不匹配，请输入「{spec['confirm']}」"}

    preview = reset_preview(scope)
    if preview.get("error"):
        return preview

    # 1) 先备份（导出要删的全部行；导出与删除之间的新增行由复核兜底）
    backup_rows, backup_meta = {}, []
    for table, label in spec["tables"]:
        rows = _rest_dump(table)
        backup_rows[table] = rows
        backup_meta.append({"table": table, "label": label, "rows": len(rows)})
    kept_notes = []
    for table, label, col in spec.get("keep_latest", ()):
        newest = _rest_newest(table, col)
        rows = [r for r in _rest_dump(table) if int(r.get(col) or 0) != newest]
        backup_rows[table] = rows
        backup_meta.append({"table": table, "label": label, "rows": len(rows)})
        kept_notes.append({"table": table, "label": label, "keptVersion": newest})
    backup = {
        "schema": 1, "scope": scope, "label": spec["label"],
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "environment": runtime_client.ENDPOINTS.environment_name,
        "rows": backup_rows,
    }
    raw = json.dumps(backup, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(raw) > RESET_BACKUP_MAX_BYTES:
        return {"error": f"备份体积 {len(raw) // 1024}KB 超过上限，请分批还原（先清流水再清卡密）"}

    # 2) 删除（先子表后主表；保留最新版的表用过滤器排除当前生效那一行）
    deleted = []
    try:
        for table, label in spec["tables"]:
            _rest_delete(table)
            deleted.append({"table": table, "label": label, "deleted": len(backup_rows.get(table, []))})
        for table, label, col in spec.get("keep_latest", ()):
            newest = _rest_newest(table, col)
            if newest > 0:
                _rest_delete(table, f"{col}=lt.{newest}")
            deleted.append({"table": table, "label": label, "deleted": len(backup_rows.get(table, [])),
                            "kept": (f"v{newest}" if newest > 0 else "")})
    except Exception as e:  # noqa: BLE001
        return {"error": f"删除中断: {e}。已导出备份见响应 backup，可直接用于恢复", "backup": backup}

    # 3) 热更包：顺手清桶内对象（失败不影响结果，孤立对象无害）
    if spec.get("hu_objects"):
        for row in backup_rows.get("hotupdate_pkg", []):
            key = str(row.get("object_key") or "").strip()
            if not key:
                code = row.get("hot_version_code")
                key = f"backend-hu{code}.zip" if code else ""
            if key:
                _hu_obj_delete(key)

    # 4) 复核：删后条数必须归零（保留版的按预期剩 1）
    verify = []
    for table, label in spec["tables"]:
        left = _rest_count(table)
        verify.append({"table": table, "label": label, "left": left})
    for table, label, col in spec.get("keep_latest", ()):
        left = _rest_count(table)
        verify.append({"table": table, "label": label, "left": left, "kept": "当前生效版本"})
    bad = [v for v in verify if v["left"] > (1 if v.get("kept") else 0)]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "ok": True, "scope": scope, "label": spec["label"],
        "deleted": deleted, "kept": kept_notes, "verify": verify, "residual": bad,
        "backup": {"name": f"admin-reset-{stamp}-{scope}.json", "bytes": len(raw),
                   "sha256": hashlib.sha256(raw).hexdigest(), "rows": backup_meta},
        "backupData": backup,
    }


def h_reset_preview(d: dict) -> dict:
    return reset_preview(str(d.get("scope") or "").strip())


def h_reset(d: dict) -> dict:
    return reset_apply(str(d.get("scope") or "").strip(),
                       str(d.get("password") or ""), str(d.get("confirm") or ""))


def h_stats(_d: dict) -> dict:
    return rpc("admin_stats", {})


def h_licenses(d: dict) -> dict:
    try:
        limit = max(1, min(int(d.get("limit") or 50), 200))
        offset = max(0, int(d.get("offset") or 0))
    except (TypeError, ValueError):
        return {"error": "bad_limit"}
    sort = (d.get("sort") or "updated_at").strip() or "updated_at"
    direction = "asc" if (d.get("dir") or "").strip() == "asc" else "desc"
    return rpc("admin_licenses", {
        "p_q": (d.get("q") or "").strip() or None,
        "p_plan": (d.get("plan") or "").strip() or None,
        "p_status": (d.get("status") or "").strip() or None,
        "p_limit": limit, "p_offset": offset,
        "p_sort": sort, "p_dir": direction})


def h_set_status(d: dict) -> dict:
    code = (d.get("code") or "").strip().upper()
    status = (d.get("status") or "").strip()
    if not re.fullmatch(r"OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}", code):
        return {"error": "bad_code"}
    if status not in ("active", "banned"):
        return {"error": "bad_status"}
    return rpc("admin_set_status", {"p_code": code, "p_status": status})


def h_unbind(d: dict) -> dict:
    code = (d.get("code") or "").strip().upper()
    device = (d.get("device") or "").strip()
    if not re.fullmatch(r"OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}", code):
        return {"error": "bad_code"}
    if device and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", device):
        return {"error": "bad_device"}
    return rpc("admin_unbind", {"p_code": code, "p_device": device or None})


def h_mark_sold(d: dict) -> dict:
    code = (d.get("code") or "").strip().upper()
    sold = bool(d.get("sold"))
    if not re.fullmatch(r"OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}", code):
        return {"error": "bad_code"}
    return rpc("admin_mark_sold", {"p_code": code, "p_sold": sold})


def h_update_note(d: dict) -> dict:
    code = str(d.get("code") or "").strip().upper()
    note = str(d.get("note") or "").strip()
    if not re.fullmatch(r"OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}", code):
        return {"error": "bad_code"}
    if len(note) > 200:
        return {"error": "note_too_long"}
    return rpc("admin_update_note", {"p_code": code, "p_note": note})


def h_logs(d: dict) -> dict:
    """激活流水：读自建库的 activate_log 导出函数。

    历史实现调的是 CloudBase 的 admin_logs(p_code, p_limit)，切到自建栈后
    该函数并不存在，PostgREST 会直接返回
    "Could not find the function public.admin_logs(...)"，后台"激活流水"页永远
    报错。自建栈对应的是 admin_license_v2_log_export(p_limit, p_offset)。
    它不接受按码过滤，所以这里导出后在服务端按 code 过滤，保持前端
    "按码过滤"输入框的既有语义。
    """
    try:
        limit = max(1, min(int(d.get("limit") or 100), 500))
    except (TypeError, ValueError):
        return {"error": "bad_limit"}
    code = (d.get("code") or "").strip().upper()
    out = rpc("admin_license_v2_log_export", {"p_limit": limit, "p_offset": 0})
    if not isinstance(out, dict):
        # rpc() normalizes HTTP failures to {"error": ...}, so a non-dict body
        # means something upstream returned an unexpected shape. Always hand
        # the console a dict it can read .error / .rows off.
        return {"error": "bad_rpc_response"}
    if code:
        rows = [r for r in (out.get("rows") or [])
                if code in str(r.get("code") or "").upper()]
        out = dict(out, total=len(rows), rows=rows)
    return out


def h_hu_list(d: dict) -> dict:
    try:
        limit = max(1, min(int(d.get("limit") or 100), 200))
        offset = max(0, int(d.get("offset") or 0))
    except (TypeError, ValueError):
        return {"error": "bad_limit"}
    out = rpc("hotupdate_admin_list", {"p_limit": limit, "p_offset": offset})
    return {"rows": out} if isinstance(out, list) else out


def h_hu_logs(d: dict) -> dict:
    try:
        limit = max(1, min(int(d.get("limit") or 100), 500))
    except (TypeError, ValueError):
        return {"error": "bad_limit"}
    out = rpc("hotupdate_admin_logs", {"p_limit": limit})
    return {"rows": out} if isinstance(out, list) else out


def h_hu_set_status(d: dict) -> dict:
    try:
        version = int(d.get("version") or 0)
    except (TypeError, ValueError):
        return {"error": "bad_version"}
    status = (d.get("status") or "").strip()
    if version < 1 or status not in ("published", "offline"):
        return {"error": "bad_param"}
    return rpc("hotupdate_admin_set_status", {"p_hot_version": version, "p_status": status})


def h_hu_delete(d: dict) -> dict:
    try:
        version = int(d.get("version") or 0)
    except (TypeError, ValueError):
        return {"error": "bad_version"}
    if version < 1:
        return {"error": "bad_version"}
    out = rpc("hotupdate_admin_delete", {"p_hot_version": version})
    if out.get("error"):
        return out
    # 元数据已删 → 桶内对象顺手清理（失败不影响结果，孤立对象无害）
    try:
        req = urllib.request.Request(runtime_client.ENDPOINTS.storage_object_url(f"backend-hu{version}.zip"),
                                     method="DELETE",
                                     headers={"Authorization": "Bearer " + api_token()})
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=60):
            pass
    except Exception:  # noqa: BLE001
        pass
    return out


def _hu_alloc_version() -> int:
    """版本号自动分配：当前最大版本号 + 1（hotupdate_admin_list 按 code 倒序，首行即最大）"""
    rows = rpc("hotupdate_admin_list", {"p_limit": 1, "p_offset": 0})
    if not isinstance(rows, list):
        raise ValueError("版本号自动分配失败: " + str(rows.get("error", rows)))
    return (rows[0]["hot_version_code"] + 1) if rows else 1


def _hu_parse_meta(qs: dict) -> dict:
    """hu_upload 直传/分片 commit 共用的元数据解析与校验（出错抛 ValueError）"""
    def q(name: str, default: str = "") -> str:
        v = qs.get(name)
        return (v[0] if v else default)

    status = q("status", "published")
    if status not in ("published", "offline", "scheduled"):
        raise ValueError("bad_status")
    publish_at = None
    if status == "scheduled":
        try:
            publish_at = datetime.fromisoformat(q("publishAt").strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("定时发布时间格式不合法")
        if publish_at.tzinfo is None:
            publish_at = publish_at.replace(tzinfo=timezone.utc)
        if publish_at <= datetime.now(timezone.utc):
            raise ValueError("定时发布时间必须晚于当前时间")
    return {"status": status, "publish_at": publish_at, "q": q}


def _hu_publish(raw: bytes, version: int, meta: dict) -> dict:
    """zip 校验 → 传 pgstore 公开桶 → 元数据入库（直传与分片 commit 共用收尾）"""
    import io
    import zipfile

    q = meta["q"]
    if not 1 <= len(raw) <= HU_MAX_BYTES:
        return {"error": f"zip 大小不合法（1B–{HU_MAX_BYTES // 1048576}MB）"}
    # zip 结构校验：五个后端模块 + www 资源，缺一不可（防误传任意 zip 打挂全部设备）
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return {"error": "不是有效的 zip 文件"}
    missing = [n for n in HU_REQUIRED_ENTRIES if n not in names]
    if missing:
        return {"error": "zip 缺少必备条目: " + ", ".join(missing)}
    if not any(n.startswith("www/") for n in names):
        return {"error": "zip 缺少 www/ 资源目录"}

    sha = hashlib.sha256(raw).hexdigest()
    key = f"backend-hu{version}.zip"
    # 1) 对象上传（svc_import 凭据；x-upsert 允许同版本重传覆盖）
    try:
        pg_put(key, raw, "application/zip")
    except urllib.error.HTTPError as e:
        return {"error": f"对象上传失败 HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"对象上传失败: {e}"}
    # 2) 元数据入库（SECURITY DEFINER + svc_import 白名单）
    out = rpc("hotupdate_admin_upsert", {
        "p_hot_version": version, "p_object_key": key, "p_file_size": len(raw),
        "p_sha256": sha, "p_notes": q("notes").strip(), "p_force": q("force") == "true",
        "p_apk_min": 0, "p_apk_max": 2147483647,
        "p_name": q("name").strip(), "p_status": meta["status"],
        "p_publish_at": meta["publish_at"].isoformat() if meta["publish_at"] else None,
    })
    if out.get("error"):
        return {"error": f"元数据入库失败（对象已上传，可在同版本号重试）: {out['error']}"}
    return {"ok": True, "version": version, "size": len(raw), "sha256": sha, "key": key,
            "status": meta["status"]}


def h_hu_upload(qs: dict, raw: bytes) -> dict:
    """发布热更包（小包直传路径，≤HU_DIRECT_MAX）：浏览器直传 zip 字节 → 校验 → 发布。

    元数据字段走 query string（Content-Type 是 zip 原始字节，非 JSON）。
    大包走分片链路（h_hu_upload_chunk + h_hu_upload_commit），避免单次请求占用过多内存。"""
    try:
        version = int((qs.get("version") or ["0"])[0] or 0)
    except ValueError:
        return {"error": "版本号必须是数字"}
    if version and not 1 <= version <= HU_MAX_VERSION:
        return {"error": "版本号超出范围"}
    try:
        version = version if version >= 1 else _hu_alloc_version()
        meta = _hu_parse_meta(qs)
    except ValueError as e:
        return {"error": str(e)}
    return _hu_publish(raw, version, meta)


# 分片上传：单片 ≤4MB（网关 6MB 上限留余量），上传会话 = 客户端生成的 hex uid，
# 分片暂存 pgstore（hu-uploads/{uid}/part-{seq}）——云函数多实例无粘性，状态必须外置
HU_CHUNK_MAX = 4 * 1024 * 1024
HU_DIRECT_MAX = 5 * 1024 * 1024
HU_UPLOAD_UID_RE = re.compile(r"^[0-9a-f]{8,32}$")


def h_hu_upload_chunk(qs: dict, raw: bytes) -> dict:
    """分片上传一片：uid+seq 定位 pgstore 暂存对象（x-upsert 可重传覆盖）"""
    uid = (qs.get("uid") or [""])[0]
    try:
        seq = int((qs.get("seq") or ["-1"])[0])
    except ValueError:
        seq = -1
    if not HU_UPLOAD_UID_RE.match(uid):
        return {"error": "bad_uid"}
    if not 0 <= seq <= 2047:
        return {"error": "bad_seq"}
    if not 1 <= len(raw) <= HU_CHUNK_MAX:
        return {"error": f"分片大小不合法（1B–{HU_CHUNK_MAX // 1048576}MB）"}
    try:
        pg_put(f"hu-uploads/{uid}/part-{seq:04d}", raw, "application/octet-stream")
    except Exception as e:  # noqa: BLE001
        return {"error": f"分片暂存失败: {e}"}
    return {"ok": True, "seq": seq, "size": len(raw)}


def _hu_obj_delete(key: str) -> None:
    """尽力清理暂存分片（DELETE 不被支持时静默跳过——残留小对象无害）"""
    # 删除走私有 storage API：自托管模式下公开对象地址是 VPS 的 HTTPS origin，
    # 不在 check_url 白名单内，用公开地址删除会在这里抛异常（2026-09-24 线上 502）。
    url = runtime_client.ENDPOINTS.storage_object_url(key)
    runtime_client.check_url(url)
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Authorization": "Bearer " + api_token()})
    try:
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=60) as r:
            r.read()
    except Exception:  # noqa: BLE001
        pass


def h_hu_upload_commit(qs: dict) -> dict:
    """分片上传收尾：按序读回全部分片 → 重组校验（尺寸/zip 结构）→ 发布 → 清理暂存。

    版本号在 commit 时才分配（缺省 max+1，可带 version= 显式指定）。expected size 由
    客户端上报，重组后必须精确相等，防止分片缺失/错序拼出坏包。"""
    uid = (qs.get("uid") or [""])[0]
    try:
        total = int((qs.get("total") or ["0"])[0])
        size = int((qs.get("size") or ["0"])[0])
        # 显式版本号保持站点与设备端版本连续；缺省仍走 max+1 自动分配。
        explicit = int((qs.get("version") or ["0"])[0] or 0)
    except ValueError:
        return {"error": "bad_total_or_size"}
    if not HU_UPLOAD_UID_RE.match(uid):
        return {"error": "bad_uid"}
    if not 1 <= total <= 2048 or not 1 <= size <= HU_MAX_BYTES:
        return {"error": "bad_total_or_size"}
    if explicit and not 1 <= explicit <= HU_MAX_VERSION:
        return {"error": "bad_version"}
    keys = [f"hu-uploads/{uid}/part-{i:04d}" for i in range(total)]
    buf = bytearray()
    missing = []
    for k in keys:
        part = pg_get_public(k, max_bytes=HU_CHUNK_MAX + 1)
        if not part:
            missing.append(k.rsplit("-", 1)[-1])
            continue
        buf += part
    if missing:
        return {"error": f"缺少 {len(missing)} 个分片（首个 seq={missing[0]}）——请重新上传"}
    if len(buf) != size:
        for k in keys:
            _hu_obj_delete(k)
        return {"error": f"重组尺寸不符（期望 {size}，实得 {len(buf)}）——请重新上传"}
    try:
        version = explicit if explicit >= 1 else _hu_alloc_version()
        meta = _hu_parse_meta(qs)
    except ValueError as e:
        for k in keys:
            _hu_obj_delete(k)
        return {"error": str(e)}
    out = _hu_publish(bytes(buf), version, meta)
    for k in keys:
        _hu_obj_delete(k)
    return out


def h_hu_health(_d: dict) -> dict:
    """热更端点连通探测：合法 deviceId + apkVersion=0 → 函数参数层拒绝（ret=400 bad_apk），
    证明 网关/云函数/signin/RPC 全链路活着且不入库；ret=0+blackout=true = 端点活着但处于
    紧急停更（OTV_HU_OFF=1），界面显示 BLACKOUT: ON 而非异常。"""
    runtime_client.check_url(HU_ENDPOINT + "/check")
    body = json.dumps({"deviceId": "healthcheck0000", "apkVersion": 0, "hotVersion": 0}).encode("utf-8")
    req = urllib.request.Request(HU_ENDPOINT + "/check", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=8) as r:
            out = json.loads(r.read().decode("utf-8"))
        alive = out.get("ret") == 400 or (out.get("ret") == 0 and out.get("blackout") is True)
        return {"ok": alive, "blackout": bool(out.get("blackout")), "ret": out.get("ret"),
                "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "detail": str(e)[:120]}


def h_gen(d: dict) -> dict:
    plan = (d.get("plan") or "").strip()
    note = (d.get("note") or "").strip() or "后台补货"
    try:
        count = int(d.get("count") or 0)
    except (TypeError, ValueError):
        return {"error": "数量必须是数字"}
    if plan not in ("monthly", "quarterly", "yearly", "lifetime", "weekly"):
        return {"error": "套餐不合法"}
    if not 1 <= count <= 500:
        return {"error": "数量需在 1–500 之间"}
    gen = subprocess.run(
        [sys.executable, str(ROOT / "license_gen.py"), "gen",
         "--plan", plan, "--count", str(count), "--note", note],
        capture_output=True, text=True, cwd=ROOT)
    if gen.returncode != 0:
        return {"error": "签发失败:\n" + (gen.stderr or gen.stdout)[-400:]}
    m = re.search(r"批次 (\S+):", gen.stdout)
    if not m:
        return {"error": "无法解析批次目录:\n" + gen.stdout[-300:]}
    batch = m.group(1)
    imp = subprocess.run(
        [sys.executable, str(ROOT / "selfhosted" / "import_licenses.py"), str(OUT_ROOT / batch)],
        capture_output=True, text=True, cwd=ROOT)
    if imp.returncode != 0:
        return {"error": f"批次 {batch} 已签发但入库失败（可手动重跑 selfhosted/import_licenses.py）:\n"
                         + (imp.stderr or imp.stdout)[-400:]}
    result_match = re.fullmatch(
        r"Inserted (\d+) of (\d+) validated license tickets into self-hosted PostgreSQL\.",
        imp.stdout.strip(),
    )
    if not result_match:
        return {"error": "无法解析卡密导入结果:\n" + imp.stdout[-300:]}
    inserted, validated = map(int, result_match.groups())
    csv_path = OUT_ROOT / batch / "cards.csv"
    try:
        csv_text = csv_path.read_text(encoding="utf-8")
    except OSError:
        csv_text = ""
    # 容器临时目录不作为长期存储：将 CSV 文本一并回传给后台下载入口。
    return {"batch": batch, "imported": inserted, "validated": validated,
            "csv": str(csv_path), "csv_text": csv_text}


# ---------------- v1.20 运营公告管理（后台发布 → app 顶部悬浮窗） ----------------

def h_ann_get(_d: dict) -> dict:
    """当前生效的公告（含 offline 的最后一条，便于界面回显）"""
    return rpc("announcement_admin_get", {})


def h_ann_publish(d: dict) -> dict:
    title = (d.get("title") or "").strip()
    content = (d.get("content") or "").strip()
    status = (d.get("status") or "published").strip()
    if not 1 <= len(title) <= 60:
        return {"error": "标题需 1–60 字"}
    if len(content) > 600:
        return {"error": "正文最长 600 字"}
    if status not in ("published", "offline"):
        return {"error": "bad_status"}
    return rpc("announcement_admin_publish", {
        "p_title": title, "p_content": content, "p_status": status})


# ---------------- v1.20 电视源动态管理（后台#8） ----------------

IPTV_KEY_LATEST = "iptv/iptv-latest.m3u"
IPTV_KEY_META = "iptv/version.json"
# 「API 同步」按钮使用的上游源
IPTV_UPSTREAMS = [
    "https://gh-proxy.com/raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://gh-proxy.com/raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
]
IPTV_FETCH_MAX = 16 * 1024 * 1024
_private_ip = re.compile(
    r"^(127\.|10\.|0\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$|f[cd]|fe80)")


def _assert_public_https(u: str) -> None:
    p = urllib.parse.urlsplit(u)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("仅允许 https URL")
    port = p.port or 443
    for info in socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("目标不是公网地址")


def safe_fetch(u: str, timeout: float = 30.0) -> bytes:
    """出站抓取（M3U 导入 URL / 上游同步）：仅 https + 公网 IP + ≤3 跳重定向（SSRF 防护）"""
    cur, hops = u, 0
    while True:
        _assert_public_https(cur)
        req = urllib.request.Request(cur, headers={"User-Agent": "Mozilla/5.0 OptimalTV-admin"})
        try:
            with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=timeout) as r:
                return r.read(IPTV_FETCH_MAX)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc or hops >= 3:
                    raise
                hops += 1
                cur = urllib.parse.urljoin(cur, loc)
                continue
            raise


def parse_m3u_to_channels(raw: str) -> list:
    """m3u 文本 → [{name, group, logo, lines: [url]}]（同名同组聚线路，上限 24 线/频道）"""
    out, by_key = [], {}
    name = logo = group = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = line[line.rfind(",") + 1:].strip()
            m = re.search(r'tvg-logo="([^"]*)"', line)
            logo = m.group(1) if m else ""
            m = re.search(r'group-title="([^"]*)"', line)
            group = (m.group(1) if m else "").strip()
        elif line.startswith(("http://", "https://")):
            if not name or not line:
                continue
            key = group + "|" + name
            ch = by_key.get(key)
            if ch is None:
                ch = {"name": name, "group": group or "其他", "logo": logo, "lines": []}
                by_key[key] = ch
                out.append(ch)
            if len(ch["lines"]) < 24 and line not in ch["lines"]:
                ch["lines"].append(line)
            name = logo = group = ""
    return out


def serialize_m3u(channels: list) -> str:
    parts = ["#EXTM3U"]
    for ch in channels:
        logo = (ch.get("logo") or "").replace('"', "")
        group = (ch.get("group") or "其他").replace('"', "")
        for u in ch.get("lines") or []:
            parts.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{ch["name"]}')
            parts.append(u)
    return "\n".join(parts) + "\n"


def _write_meta(mode: str, extra: dict) -> None:
    meta = {"ret": 0, "mode": mode,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    meta.update(extra)
    pg_put(IPTV_KEY_META, json.dumps(meta, ensure_ascii=False).encode("utf-8"),
           "application/json")


def h_iptv_list(_d: dict) -> dict:
    out = rpc("iptv_admin_list", {})
    mode = rpc("iptv_admin_get_mode", {})
    if isinstance(out, dict):
        out["mode"] = mode.get("mode", "auto")
    return out


def h_iptv_save(d: dict) -> dict:
    lines = d.get("lines")
    if lines is None:
        lines = [{"url": u} for u in (d.get("urls") or [])]
    return rpc("iptv_admin_upsert_channel", {
        "p_id": d.get("id"), "p_name": (d.get("name") or "").strip(),
        "p_group": (d.get("group") or "").strip() or "其他",
        "p_logo": (d.get("logo") or "").strip(),
        "p_sort": int(d.get("sort") or 0), "p_enabled": bool(d.get("enabled", True)),
        "p_note": (d.get("note") or "").strip(), "p_lines": lines})


IPTV_LOGO_MAX = 2 * 1024 * 1024


def h_iptv_logo_upload(qs: dict, raw: bytes) -> dict:
    """台标图片直传：PNG/JPG/WEBP ≤2MB → pgstore 公开桶 iptv/logos/（热更包同桶）。
    魔数校验真实格式（不信任扩展名）；key 带频道名哈希+时间戳避免缓存串台；
    返回匿名可读 URL（与客户端拉 iptv-latest.m3u 同一公开路径），直接可填 tvg-logo。"""
    import hashlib

    def q(name: str, default: str = "") -> str:
        v = qs.get(name)
        return (v[0] if v else default)

    if not 1 <= len(raw) <= IPTV_LOGO_MAX:
        return {"error": "图片大小不合法（1B–2MB）"}
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        ext, ctype = "png", "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        ext, ctype = "jpg", "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext, ctype = "webp", "image/webp"
    else:
        return {"error": "仅支持 PNG / JPG / WEBP 图片（按文件内容识别）"}
    slug = hashlib.md5((q("name") or "logo").encode("utf-8")).hexdigest()[:10]
    key = f"iptv/logos/{slug}-{time.strftime('%Y%m%d%H%M%S')}.{ext}"
    try:
        pg_put(key, raw, ctype)
    except Exception as e:  # noqa: BLE001
        return {"error": f"上传失败: {e}"}
    return {"ret": 0, "url": obj_url(key), "key": key, "size": len(raw)}


def h_iptv_delete(d: dict) -> dict:
    try:
        cid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        return {"error": "bad_id"}
    return rpc("iptv_admin_delete_channel", {"p_id": cid})


def h_iptv_batch(d: dict) -> dict:
    return rpc("iptv_admin_batch", {"p_ops": d.get("ops") or []})


def h_iptv_import(d: dict) -> dict:
    """M3U 导入：{m3u: 文本} 或 {url: https 地址}；mode = merge | replace"""
    mode = d.get("mode") or "merge"
    raw_text = (d.get("m3u") or "").strip()
    url = (d.get("url") or "").strip()
    try:
        if url:
            raw_bytes = safe_fetch(url, timeout=60.0)
            raw_text = raw_bytes.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"error": f"拉取失败: {e}"}
    if not raw_text or "#EXTM3U" not in raw_text and "://" not in raw_text:
        return {"error": "内容为空或不是 M3U"}
    channels = parse_m3u_to_channels(raw_text)
    if not channels:
        return {"error": "未解析到任何频道"}
    out = rpc("iptv_admin_import", {"p_channels": channels, "p_mode": mode})
    if isinstance(out, dict) and out.get("ret") == 0:
        out["parsed"] = len(channels)
    return out


def h_iptv_sync(d: dict) -> dict:
    """上游 API 同步：拉 IPTV_UPSTREAMS（与每日云函数同源）→ merge/replace 入库（先审后发）"""
    mode = d.get("mode") or "merge"
    last_err = None
    for u in IPTV_UPSTREAMS:
        try:
            raw = safe_fetch(u, timeout=60.0).decode("utf-8", "replace")
            if "#EXTM3U" not in raw:
                continue
            channels = parse_m3u_to_channels(raw)
            if not channels:
                continue
            out = rpc("iptv_admin_import", {"p_channels": channels, "p_mode": mode})
            if isinstance(out, dict) and out.get("ret") == 0:
                out["src"] = u
                out["parsed"] = len(channels)
                return out
            last_err = out
        except Exception as e:  # noqa: BLE001
            last_err = {"error": str(e)}
    return last_err or {"error": "全部上游不可达"}


# ===== 自动管理模式（2026-09-05 需求③：自动找源 → 自动测活 → 自动推送）=====

def _assert_public_media_url(u: str) -> None:
    """直播流测活出站校验：http/https 均可（直播源大量 http），但必须公网 IP。
    与 _assert_public_https 同款 DNS 级校验（解析全内网 → 拒绝）。"""
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("bad url")
    port = p.port or (443 if p.scheme == "https" else 80)
    for info in socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("目标不是公网地址")


_PROBE_UA = {"User-Agent": "Mozilla/5.0 OptimalTV-probe"}


def _probe_iptv_line(url: str, timeout: float = 5.0):
    """单线测活（与 iptv-rebuild 云函数 probe_line 同语义）：清单可拉 + 首分片
    Range 探测 8KB。返回耗时 ms；失败返回 None。重定向手动 ≤2 跳（每跳校验公网）。"""
    cur, hops = url, 0
    try:
        _assert_public_media_url(cur)
        t0 = time.time()
        opener = urllib.request.build_opener(runtime_client.NoRedirect)
        body = None
        for _ in range(3):
            req = urllib.request.Request(cur, headers=_PROBE_UA)
            try:
                with opener.open(req, timeout=timeout) as r:
                    if r.status != 200:
                        return None
                    body = r.read(4096)
                    break
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if not loc or hops >= 2:
                        return None
                    hops += 1
                    cur = urllib.parse.urljoin(cur, loc)
                    _assert_public_media_url(cur)
                    continue
                return None
        if body is None:
            return None
        if b"#EXTM3U" in body[:64]:
            text = body.decode("utf-8", "replace")
            seg = next((l.strip() for l in text.splitlines()
                        if l.strip() and not l.startswith("#")), "")
            if seg:
                absu = urllib.parse.urljoin(cur, seg)
                _assert_public_media_url(absu)
                req2 = urllib.request.Request(
                    absu, headers={**_PROBE_UA, "Range": "bytes=0-8192"})
                with opener.open(req2, timeout=timeout) as r2:
                    if r2.status not in (200, 206):
                        return None
                    r2.read(2048)
        return int((time.time() - t0) * 1000)
    except Exception:
        return None


_AUTO_RUN_LOCK = threading.Lock()


def h_iptv_auto_run(d: dict) -> dict:
    """自动管理一键执行：拉上游聚合源（自动找源）→ 全线并发测活（自动测试可用）→
    剔死线/全死频道、按测速排序 → 直推 hotupdate 桶 iptv-latest.m3u + version.json
    （自动推送；mode=auto 不翻转，与每日 iptv-rebuild 云函数同语义）。
    SCF 60s 硬顶：测活预算 ~35s 截止，超时未完成的线路按死线剔除（本轮保守）；
    广度优先（每频道先测主线路，再测备线），保证覆盖尽量多的频道。"""
    if not _AUTO_RUN_LOCK.acquire(blocking=False):
        return {"error": "已有自动重建在执行中，请稍候"}
    try:
        from concurrent.futures import ThreadPoolExecutor, FIRST_EXCEPTION, wait
        deadline = time.time() + 38.0
        channels = []
        src_used = ""
        for u in IPTV_UPSTREAMS:
            try:
                raw = safe_fetch(u, timeout=20.0).decode("utf-8", "replace")
                if "#EXTM3U" not in raw:
                    continue
                parsed = parse_m3u_to_channels(raw)
                if parsed:
                    channels, src_used = parsed, u
                    break
            except Exception:
                continue
        if not channels:
            return {"error": "上游聚合源全部不可达"}

        # 广度优先的测活任务序：第 i 轮取每频道第 i 条线（≤8 线/频道，客户端同上限）
        rounds = []
        for i in range(8):
            rounds.extend(ch["lines"][i] for ch in channels if len(ch["lines"]) > i)
        urls = list(dict.fromkeys(rounds))[:520]

        scores = {}
        with ThreadPoolExecutor(max_workers=32) as ex:
            futs = {ex.submit(_probe_iptv_line, u): u for u in urls}
            pending = set(futs)
            while pending:
                left = deadline - time.time()
                if left <= 0:
                    for f in pending:
                        f.cancel()
                    break
                done, pending = wait(pending, timeout=min(left, 2.0),
                                     return_when=FIRST_EXCEPTION)
                for f in done:
                    ms = f.result() if not f.cancelled() and f.exception() is None else None
                    scores[futs[f]] = ms

        alive = 0
        usable = []
        for ch in channels:
            lines = [u for u in ch["lines"] if scores.get(u) is not None]
            if not lines:
                continue
            lines.sort(key=lambda u: scores[u])
            alive += len(lines)
            usable.append({**ch, "lines": lines[:8]})
        if not usable:
            return {"error": "测活后无可用频道（上游源质量异常）"}

        m3u = serialize_m3u(usable)
        n_lines = sum(len(c["lines"]) for c in usable)
        sha = hashlib.sha256(m3u.encode("utf-8")).hexdigest()
        beg = rpc("iptv_admin_publish_begin", {"p_notes": "自动重建（后台触发）"})
        version = beg.get("version") if isinstance(beg, dict) else None
        if not version:
            return {"error": f"发布登记失败: {beg}"}
        key = f"iptv/iptv-v{version}.m3u"
        try:
            pg_put(key, m3u.encode("utf-8"), "audio/x-mpegurl")
            pg_put(IPTV_KEY_LATEST, m3u.encode("utf-8"), "audio/x-mpegurl")
            _write_meta("auto", {"version": version, "channels": len(usable),
                                 "lines": n_lines, "notes": "auto-run", "source": "admin-auto"})
        except Exception as e:  # noqa: BLE001
            rpc("iptv_admin_publish_confirm", {
                "p_version": version, "p_object_key": key, "p_sha256": sha,
                "p_file_size": len(m3u.encode("utf-8")), "p_channels": len(usable),
                "p_lines": n_lines, "p_status": "failed"})
            return {"error": f"对象上传失败: {e}"}
        out = rpc("iptv_admin_publish_confirm", {
            "p_version": version, "p_object_key": key, "p_sha256": sha,
            "p_file_size": len(m3u.encode("utf-8")), "p_channels": len(usable),
            "p_lines": n_lines, "p_status": "published"})
        if isinstance(out, dict) and out.get("ret") == 0:
            out.update({"version": version, "channels": len(usable), "lines": n_lines,
                        "probed": len(scores), "alive": alive, "dead": len(urls) - alive,
                        "upstream": src_used, "elapsedMs": int((time.time() - (deadline - 38.0)) * 1000)})
        return out
    finally:
        _AUTO_RUN_LOCK.release()


def h_iptv_probe(d: dict) -> dict:
    """频道线路测活（频道编辑用）：{lines: [url]} → [{url, ms|null}]"""
    lines = d.get("lines") or []
    if not lines or not isinstance(lines, list):
        return {"error": "bad_lines"}
    lines = lines[:24]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(12, len(lines))) as ex:
        ms = list(ex.map(_probe_iptv_line, lines))
    return {"ret": 0, "results": [{"url": u, "ms": m} for u, m in zip(lines, ms)]}


def h_iptv_publish(d: dict) -> dict:
    """发布：DB 导出 → 序列化 → v{n} 归档 + latest 覆盖 + meta(manual) → 确认。
    发布即切托管模式（每日自动推流暂停，防止次晨覆盖手动列表）。"""
    notes = (d.get("notes") or "").strip()
    exp = rpc("iptv_admin_export", {})
    channels = (exp.get("channels") if isinstance(exp, dict) else None) or []
    usable = [c for c in channels if c.get("lines")]
    if not usable:
        return {"error": "频道表为空（先导入或添加频道）"}
    m3u = serialize_m3u(usable)
    n_lines = sum(len(c["lines"]) for c in usable)
    sha = hashlib.sha256(m3u.encode("utf-8")).hexdigest()

    beg = rpc("iptv_admin_publish_begin", {"p_notes": notes})
    version = beg.get("version") if isinstance(beg, dict) else None
    if not version:
        return {"error": f"发布登记失败: {beg}"}
    key = f"iptv/iptv-v{version}.m3u"
    try:
        pg_put(key, m3u.encode("utf-8"), "audio/x-mpegurl")
        pg_put(IPTV_KEY_LATEST, m3u.encode("utf-8"), "audio/x-mpegurl")
        _write_meta("manual", {"version": version, "channels": len(usable),
                               "lines": n_lines, "notes": notes, "source": "admin"})
    except Exception as e:  # noqa: BLE001
        rpc("iptv_admin_publish_confirm", {
            "p_version": version, "p_object_key": key, "p_sha256": sha,
            "p_file_size": len(m3u.encode("utf-8")), "p_channels": len(usable),
            "p_lines": n_lines, "p_status": "failed"})
        return {"error": f"对象上传失败: {e}"}
    rpc("iptv_admin_set_mode", {"p_mode": "manual"})
    out = rpc("iptv_admin_publish_confirm", {
        "p_version": version, "p_object_key": key, "p_sha256": sha,
        "p_file_size": len(m3u.encode("utf-8")), "p_channels": len(usable),
        "p_lines": n_lines, "p_status": "published"})
    if isinstance(out, dict) and out.get("ret") == 0:
        out.update({"version": version, "channels": len(usable), "lines": n_lines,
                    "sha256": sha, "size": len(m3u.encode("utf-8"))})
    return out


def h_iptv_rollback(d: dict) -> dict:
    """回滚：克隆目标版本元数据 → 复制其归档对象到 latest → meta(manual) → 确认发布"""
    try:
        target = int(d.get("version") or 0)
    except (TypeError, ValueError):
        return {"error": "bad_version"}
    beg = rpc("iptv_admin_rollback_begin", {"p_target_version": target})
    version = beg.get("version") if isinstance(beg, dict) else None
    if not version:
        return {"error": f"回滚登记失败: {beg}"}
    src_key = f"iptv/iptv-v{target}.m3u"
    try:
        raw = pg_get_public(src_key)
        if not raw:
            raise RuntimeError(f"归档对象 {src_key} 不存在或为空")
        pg_put(IPTV_KEY_LATEST, raw, "audio/x-mpegurl")
        _write_meta("manual", {"version": version, "rollbackFrom": target, "source": "admin"})
    except Exception as e:  # noqa: BLE001
        return {"error": f"对象复制失败: {e}"}
    rpc("iptv_admin_set_mode", {"p_mode": "manual"})
    src = rpc("iptv_admin_versions", {"p_limit": 200})
    meta = next((v for v in (src if isinstance(src, list) else [])
                 if v.get("version") == target), {})
    out = rpc("iptv_admin_publish_confirm", {
        "p_version": version, "p_object_key": src_key,
        "p_sha256": meta.get("sha256") or "", "p_file_size": meta.get("file_size") or len(raw or b""),
        "p_channels": meta.get("channels") or 0, "p_lines": meta.get("lines") or 0,
        "p_status": "published"})
    if isinstance(out, dict) and out.get("ret") == 0:
        out.update({"version": version, "rollbackFrom": target})
    return out


def h_iptv_versions(d: dict) -> dict:
    try:
        limit = max(1, min(int(d.get("limit") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    out = rpc("iptv_admin_versions", {"p_limit": limit})
    return {"rows": out} if isinstance(out, list) else out


def h_iptv_logs(d: dict) -> dict:
    try:
        limit = max(1, min(int(d.get("limit") or 200), 500))
    except (TypeError, ValueError):
        limit = 200
    out = rpc("iptv_admin_logs", {"p_limit": limit})
    return {"rows": out} if isinstance(out, list) else out


def h_iptv_mode(d: dict) -> dict:
    mode = (d.get("mode") or "").strip()
    if mode not in ("auto", "manual"):
        return rpc("iptv_admin_get_mode", {})
    out = rpc("iptv_admin_set_mode", {"p_mode": mode})
    # meta 同步切模式：manual 保护 latest 不被每日推流覆盖；auto 恢复每日推流
    try:
        if mode == "manual":
            _write_meta("manual", {"source": "admin"})
        else:
            _write_meta("auto", {"source": "admin"})
    except Exception:  # noqa: BLE001
        pass
    return out


def _guard_iptv_publisher(handler, *, automatic: bool = False):
    """Serialize local scheduled work with manual publishes through a DB lease."""
    if not SELFHOSTED:
        return handler

    def guarded(d: dict) -> dict:
        from selfhosted.scheduler import DatabasePublishLock

        try:
            with DatabasePublishLock(rpc).hold() as acquired:
                if not acquired:
                    return {"error": "电视源正在发布，请稍后重试"}
                lease_token = _IPTV_PUBLISH_LEASE.set(acquired)
                try:
                    acquired.assert_valid()
                    if automatic:
                        mode = rpc("iptv_admin_get_mode", {})
                        if not isinstance(mode, dict) or mode.get("mode") != "auto":
                            return {"skipped": True, "reason": "manual mode is active"}
                    result = handler(d)
                    acquired.assert_valid()
                    return result
                finally:
                    _IPTV_PUBLISH_LEASE.reset(lease_token)
        except Exception as exc:  # noqa: BLE001 - never publish without the shared lock
            sys.stderr.write(
                f"[admin] IPTV publish lock unavailable ({type(exc).__name__}); operation skipped\n"
            )
            return {"error": "电视源发布锁不可用，操作未执行"}

    guarded.__name__ = getattr(handler, "__name__", "guarded")
    guarded.__doc__ = getattr(handler, "__doc__", None)
    return guarded


# The auto route is also used by the daily scheduler. Manual publication,
# rollback, and mode changes take the same database lease.
h_iptv_auto_run = _guard_iptv_publisher(h_iptv_auto_run, automatic=True)
h_iptv_publish = _guard_iptv_publisher(h_iptv_publish)
h_iptv_rollback = _guard_iptv_publisher(h_iptv_rollback)
h_iptv_mode = _guard_iptv_publisher(h_iptv_mode)


def h_iptv_latest(_d: dict) -> dict:
    """当前线上 latest 内容探针（拉公开桶，验证客户端视角）"""
    try:
        raw = pg_get_public(IPTV_KEY_LATEST, 2 * 1024 * 1024)
        if raw is None:
            return {"error": "latest 对象不存在（从未发布过）"}
        text = raw.decode("utf-8", "replace")
        chs = parse_m3u_to_channels(text)
        managed = any(c["group"] != "其他" for c in chs)
        meta = {}
        try:
            mraw = pg_get_public(IPTV_KEY_META, 65536)
            if mraw:
                meta = json.loads(mraw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            pass
        return {"channels": len(chs), "lines": sum(len(c["lines"]) for c in chs),
                "managed": managed, "mode": meta.get("mode"), "updatedAt": meta.get("updatedAt"),
                "head": text[:400]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def h_iptv_group_list(_d: dict) -> dict:
    return rpc("iptv_admin_group_list", {})


def h_iptv_group_create(d: dict) -> dict:
    return rpc("iptv_admin_group_create", {"p_name": (d.get("name") or "").strip()})


def h_iptv_group_rename(d: dict) -> dict:
    try:
        gid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        return {"error": "bad_id"}
    return rpc("iptv_admin_group_rename", {"p_id": gid, "p_name": (d.get("name") or "").strip()})


def h_iptv_group_delete(d: dict) -> dict:
    try:
        gid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        return {"error": "bad_id"}
    return rpc("iptv_admin_group_delete", {"p_id": gid, "p_move_to": (d.get("moveTo") or "未分组").strip()})


def h_iptv_group_move(d: dict) -> dict:
    try:
        gid = int(d.get("id") or 0)
        delta = int(d.get("delta") or 0)
    except (TypeError, ValueError):
        return {"error": "bad_id"}
    return rpc("iptv_admin_group_move", {"p_id": gid, "p_delta": delta})


def h_iptv_refresh_token(_d: dict) -> dict:
    """前端直调 iptv-http 函数 /refresh 的触发令牌（与函数环境变量 OTV_REFRESH_TOKEN 同值，
    仅登录会话可取）"""
    return {"token": os.environ.get("OTV_REFRESH_TOKEN", "")}


# ---------------- 影视播放源管理 ----------------

def vod_test_grade(score: int) -> str:
    return "优秀" if score >= 80 else "良好" if score >= 60 else "一般" if score >= 40 else "不可用"


def h_vod_source_list(_d: dict) -> dict:
    return rpc("vod_source_admin_list", {})


def h_vod_source_save(d: dict) -> dict:
    name = str(d.get("name") or "").strip()[:80]
    site_url = str(d.get("site_url") or "").strip()
    api_url = str(d.get("api_url") or "").strip()
    adapter = str(d.get("adapter_type") or "").strip()
    if not name or adapter not in ("hhkan", "macms_json", "macms_xml"):
        return {"error": "名称或适配器类型不合法"}
    try:
        vod_sources._validate_public_url(site_url)
        if api_url:
            vod_sources._validate_public_url(api_url)
    except ValueError as exc:
        return {"error": str(exc)}
    return rpc("vod_source_admin_save", {
        "p_id": d.get("id"), "p_name": name, "p_site_url": site_url,
        "p_api_url": api_url, "p_adapter_type": adapter,
        "p_enabled": bool(d.get("enabled", True)), "p_sort_order": int(d.get("sort_order") or 0),
    })


def h_vod_source_set_enabled(d: dict) -> dict:
    source_id = str(d.get("id") or "").strip()
    if not source_id:
        return {"error": "缺少影视源 ID"}
    enabled = d.get("enabled")
    if not isinstance(enabled, bool):
        return {"error": "启用状态不合法"}
    return rpc("vod_source_admin_set_enabled", {
        "p_id": source_id, "p_enabled": enabled,
    })


def h_vod_source_set_priority(d: dict) -> dict:
    source_id = str(d.get("id") or "").strip()
    if not source_id:
        return {"error": "缺少影视源 ID"}
    return rpc("vod_source_admin_set_priority", {"p_id": source_id})


def h_vod_source_delete(d: dict) -> dict:
    return rpc("vod_source_admin_delete", {"p_id": d.get("id")}) if d.get("id") else {"error": "缺少影视源 ID"}


def h_vod_source_discover(d: dict) -> dict:
    try:
        source = vod_sources.discover_source(str(d.get("site_url") or "").strip())
        return {"ok": True, "source": {
            "id": source.id, "name": source.name, "site_url": source.site_url,
            "api_url": source.api_url, "adapter_type": source.adapter_type,
            "enabled": source.enabled, "revision": source.revision}}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:180]}


def _first_maccms_preview(raw: dict) -> str:
    for group in str(raw.get("vod_play_url") or "").split("$$$"):
        for episode in group.split("#"):
            value = episode.rsplit("$", 1)[-1].strip()
            if value.startswith(("http://", "https://")):
                return value
    return ""


def h_vod_source_test(d: dict) -> dict:
    title = str(d.get("title") or "").strip()[:120]
    if not title:
        return {"error": "请输入影片名称"}
    year = str(d.get("year") or "").strip()[:4]
    episode = max(1, min(9999, int(d.get("episode") or 1)))
    raw = d.get("source") if isinstance(d.get("source"), dict) else d
    try:
        source = vod_sources.VodSource(
            id=str(raw.get("id") or "test"), name=str(raw.get("name") or "测试源"),
            site_url=str(raw.get("site_url") or ""), api_url=str(raw.get("api_url") or ""),
            adapter_type=str(raw.get("adapter_type") or "macms_json"), enabled=True,
            revision=int(raw.get("revision") or 1))
        started = time.monotonic()
        preview, match = "", 0
        if source.adapter_type.startswith("macms"):
            candidates = vod_sources.MacCmsAdapter(source).search({"title": title, "year": year})
            subject = {"title": title, "year": year, "season": 0}
            best = max(candidates, key=lambda item: vod_sources.match_score(subject, item), default=None)
            if best:
                match = vod_sources.match_score(subject, best)
                preview = _first_maccms_preview(best.get("raw") or {})
        else:
            token = hhkan.get_search_token()
            page = hhkan.get_page("/search?t=%s&k=%s&p=1" % (urllib.parse.quote(token), urllib.parse.quote(title)))
            cards = hhkan._parse_vod_list(page)
            exact = next((item for item in cards if vod_sources.normalize_title(item.get("title")) == vod_sources.normalize_title(title)), None)
            if exact:
                match = 90
                detail = hhkan.parse_detail(hhkan.get_page("/detail/%s.html" % exact["id"]), exact["id"])
                episodes = ((detail.get("sources") or [{}])[0].get("episodes") or [])
                first = episodes[min(episode - 1, len(episodes) - 1)] if episodes else {}
                play = hhkan.parse_play_page(hhkan.get_page("/play/%s-%s-%s.html" % (
                    exact["id"], first.get("pid", 0), first.get("vid", 0))))
                preview = ((play.get("sources") or [{}])[0].get("url") or "")
        elapsed = int((time.monotonic() - started) * 1000)
        speed = 100 if elapsed <= 1000 else 80 if elapsed <= 3000 else 55 if elapsed <= 6000 else 30
        matched = match >= vod_sources.AUTO_MATCH_THRESHOLD
        score = int(match * 0.6 + speed * 0.4) if preview and matched else min(39, int(match * 0.4))
        summary_state = "可试看" if preview and matched else (
            "找到候选但未通过自动匹配" if preview else "未取得可播放地址")
        return {"grade": vod_test_grade(score), "score": score,
                "summary": "%s，匹配度 %d，响应 %dms" % (summary_state, match, elapsed),
                # The console stores this value in its latency column and sorts
                # by it. Omitting it made every source look like 0ms, so the
                # fastest-source ordering put them all first.
                "matched": matched, "preview_url": preview, "latency_ms": elapsed}
    except Exception as exc:  # noqa: BLE001
        return {"grade": "不可用", "score": 0, "summary": str(exc)[:160], "matched": False,
                "preview_url": "", "latency_ms": None}


def h_vod_source_publish(_d: dict) -> dict:
    return rpc("vod_source_admin_publish", {})


def h_vod_source_versions(_d: dict) -> dict:
    """可回滚的历史版本列表（新到旧）。撤销功能的前置。"""
    return rpc("vod_source_admin_versions", {})


def h_vod_source_probe_device(d: dict) -> dict:
    """转发到**设备**侧做源探活（2026-09-30）。

    刻意不在这里 ping 源：后台跑在 VPS 上，磐石云没有境外出口，Cloudflare 段的源
    从 VPS 必然超时。2026-09-30 实测 10 个源有 8 个在后台显示「失败」，但同一批
    在实机网络下 7 个能正常取流——照后台结果清理会砍掉 App 里能用的线路。

    设备基址由 App 报上来（`d["base"]`），默认取最近一次心跳记录；拿不到就
    明确报错，不静默回落到「VPS 自己测」——那正是要避开的误判源。
    """
    base = str(d.get("base") or "").strip().rstrip("/")
    if not base:
        return {"error": "device_unavailable",
                "hint": "缺少 base：设备侧探活需要 App 的内嵌后端地址，"
                        "请传 base=http://<设备IP>:8090"}
    if not base.startswith(("http://", "https://")):
        return {"error": "bad_base"}
    target = base + "/vod/sources/probe"
    try:
        req = urllib.request.Request(target, headers={"User-Agent": UA})
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=90) as r:
            body = r.read()
    except Exception as exc:  # noqa: BLE001
        return {"error": "device_probe_failed", "detail": str(exc)[:160], "base": base}
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {"error": "device_probe_bad_response", "base": base}


def h_vod_source_rollback(d: dict) -> dict:
    """把 drafts 回滚到某个历史版本（BUG-06）。

    刻意**不隐式发布**：回滚只改 drafts，线上要生效还得再点一次「发布」。
    否则用户会以为「撤销了」而线上其实还是那份配置。
    """
    try:
        version = int(d.get("version"))
    except (TypeError, ValueError):
        return {"error": "bad_version"}
    if version < 1:
        return {"error": "bad_version"}
    return rpc("vod_source_admin_rollback", {"p_version": version})


def h_vod_source_speed(d: dict) -> dict:
    """单源测速：只打一次该源最轻的请求，返回毫秒延时，供后台「全部测速」后按延时排序。
    macms 取 `?ac=list`（接口探活）；其余适配器打站内搜索页（无 API，只能测页面）。"""
    raw = d.get("source") if isinstance(d.get("source"), dict) else d
    adapter = str(raw.get("adapter_type") or "")
    if adapter.startswith("macms"):
        base = str(raw.get("api_url") or "").rstrip("/")
        url = base + "?ac=list" if base else ""
    else:
        site = str(raw.get("site_url") or "").rstrip("/")
        url = site + "/search?k=" + urllib.parse.quote("测试") + "&p=1" if site else ""
    if not url:
        return {"ok": False, "latency_ms": 0, "error": "该源未配置可用地址"}
    started = time.monotonic()
    try:
        vod_sources._validate_public_url(url, resolve_dns=True)
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 OptimalTV-admin"})
        with urllib.request.urlopen(request, timeout=8) as response:
            vod_sources._validate_public_url(response.geturl(), resolve_dns=True)
            body = response.read(256 * 1024)
            status = response.status
        return {"ok": status == 200 and len(body) > 0,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status": status, "bytes": len(body)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": int((time.monotonic() - started) * 1000),
                "error": str(exc)[:120]}


def h_health(_d: dict) -> dict:
    """激活端点连通探测：用非法格式码打公网端点，网关/云函数活着即返回 ret=400
    （参数层拒绝，不入库不留审计痕迹；signin/RPC 链路由业务请求自然验证）。
    带 protocol=2 并以 400 为主判据；426（协议升级提示）同样说明链路活着，
    否则 v1.26+ 分协议校验上线后探针会恒定误报「异常」。"""
    url = runtime_client.ACTIVATE_ENDPOINT
    runtime_client.check_url(url)
    body = json.dumps({"code": "OTV-0000-0000", "deviceId": "healthcheck0000",
                       "protocol": 2}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=8) as r:
            out = json.loads(r.read().decode("utf-8"))
        return {"ok": out.get("ret") in (400, 426), "ret": out.get("ret"),
                "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "detail": str(e)[:120]}


ROUTES = {
    "/api/stats": h_stats,
    "/api/health": h_health,
    "/api/licenses": h_licenses,
    "/api/set_status": h_set_status,
    "/api/unbind": h_unbind,
    "/api/mark_sold": h_mark_sold,
    "/api/update_note": h_update_note,
    "/api/logs": h_logs,
    "/api/gen": h_gen,
    "/api/hu_list": h_hu_list,
    "/api/hu_logs": h_hu_logs,
    "/api/hu_set_status": h_hu_set_status,
    "/api/hu_delete": h_hu_delete,
    "/api/hu_health": h_hu_health,
    "/api/apk_update_get": h_apk_update_get,
    "/api/ann_get": h_ann_get,
    "/api/ann_publish": h_ann_publish,
    # 还原（按页面清库）：密码 + 逐页确认短语双重闸，且强制先导出备份
    "/api/reset_preview": h_reset_preview,
    "/api/reset": h_reset,
    # 影视源草稿、测试与显式发布（均位于统一会话门禁之后）
    "/api/vod_source_list": h_vod_source_list,
    "/api/vod_source_save": h_vod_source_save,
    "/api/vod_source_set_enabled": h_vod_source_set_enabled,
    "/api/vod_source_set_priority": h_vod_source_set_priority,
    "/api/vod_source_delete": h_vod_source_delete,
    "/api/vod_source_discover": h_vod_source_discover,
    "/api/vod_source_test": h_vod_source_test,
    "/api/vod_source_speed": h_vod_source_speed,
    "/api/vod_source_publish": h_vod_source_publish,
    "/api/vod_source_versions": h_vod_source_versions,
    "/api/vod_source_rollback": h_vod_source_rollback,
    "/api/vod_source_probe_device": h_vod_source_probe_device,
    # v1.20 电视源管理
    "/api/iptv_list": h_iptv_list,
    "/api/iptv_save": h_iptv_save,
    "/api/iptv_delete": h_iptv_delete,
    "/api/iptv_batch": h_iptv_batch,
    "/api/iptv_import": h_iptv_import,
    "/api/iptv_sync": h_iptv_sync,
    "/api/iptv_auto_run": h_iptv_auto_run,
    "/api/iptv_probe": h_iptv_probe,
    "/api/iptv_publish": h_iptv_publish,
    "/api/iptv_rollback": h_iptv_rollback,
    "/api/iptv_versions": h_iptv_versions,
    "/api/iptv_logs": h_iptv_logs,
    "/api/iptv_mode": h_iptv_mode,
    "/api/iptv_latest": h_iptv_latest,
    "/api/iptv_group_list": h_iptv_group_list,
    "/api/iptv_group_create": h_iptv_group_create,
    "/api/iptv_group_rename": h_iptv_group_rename,
    "/api/iptv_group_delete": h_iptv_group_delete,
    "/api/iptv_group_move": h_iptv_group_move,
    "/api/iptv_refresh_token": h_iptv_refresh_token,
}


class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _session_ok(self) -> bool:
        c = http_cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except http_cookies.CookieError:
            return False
        morsel = c.get(COOKIE)
        return bool(morsel and check_session(morsel.value))

    def _peer(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/favicon.ico", "/logo.png"):
            try:
                self._send(200, LOGO_PATH.read_bytes(), "image/png")
            except OSError:
                self._send(404, b"logo missing", "text/plain; charset=utf-8")
            return
        if self.path == "/api/whoami":
            if NOAUTH or self._session_ok():
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            else:
                self._send(401, b'{"ok":false}', "application/json; charset=utf-8")
            return
        if self.path != "/":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            html = HTML_PATH.read_text(encoding="utf-8")
        except OSError:
            self._send(500, "admin.html 缺失".encode(), "text/plain; charset=utf-8")
            return
        # 登录态由前端 whoami 判定（同一 HTML 含登录浮层；服务端不重复渲染）
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        # 登录：唯一匿名 POST（限速 + 常时比较）
        if path == "/api/login":
            if login_too_busy(self._peer()):
                self._send(429, '{"error": "尝试过于频繁，请 1 分钟后再试"}'.encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                d = {}
            user = str(d.get("user") or "")
            ok = hmac.compare_digest(user.encode(), ADMIN_USER.encode()) and \
                hmac.compare_digest(str(d.get("pass") or "").encode(), ADMIN_PASS.encode())
            note_login(self._peer(), ok)
            if not ok:
                self._send(401, '{"error": "账号或密码错误"}'.encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            cookie = f"{COOKIE}={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
            if CONTAINER_MODE:
                cookie += "; Secure"
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8",
                       [("Set-Cookie", cookie)])
            return

        # 会话门禁：其余 /api/* 一律须带有效 Cookie
        if not (NOAUTH or self._session_ok()):
            self._send(401, '{"error": "未登录或会话已过期"}'.encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        # 热更包发布：zip 原始字节直传（仅小包；大包走分片链路——网关 body 上限 ~6MB）
        if path == "/api/hu_upload":
            n = int(self.headers.get("Content-Length") or 0)
            if not 1 <= n <= HU_MAX_BYTES:
                self._send(200, '{"error": "zip 大小不合法"}'.encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            if n > HU_DIRECT_MAX:
                self._send(200, json.dumps(
                    {"error": f"包体 {n // 1048576}MB 超过网关直传上限（~5MB），请使用分片上传"},
                    ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            raw = self.rfile.read(n)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = h_hu_upload(qs, raw)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        # 热更包分片：单片原始字节 + uid/seq 走 query string（暂存 pgstore）
        if path == "/api/hu_upload_chunk":
            n = int(self.headers.get("Content-Length") or 0)
            if not 1 <= n <= HU_CHUNK_MAX:
                self._send(200, json.dumps({"error": f"分片大小不合法（1B–{HU_CHUNK_MAX // 1048576}MB）"},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            raw = self.rfile.read(n)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = h_hu_upload_chunk(qs, raw)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        # 热更包分片收尾：读回重组 → 校验发布（元数据走 query string）
        if path == "/api/hu_upload_commit":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = h_hu_upload_commit(qs)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        # APK 直传：小包直接写入对象桶；大包由下方分片链路处理。
        if path == "/api/apk_update_upload":
            n = int(self.headers.get("Content-Length") or 0)
            if not 1 <= n <= APK_MAX_BYTES:
                self._send(200, json.dumps({"error": "APK 大小不合法"}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            if n > APK_UPLOAD_DIRECT_MAX:
                self._send(200, json.dumps({"error": "APK 超过直传上限，请使用分片上传"}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            raw = self.rfile.read(n)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = h_apk_update_upload(qs, raw)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/apk_update_chunk":
            n = int(self.headers.get("Content-Length") or 0)
            if not 1 <= n <= APK_UPLOAD_CHUNK_MAX:
                self._send(200, json.dumps({"error": "APK 分片大小不合法"}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            raw = self.rfile.read(n)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = h_apk_update_chunk(qs, raw)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/apk_update_commit":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = h_apk_update_commit(qs)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        # M3U 文件导入：浏览器直传 m3u 文本，模式走 query string
        if path == "/api/iptv_upload":
            n = int(self.headers.get("Content-Length") or 0)
            if not 1 <= n <= 32 * 1024 * 1024:
                self._send(200, '{"error": "文件过大（≤32MB）"}'.encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            raw = self.rfile.read(n)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            mode = (qs.get("mode") or ["merge"])[0]
            result = h_iptv_import({"m3u": raw.decode("utf-8", "replace"), "mode": mode})
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        # 台标图片直传：PNG/JPG/WEBP 原始字节，频道名走 query string
        if path == "/api/iptv_logo_upload":
            n = int(self.headers.get("Content-Length") or 0)
            if not 1 <= n <= IPTV_LOGO_MAX:
                self._send(200, '{"error": "图片过大（≤2MB）"}'.encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            raw = self.rfile.read(n)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result = h_iptv_logo_upload(qs, raw)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        fn = ROUTES.get(path)
        if fn is None:
            self._send(404, b'{"error": "not found"}', "application/json; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError):
            data = {}
        result = fn(data if isinstance(data, dict) else {})
        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # 精简访问日志
        sys.stderr.write("[admin] %s %s\n" % (self._peer(), fmt % args))


def main() -> None:
    bind = "0.0.0.0" if CONTAINER_MODE else "127.0.0.1"
    srv = ThreadingHTTPServer((bind, PORT), Handler)
    scheduler = None
    if SELFHOSTED and os.environ.get("OTV_IPTV_SCHEDULER_ENABLED", "1") == "1":
        from selfhosted.scheduler import DailyIptvScheduler

        schedule_at = os.environ.get("OTV_IPTV_DAILY_TIME", "06:30")
        try:
            hour_text, minute_text = schedule_at.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if len(hour_text) != 2 or len(minute_text) != 2:
                raise ValueError
            scheduler = DailyIptvScheduler(
                lambda: h_iptv_auto_run({"source": "daily-scheduler"}),
                hour=hour,
                minute=minute,
                logger=lambda msg: print(f"[scheduler] {msg}"),
            )
            scheduler.start()
            print(f"IPTV daily rebuild scheduled for {schedule_at} Asia/Shanghai")
        except ValueError:
            srv.server_close()
            raise ValueError("OTV_IPTV_DAILY_TIME must use HH:MM")
    url = f"http://127.0.0.1:{PORT}/" if not CONTAINER_MODE else f"http://0.0.0.0:{PORT}/（容器内监听地址）"
    print(f"My TV 后台管理系统 v1.20 已启动: {url}")
    print(f"功能: 卡密 · 补货 · 热更新 · APK 更新 · 公告 · 电视源管理（v1.20）")
    if CONTAINER_MODE:
        if _ADMIN_CREDENTIALS:
            print("登录门禁: 已从安全运行时凭据加载")
        elif os.environ.get("OTV_ADMIN_PASS"):
            print("登录门禁: 账号 OTV_ADMIN_USER / 密码 OTV_ADMIN_PASS（环境变量）")
        else:
            print("⚠ 容器未加载管理员凭据，使用临时密码；请配置运行时凭据文件")
    else:
        print(f"登录门禁: 账号 {ADMIN_USER} / 密码 {ADMIN_PASS}")
        print("仅本机可访问；关闭本窗口即下线。")
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        if scheduler:
            scheduler.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
