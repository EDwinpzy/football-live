"""APK 应用内更新清单的纯校验与组装逻辑。

这个模块不访问网络，供后台上传接口和单元测试共同使用。APK 文件由后台写入自建对象存储；
客户端只读取这里生成的稳定 JSON 结构。

2026-09-21：对象桶单对象硬上限 100 MiB（平台级，随套餐固定），两端 APK 约 157 MiB。
因此清单支持**分片**——APK 拆成若干 < 100 MiB 的对象，客户端按序下载拼成一个文件，
再对整包做 SHA-256 与签名校验。`apk.sha256` / `apk.size` 始终是**整包**口径，
`apk.parts` 只描述下载方式；不认识分片的旧客户端会因 `apk.url` 为空而静默跳过。
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

# 单对象上限是 100 MiB；分片留余量，并限制段数避免清单膨胀。
PART_MAX_BYTES = 96 * 1024 * 1024
PART_MIN_BYTES = 1024 * 1024
PARTS_MAX = 16


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


def plan_parts(size_bytes: int, part_size: int) -> list[tuple[int, int]]:
    """按 part_size 切分整包，返回 [(起始偏移, 长度), ...]。

    分片是「顺序拼接」语义：各段长度之和等于整包大小，客户端按数组顺序写入同一文件。
    """
    size = int(size_bytes)
    if size < 1:
        raise ValueError("APK 文件大小无效")
    if not PART_MIN_BYTES <= int(part_size) <= PART_MAX_BYTES:
        raise ValueError(f"分片大小必须在 {PART_MIN_BYTES // 1048576}–{PART_MAX_BYTES // 1048576} MiB 之间")
    plan: list[tuple[int, int]] = []
    offset = 0
    while offset < size:
        plan.append((offset, min(int(part_size), size - offset)))
        offset += int(part_size)
    if len(plan) > PARTS_MAX:
        raise ValueError(f"分片数量超过 {PARTS_MAX} 段，请增大分片大小")
    return plan


def split_apk_parts(raw: bytes, part_size: int) -> list[bytes]:
    """按 plan_parts 的顺序切出各分片字节。"""
    return [raw[offset:offset + length] for offset, length in plan_parts(len(raw), part_size)]


def normalize_parts(value: Any, size_bytes: int) -> list[dict[str, Any]]:
    """校验并规范化分片描述（每条含 https url / size / sha256）。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("分片列表必须是数组")
    if len(value) > PARTS_MAX:
        raise ValueError(f"分片数量超过 {PARTS_MAX} 段")
    parts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("分片描述必须是对象")
        url = _text(item.get("url"))
        if not url.startswith("https://"):
            raise ValueError("分片下载地址必须使用 HTTPS")
        part_sha256 = _text(item.get("sha256")).lower()
        if not HEX64_RE.fullmatch(part_sha256):
            raise ValueError("分片 SHA-256 必须是 64 位十六进制")
        try:
            part_size = int(item.get("size"))
        except (TypeError, ValueError):
            raise ValueError("分片大小无效") from None
        if part_size < 1:
            raise ValueError("分片大小无效")
        parts.append({"url": url, "size": part_size, "sha256": part_sha256})
    if parts and sum(part["size"] for part in parts) != int(size_bytes):
        total = sum(part["size"] for part in parts)
        raise ValueError(f"分片总大小 {total} 与 APK 大小 {int(size_bytes)} 不一致")
    return parts


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
    parts: Any = None,
) -> dict[str, Any]:
    """组装客户端 ApkUpdateManager 兼容的完整清单。

    parts 非空即分片清单：此时 `apk.url` 允许为空，客户端改为按序下载各分片后校验整包。
    """
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
    if int(size_bytes) < 1:
        raise ValueError("APK 文件大小无效")
    apk_url = _text(apk_url)
    normalized_parts = normalize_parts(parts, size_bytes)
    if apk_url and not apk_url.startswith("https://"):
        raise ValueError("APK 下载地址必须使用 HTTPS")
    if not normalized_parts and not apk_url.startswith("https://"):
        raise ValueError("APK 下载地址必须使用 HTTPS")
    sha256 = _text(sha256).lower()
    if not HEX64_RE.fullmatch(sha256):
        raise ValueError("APK SHA-256 必须是 64 位十六进制")
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
        "schema": 2 if normalized_parts else 1,
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
            "parts": normalized_parts,
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
