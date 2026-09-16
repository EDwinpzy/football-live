#!/usr/bin/env python3
"""My TV 卡密后台管理系统（v1.20：登录门禁 + IPTV 电视源动态管理 + 云端部署）

两种运行形态（同一份代码）：
  本地  python tools/admin_server.py
        → 自动打开浏览器 http://127.0.0.1:8791（仅本机可访问）
  云端  Docker 容器 / 腾讯云 CloudBase 云托管（外网 HTTPS 访问）
        环境变量：PORT=8080（容器平台注入）
                  OTV_ADMIN_USER / OTV_ADMIN_PASS   登录门禁账号（云端必设）
                  OTV_AUTH_USER / OTV_AUTH_PASS     svc_import 服务账号（云端必设）
                  OTV_PRIV_KEY                      卡密签发私钥 PEM（云端可选：
                                                    不设则「补货」禁用，仍可本地补货）
                  OTV_ADMIN_SECRET                  会话签名密钥（云端建议设；不设则
                                                    由 ADMIN_USER/ADMIN_PASS 派生固定值）

登录门禁（v1.20 需求：移到腾讯云 + 外网访问 + 登录门禁）：
  - GET / 页面内置登录浮层；POST /api/login 校验账号密码 → 下发 HMAC 签名的
    HttpOnly 会话 Cookie（12h 有效；密钥固定，多实例/重启后仍有效）
  - 除 /api/login、静态资源外全部接口须带有效 Cookie；登录失败限速（5 次/60s 锁定）

功能（v1.17 → v1.20）:
  总览    各套餐库存 + 近 24h 激活动作 + 通道状态
  卡密    搜索/筛选、出售/封禁/解绑、复制
  日志    activate_log 审计记录按码过滤
  补货    license_gen.py 私钥签发 → 自动入库 → cards.csv（云端未注入私钥时禁用）
  热更新  发布/上下线/删除热更包、设备上报日志（v1.18）
  公告    运营公告发布/下线（v1.20）
  电视源  频道 CRUD、M3U 导入（文件/文本/URL）、上游 API 同步、版本发布、
          回滚、排序/批量启停、操作日志导出、自动/托管模式切换（v1.20 后台#8）
          发布产物写 pgstore 公开桶 iptv/iptv-v{n}.m3u + iptv-latest.m3u，
          客户端 SOURCES 首位 ≤30 分钟自动拉取生效（客户端自动更新）；
          发布即切「托管模式」（每日 iptv-rebuild 自动推流暂停，防覆盖）。

安全边界：
  - 本地形态仅绑定 127.0.0.1；云端形态由容器平台提供 HTTPS + 登录门禁把关；
  - 云端凭据走环境变量（svc signin 2h token 内存缓存），管理 RPC 在数据库侧
    白名单校验 auth.uid()（仅 svc_import 可执行）；
  - M3U 导入 URL / 上游同步出站走 _safe_fetch（仅 https + 公网 IP + ≤3 跳重定向，
    与 iptv-rebuild 云函数同款 SSRF 防护）；
  - 私钥默认不上云（OTV_PRIV_KEY 未设时补货页提示在本地执行）。
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
import db_import  # noqa: E402  复用 signin/URL 白名单（凭据读 keys/svc_import_*.txt 或 env）
import hhkan  # noqa: E402
import vod_sources  # noqa: E402

PORT = int(os.environ.get("PORT") or 8791)
CLOUD = bool(os.environ.get("PORT")) or os.environ.get("OTV_CLOUD") == "1"
HTML_PATH = ROOT / "admin.html"
# 品牌字标：本地 = Android 工程 res 只读引用；云端（HTTP 云函数/容器）= 同目录捆绑文件
_LOGO_LOCAL = ROOT / "my_tv_logo.png"
_LOGO_REPO = ROOT.parent / "android" / "app" / "src" / "main" / "res" / "drawable" / "my_tv_logo.png"
LOGO_PATH = _LOGO_LOCAL if _LOGO_LOCAL.exists() else _LOGO_REPO

# ---------------- 登录门禁（v1.20） ----------------

ADMIN_USER = os.environ.get("OTV_ADMIN_USER") or "admin"
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


ADMIN_PASS = _resolve_admin_pass()
# 会话签名密钥：优先 env；缺省时由登录凭据派生**固定**值。
# 不能退回随机值——云函数多实例/冷启动各自随机会让 Cookie 时验时不验
# （2026-09-16 线上实测：登录成功后再连发 8 次带 Cookie 请求，7 次 401）。
SESSION_SECRET = os.environ.get("OTV_ADMIN_SECRET") or hashlib.sha256(
    f"otv-admin-session:{ADMIN_USER}:{ADMIN_PASS}".encode("utf-8")).hexdigest()
SESSION_TTL = 12 * 3600
COOKIE = "otv_admin_sess"


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


# ---------------- 云端凭据注入（env → keys/ 文件，供 db_import/license_gen 子进程复用） ----------------

def _inject_env_creds() -> None:
    # 云端（HTTP 云函数）文件系统只读——写入失败静默跳过，凭据经 db_import 的
    # 环境变量分支生效；容器形态可写则落文件（供 license_gen/db_import 子进程复用）
    try:
        user = os.environ.get("OTV_AUTH_USER")
        pw = os.environ.get("OTV_AUTH_PASS")
        if user and pw:
            kd = ROOT / "keys"
            kd.mkdir(parents=True, exist_ok=True)
            for name, val in (("svc_import_user.txt", user), ("svc_import_pass.txt", pw)):
                f = kd / name
                if not f.exists():
                    f.write_text(val, encoding="utf-8")
        pk = os.environ.get("OTV_PRIV_KEY")
        if pk:
            f = ROOT / "keys" / "private_key.pem"
            if not f.exists():
                f.write_text(pk.replace("\\n", "\n"), encoding="utf-8")
    except OSError:
        pass


_inject_env_creds()

_tok = {"v": None, "exp": 0.0}
_tok_lock = threading.Lock()


def cloud_token() -> str:
    with _tok_lock:
        if _tok["v"] and time.time() < _tok["exp"] - 120:
            return _tok["v"]
        t = db_import.signin()
        _tok["v"] = t
        _tok["exp"] = time.time() + 6600  # token 有效 2h，提前 10 分钟续
        return _tok["v"]


def rpc(name: str, payload: dict) -> dict:
    url = db_import.BASE + "/v1/rdb/rest/rpc/" + name
    db_import.check_url(url)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cloud_token()})
    try:
        with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # PostgREST 错误形态是 {"code","message",...}（无 "error" 键）——归一化，
        # 否则调用方 out.get("error") 判空会把 RPC 失败误判成成功
        try:
            out = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"error": f"云端返回 HTTP {e.code}"}
        if isinstance(out, dict) and "error" not in out:
            return {"error": out.get("message") or out.get("hint") or f"HTTP {e.code} {out.get('code', '')}"}
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": f"请求失败: {e}"}


# ---------------- v1.18 热更新管理（公测版） ----------------

HU_BUCKET = "hotupdate"
HU_ENDPOINT = "https://appletv-d5ge1bth794873f76.service.tcloudbase.com/hotupdate"
# 与 APK HotUpdateManager / hotupdate_admin_upsert RPC 的校验口径一致
HU_REQUIRED_ENTRIES = (
    "proxy.py", "hhkan.py", "scraper.py", "team_backdrop.py", "decrypt_stream.js",
    "douban_catalog.py", "douban_snapshot.json", "media_index.py", "pinyin_map.json", "vod_api.py",
    "vod_sources.py", "vod_sources.default.json",
)
HU_MAX_BYTES = 220 * 1024 * 1024
# 显式指定热更版本号的上限（防手滑输入天文数字把设备端比较逻辑顶掉）
HU_MAX_VERSION = 10_000_000


def obj_url(key: str) -> str:
    return f"{db_import.BASE}/v1/storages/object/{HU_BUCKET}/{key}"


def pg_put(key: str, raw: bytes, ctype: str) -> None:
    url = obj_url(key) + "?x-upsert=true"
    db_import.check_url(url)
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Content-Type": ctype,
        "Authorization": "Bearer " + cloud_token(),
        "x-upsert": "true",
    })
    with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=600) as r:
        r.read()


def pg_get_public(key: str, max_bytes: int = 32 * 1024 * 1024):
    """读公开桶对象（匿名 GET，与客户端拉取同一路径）"""
    url = obj_url(key)
    db_import.check_url(url)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=120) as r:
        return r.read(max_bytes)


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


def h_logs(d: dict) -> dict:
    try:
        limit = max(1, min(int(d.get("limit") or 100), 500))
    except (TypeError, ValueError):
        return {"error": "bad_limit"}
    return rpc("admin_logs", {"p_code": (d.get("code") or "").strip() or None, "p_limit": limit})


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
        req = urllib.request.Request(obj_url(f"backend-hu{version}.zip"), method="DELETE",
                                     headers={"Authorization": "Bearer " + cloud_token()})
        with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=60):
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
    大包走分片链路（h_hu_upload_chunk + h_hu_upload_commit）：CloudBase HTTP 网关
    请求体上限实测 ~6MB（>6MB 返回 406 空响应体，前端 r.json() 报
    "Unexpected end of JSON input"——2026-09-07 需求④根因），python-backend.zip
    已 36MB+，必须分片。"""
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
    url = obj_url(key)
    db_import.check_url(url)
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Authorization": "Bearer " + cloud_token()})
    try:
        with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=60) as r:
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
        # 显式版本号（切换云端基座时的版本连续性：库里最高 v17、设备已到 v21，
        # 自动 max+1 只会拿到 v18 被设备忽略）；缺省仍走 max+1 自动分配。
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
    u = urllib.parse.urlsplit(HU_ENDPOINT + "/check")
    if u.scheme != "https" or u.hostname != "appletv-d5ge1bth794873f76.service.tcloudbase.com" or u.port is not None:
        return {"ok": False, "detail": "目标 URL 不在白名单"}
    body = json.dumps({"deviceId": "healthcheck0000", "apkVersion": 0, "hotVersion": 0}).encode("utf-8")
    req = urllib.request.Request(HU_ENDPOINT + "/check", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=8) as r:
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
        [sys.executable, str(ROOT / "db_import.py"), str(ROOT / "out" / batch)],
        capture_output=True, text=True, cwd=ROOT)
    if imp.returncode != 0:
        return {"error": f"批次 {batch} 已签发但入库失败（可手动重跑 db_import.py）:\n"
                         + (imp.stderr or imp.stdout)[-400:]}
    return {"batch": batch, "imported": count,
            "csv": str(ROOT / "out" / batch / "cards.csv")}


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
# 与 cloudfunctions/iptv-rebuild 同源上游（「API 同步」按钮用）
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
            with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=timeout) as r:
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
        opener = urllib.request.build_opener(db_import.NoRedirect)
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
        score = int(match * 0.6 + speed * 0.4) if preview else min(39, int(match * 0.4))
        return {"grade": vod_test_grade(score), "score": score,
                "summary": "%s，匹配度 %d，响应 %dms" % ("可试看" if preview else "未取得可播放地址", match, elapsed),
                "matched": match >= vod_sources.AUTO_MATCH_THRESHOLD, "preview_url": preview}
    except Exception as exc:  # noqa: BLE001
        return {"grade": "不可用", "score": 0, "summary": str(exc)[:160], "matched": False, "preview_url": ""}


def h_vod_source_publish(_d: dict) -> dict:
    return rpc("vod_source_admin_publish", {})


def h_health(_d: dict) -> dict:
    """激活端点连通探测：用非法格式码打公网端点，网关/云函数活着即返回 ret=400
    （参数层拒绝，不入库不留审计痕迹；signin/RPC 链路由业务请求自然验证）。"""
    url = "https://appletv-d5ge1bth794873f76.service.tcloudbase.com/activate"
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname != "appletv-d5ge1bth794873f76.service.tcloudbase.com" or u.port is not None:
        return {"ok": False, "detail": "目标 URL 不在白名单"}
    body = json.dumps({"code": "OTV-0000-0000", "deviceId": "healthcheck0000"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=8) as r:
            out = json.loads(r.read().decode("utf-8"))
        return {"ok": out.get("ret") == 400, "ret": out.get("ret"),
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
    "/api/logs": h_logs,
    "/api/gen": h_gen,
    "/api/hu_list": h_hu_list,
    "/api/hu_logs": h_hu_logs,
    "/api/hu_set_status": h_hu_set_status,
    "/api/hu_delete": h_hu_delete,
    "/api/hu_health": h_hu_health,
    "/api/ann_get": h_ann_get,
    "/api/ann_publish": h_ann_publish,
    # 影视源草稿、测试与显式发布（均位于统一会话门禁之后）
    "/api/vod_source_list": h_vod_source_list,
    "/api/vod_source_save": h_vod_source_save,
    "/api/vod_source_delete": h_vod_source_delete,
    "/api/vod_source_discover": h_vod_source_discover,
    "/api/vod_source_test": h_vod_source_test,
    "/api/vod_source_publish": h_vod_source_publish,
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
            if self._session_ok():
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
            if CLOUD:
                cookie += "; Secure"
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8",
                       [("Set-Cookie", cookie)])
            return

        # 会话门禁：其余 /api/* 一律须带有效 Cookie
        if not self._session_ok():
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
    bind = "0.0.0.0" if CLOUD else "127.0.0.1"
    srv = ThreadingHTTPServer((bind, PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/" if not CLOUD else f"http://0.0.0.0:{PORT}/（容器平台对外域名）"
    print(f"My TV 后台管理系统 v1.20 已启动: {url}")
    print(f"功能: 卡密 · 补货 · 热更新 · 公告 · 电视源管理（v1.20）")
    if CLOUD:
        if os.environ.get("OTV_ADMIN_PASS"):
            print("登录门禁: 账号 OTV_ADMIN_USER / 密码 OTV_ADMIN_PASS（环境变量）")
        else:
            print("⚠ 云端形态未设置 OTV_ADMIN_PASS，使用随机密码（见 keys/admin_pass.txt）——"
                  "建议配置环境变量固定密码")
    else:
        print(f"登录门禁: 账号 {ADMIN_USER} / 密码 {ADMIN_PASS}")
        print("仅本机可访问；关闭本窗口即下线。")
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
