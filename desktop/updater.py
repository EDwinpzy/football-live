"""My TV Windows 桌面版自动更新。

复用服务端既有的热更体系（与 Android 同一个 /hotupdate/check 契约），
但不引入第二套发布流程：服务端照旧用 hu-upload.mjs 发一个 zip，桌面端
自己去拉。

设计要点：
  * 更新范围 = 后端 Python 模块 + www 前端 + 快照，也就是 backend.zip 里的
    全部内容；EXE 壳本身（PyInstaller 冻结的 launcher/webview/quickjs）不在
    范围内，那部分改动仍然要重发版本。
  * 生效时机 = 下次启动。下载完成后只写 pending 标记，不动正在运行的进程，
    用户不会在播放中被重启。
  * 失败策略 = 静默回退。任何一步出错（网络、校验、解压、目录缺失）都退回
    内置 backend.zip 并继续启动，更新永远不能把应用卡死在起不来。

安全口径与 launcher.py 的内置包解压一致：拒绝绝对路径、`..`、盘符冒号与
符号链接；先写临时目录再 os.replace，标记最后落盘。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


CHECK_PATH = "/hotupdate/check"
STORAGE_PREFIX = "/v1/storages/object/hotupdate"
USER_AGENT = "MyTV-Desktop-Updater/1.0"
CHECK_TIMEOUT = 12.0
DOWNLOAD_TIMEOUT = 180.0
MANIFEST_MAX_BYTES = 256 * 1024

_VERSION_RE = re.compile(r"(\d+)")


class UpdateError(ValueError):
    """更新流程中的可预期错误；调用方一律降级处理，不弹窗、不阻断启动。"""


@dataclass(frozen=True)
class UpdateInfo:
    version: int
    url: str
    sha256: str
    size: int
    object_key: str


@dataclass(frozen=True)
class UpdateCheckResult:
    update_available: bool
    version: Optional[int] = None
    url: Optional[str] = None
    sha256: Optional[str] = None
    size: Optional[int] = None
    object_key: Optional[str] = None


def update_root(app_data_root: Path) -> Path:
    """更新状态目录：%LOCALAPPDATA%\\MyTV\\update。"""
    return Path(app_data_root) / "update"


def _pending_file(root: Path) -> Path:
    return Path(root) / "pending.json"


def read_pending(root: Path) -> Optional[tuple]:
    """读下次启动要生效的更新标记；损坏或缺失一律当作没有。"""
    path = _pending_file(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version")
    sha256 = payload.get("sha256")
    key = payload.get("objectKey")
    if not isinstance(version, int) or not isinstance(sha256, str):
        return None
    if not isinstance(key, str) or not key:
        return None
    return version, sha256, key


def write_pending(root: Path, version: int, sha256: str, object_key: str) -> None:
    """原子写 pending 标记。必须在目录解压完成之后调用。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    payload = {"version": int(version), "sha256": sha256, "objectKey": object_key}
    fd, tmp_name = tempfile.mkstemp(prefix=".pending-", suffix=".json", dir=str(root))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(str(tmp), str(_pending_file(root)))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def clear_pending(root: Path) -> None:
    _pending_file(root).unlink(missing_ok=True)


def _staging_name(object_key: str) -> str:
    """object_key -> 落地目录名。去掉 .zip 后缀，避免目录名看起来像个包。"""
    name = Path(str(object_key or "")).name
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return name or "update"


def _normalize_origin(origin: str) -> str:
    value = str(origin or "").strip().rstrip("/")
    if not value:
        raise ValueError("api origin is required")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("api origin must be an HTTPS origin")
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("api origin must not carry path/credentials/query")
    return "https://%s" % parsed.netloc


def _version_from_key(object_key: str, payload: dict) -> int:
    """版本号优先取包名（backend-hu21.zip -> 21），否则回落到响应字段。"""
    match = _VERSION_RE.search(str(object_key or ""))
    if match:
        return int(match.group(1))
    for key in ("version", "hotVersion"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    raise UpdateError("cannot determine update version from %r" % (object_key,))


def post_check(origin: str, hot_version: int, apk_version: int, device_id: str) -> dict:
    """POST /hotupdate/check，走 launcher 已装好的私有 CA opener。"""
    url = _normalize_origin(origin) + CHECK_PATH
    body = json.dumps({
        "hotVersion": int(hot_version),
        "apkVersion": int(apk_version),
        "deviceId": str(device_id or ""),
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as response:
        raw = response.read(MANIFEST_MAX_BYTES)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("hotupdate/check returned non-JSON") from exc


def check_for_update(origin: str, hot_version: int, apk_version: int,
                     device_id: str) -> UpdateCheckResult:
    base = _normalize_origin(origin)
    payload = post_check(base, hot_version, apk_version, device_id)
    if not payload.get("hasUpdate"):
        return UpdateCheckResult(update_available=False)
    package = payload.get("pkg")
    if not isinstance(package, dict):
        raise UpdateError("hotupdate/check hasUpdate without pkg")
    sha256 = str(package.get("sha256") or "").strip().lower()
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise UpdateError("hotupdate/check pkg has no usable sha256")
    object_key = str(package.get("objectKey") or "").strip()
    if not object_key:
        raise UpdateError("hotupdate/check pkg has no objectKey")
    url = str(package.get("url") or "").strip()
    if not url:
        url = base + STORAGE_PREFIX + "/" + object_key.lstrip("/")
    size = package.get("size")
    return UpdateCheckResult(
        update_available=True,
        version=_version_from_key(object_key, payload),
        url=url,
        sha256=sha256,
        size=int(size) if isinstance(size, int) and size > 0 else 0,
        object_key=object_key)


def download_file(url: str, timeout: float = DOWNLOAD_TIMEOUT) -> bytes:
    """下载到内存。桌面包约 8 MB，不必流式落盘。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _validate_member(name: str) -> str:
    normalized = str(name).replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise UpdateError("unsafe zip member: %r" % name)
    parts = [p for p in normalized.split("/") if p]
    if not parts or any(p == ".." for p in parts):
        raise UpdateError("unsafe zip member: %r" % name)
    if any(":" in p for p in parts):
        raise UpdateError("unsafe zip member: %r" % name)
    return "/".join(parts)


def _safe_extract(archive: zipfile.ZipFile, staging: Path) -> None:
    base = staging.resolve()
    for info in archive.infolist():
        name = _validate_member(info.filename)
        if ((info.external_attr >> 16) & 0xF000) == 0xA000:
            raise UpdateError("zip symlink member is not allowed: %r" % info.filename)
        destination = (staging / name).resolve()
        try:
            destination.relative_to(base)
        except ValueError:
            raise UpdateError("zip member escapes staging: %r" % info.filename)
    for info in archive.infolist():
        name = _validate_member(info.filename)
        destination = staging / name
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, open(destination, "wb") as sink:
            shutil.copyfileobj(source, sink)


def stage_update(root: Path, info: UpdateInfo) -> Path:
    """下载 -> 校验 -> 安全解压 -> 原子落盘 -> 写 pending。返回最终目录。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    payload = download_file(info.url)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != info.sha256:
        raise UpdateError("sha256 mismatch: expected %s, got %s" % (info.sha256, digest))
    if info.size and len(payload) != info.size:
        raise UpdateError("size mismatch: expected %d, got %d" % (info.size, len(payload)))

    target = root / _staging_name(info.object_key)
    staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(root)))
    try:
        zip_path = staging / "_payload.zip"
        zip_path.write_bytes(payload)
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, staging)
        zip_path.unlink(missing_ok=True)
        if not (staging / "proxy.py").is_file() or not (staging / "www" / "app.js").is_file():
            raise UpdateError("update package is missing proxy.py or www/app.js")
        (staging / ".extracted").write_text("ok", encoding="ascii")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        os.replace(str(staging), str(target))
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    write_pending(root, info.version, info.sha256, info.object_key)
    return target


def resolve_backend_dir(root: Path, built_in: Path) -> tuple:
    """决定本次启动用哪份后端。返回 (路径, 是否来自热更)。"""
    root = Path(root)
    pending = read_pending(root)
    if pending is not None:
        _version, _sha256, object_key = pending
        staged = root / _staging_name(object_key)
        if ((staged / ".extracted").is_file()
                and (staged / "proxy.py").is_file()
                and (staged / "www" / "app.js").is_file()):
            return staged, True
    return Path(built_in), False


def current_hot_version(root: Path) -> int:
    """当前生效的热更版本号；没有热更时为 0。"""
    pending = read_pending(Path(root))
    return pending[0] if pending else 0


def prune_old_updates(root: Path, keep: int = 2) -> None:
    """只保留最近 keep 个已暂存的包，避免 update 目录无限增长。"""
    root = Path(root)
    if not root.is_dir():
        return
    candidates = [p for p in root.iterdir()
                  if p.is_dir() and (p / ".extracted").is_file() and p.name != ".stage-x"]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in candidates[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


class BackgroundUpdater:
    """启动后放一条守护线程去检查/下载，不阻塞窗口显示。"""

    def __init__(self, app_data_root: Path, api_origin: str, device_id: str,
                 hot_version: int, apk_version: int, on_state=None):
        self.root = update_root(Path(app_data_root))
        self.api_origin = api_origin
        self.device_id = device_id
        self.hot_version = hot_version
        self.apk_version = apk_version
        self.on_state = on_state
        self._thread = None
        self._lock = threading.Lock()
        self.latest: Optional[UpdateCheckResult] = None

    def _emit(self, stage: str, detail: str = "") -> None:
        if self.on_state is not None:
            try:
                self.on_state(stage, detail)
            except Exception:  # noqa: BLE001 - 回调异常不能影响更新流程
                pass

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="mytv-updater", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            self._emit("checking")
            result = check_for_update(
                self.api_origin, self.hot_version, self.apk_version, self.device_id)
            self.latest = result
            if not result.update_available:
                self._emit("idle")
                return
            self._emit("downloading", "v%s" % result.version)
            stage_update(self.root, UpdateInfo(
                version=result.version, url=result.url, sha256=result.sha256,
                size=result.size or 0, object_key=result.object_key))
            prune_old_updates(self.root)
            self._emit("ready", "v%s" % result.version)
        except Exception as exc:  # noqa: BLE001 - 更新失败绝不冒泡到启动流程
            self._emit("failed", str(exc)[:160])
