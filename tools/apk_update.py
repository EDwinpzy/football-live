"""APK 应用内更新清单的纯校验与组装逻辑。

这个模块不访问网络，供后台上传接口和单元测试共同使用。APK 文件本身仍由
admin_server.py 写入 CloudBase 对象存储；客户端只读取这里生成的稳定 JSON 结构。
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any


CHANNELS = {
    "tv": {
        "device_type": "tv",
        "package_name": "com.qiubo.optimaltv.public",
    },
    "mobile": {
        "device_type": "mobile",
        "package_name": "com.qiubo.optimaltv.mobile.public",
    },
}

HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_FILENAME_RE = re.compile(r'^[^\x00-\x1f\\/:*?"<>|]{1,120}\.apk$')


def _text(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def normalize_certificates(value: Any) -> list[str]:
    """将逗号/空格/换行分隔的证书摘要规范成无分隔符大写 hex。"""
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = re.split(r"[\s,;]+", _text(value))
    result: list[str] = []
    for item in raw:
        cert = _text(item).replace(":", "").replace("-", "")
        if not cert:
            continue
        if not HEX64_RE.fullmatch(cert):
            raise ValueError("签名证书 SHA-256 必须是 64 位十六进制")
        if cert.upper() not in result:
            result.append(cert.upper())
    return result


def validate_apk_bytes(raw: bytes) -> list[str]:
    """验证 APK 是 ZIP 且至少包含 AndroidManifest.xml。"""
    if not raw:
        raise ValueError("APK 文件为空")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 APK/ZIP 文件") from exc
    if "AndroidManifest.xml" not in names:
        raise ValueError("APK 缺少 AndroidManifest.xml")
    if not any(name == "classes.dex" or name.startswith("classes") and name.endswith(".dex") for name in names):
        raise ValueError("APK 缺少 classes.dex")
    return sorted(names)


def safe_apk_filename(value: Any) -> str:
    name = _text(value).replace("\\", "/").rsplit("/", 1)[-1]
    if not SAFE_FILENAME_RE.fullmatch(name):
        raise ValueError("APK 文件名只能包含字母、数字、点、下划线和短横线")
    return name


def build_manifest(
    *,
    channel: str,
    version_code: int,
    version_name: str,
    min_supported_version_code: int,
    force_update: bool,
    title: str,
    changelog: Any,
    file_name: str,
    apk_url: str,
    size_bytes: int,
    sha256: str,
    certificate_sha256: Any = None,
) -> dict[str, Any]:
    """组装客户端 ApkUpdateManager 兼容的完整清单。"""
    if channel not in CHANNELS:
        raise ValueError("版本渠道必须是 tv 或 mobile")
    if int(version_code) < 1:
        raise ValueError("versionCode 必须大于 0")
    if int(min_supported_version_code) < 0:
        raise ValueError("最低支持版本不能小于 0")
    if int(min_supported_version_code) > int(version_code):
        raise ValueError("最低支持版本不能高于发布版本")
    version_name = _text(version_name)
    if not version_name or len(version_name) > 40:
        raise ValueError("versionName 不能为空且最多 40 个字符")
    file_name = safe_apk_filename(file_name)
    apk_url = _text(apk_url)
    if not apk_url.startswith("https://"):
        raise ValueError("APK 下载地址必须使用 HTTPS")
    sha256 = _text(sha256).lower()
    if not HEX64_RE.fullmatch(sha256):
        raise ValueError("APK SHA-256 必须是 64 位十六进制")
    if int(size_bytes) < 1:
        raise ValueError("APK 文件大小无效")
    certs = normalize_certificates(certificate_sha256)
    if isinstance(changelog, str):
        changes = [_text(line) for line in changelog.splitlines() if _text(line)]
    else:
        changes = [_text(line) for line in (changelog or []) if _text(line)]
    if len(changes) > 30:
        raise ValueError("更新说明最多 30 条")
    if any(len(line) > 240 for line in changes):
        raise ValueError("单条更新说明最多 240 个字符")
    info = CHANNELS[channel]
    return {
        "schema": 1,
        "packageName": info["package_name"],
        "channel": "stable",
        "deviceType": info["device_type"],
        "versionCode": int(version_code),
        "versionName": version_name,
        "minSupportedVersionCode": int(min_supported_version_code),
        "forceUpdate": bool(force_update),
        "title": _text(title) or "发现新版本",
        "changelog": changes,
        "apk": {
            "fileName": file_name,
            "url": apk_url,
            "size": int(size_bytes),
            "sha256": sha256,
            "certificateSha256": certs,
        },
    }


def validate_manifest_for_channel(manifest: dict[str, Any], channel: str) -> None:
    """对已存清单做轻量校验，避免旧数据阻塞或污染新发布。"""
    if channel not in CHANNELS:
        raise ValueError("未知 APK 渠道")
    if manifest.get("packageName") != CHANNELS[channel]["package_name"]:
        raise ValueError("已有清单包名与渠道不匹配")
    if int(manifest.get("versionCode") or 0) < 1:
        raise ValueError("已有清单 versionCode 无效")
