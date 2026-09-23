#!/usr/bin/env python3
"""发布 APK 应用内更新清单（本机直传对象桶，不经云函数）。

为什么不能走后台接口：后台的 apk_update_commit 要在**一次请求**里把全部分片读回内存、
再整体回传对象桶。云函数 60s 超时下这个动作对 >100MB 的 APK 必然失败——2026-09-21
实测 165MB 的 TV 包：40 个分片全部上传成功，commit 被杀，APK 对象与清单都没落地，
分片残留在 apk-update/uploads/ 下。这也解释了为什么「APK 应用内更新」功能做完之后
线上 apk-update/*.json 一直是 404：清单从来没成功发布过。

本脚本用与 release_all.py / db_import.py 相同的 svc_import 凭据直传同一个桶，
客户端下载地址与清单结构完全不变。

用法：
  python tools/publish_apk_update.py --channel tv \
    --apk "apk/TV版/MyTV-TV版.apk" \
    --version-code 35 --version-name 1.33 \
    --cert 601F0D63...BA43 \
    --title "发现新版本" --note "..." --note "..." \
    [--offline] [--force] [--min-supported 0] [--dry-run]

签名证书摘要必须与被发布 APK 的真实签名一致，否则客户端下载后校验失败、或系统拒绝覆盖：
  apksigner verify --print-certs <apk>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend-src"))

import db_import  # noqa: E402
from apk_update import (  # noqa: E402
    PART_MAX_BYTES,
    build_manifest,
    normalize_certificates,
    plan_parts,
    safe_apk_filename,
    split_apk_parts,
    validate_apk_bytes,
)

BUCKET = "hotupdate"
PREFIX = "apk-update"
def obj_url(key: str) -> str:
    """Public download URL written into the manifest."""
    return db_import.ENDPOINTS.public_object_url(BUCKET, key)


def upload_object_url(key: str) -> str:
    """Private storage API URL used by the publisher for uploads and reads."""
    return db_import.ENDPOINTS.storage_object_url(key)


def manifest_url(manifest_name: str) -> str:
    if manifest_name in ("tv", "mobile"):
        return db_import.ENDPOINTS.public_web_url(f"apk-update/{manifest_name}.json")
    return obj_url(f"{PREFIX}/{manifest_name}.json")


def put_object(key: str, raw: bytes, ctype: str, token: str) -> None:
    url = upload_object_url(key) + "?x-upsert=true"
    db_import.check_url(url)
    req = urllib.request.Request(url, data=raw, method=db_import.ENDPOINTS.storage_upload_method, headers={
        "Content-Type": ctype,
        "Authorization": "Bearer " + token,
        "x-upsert": "true",
    })
    with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=900) as r:
        r.read()


def get_object(key: str) -> bytes | None:
    url = upload_object_url(key)
    db_import.check_url(url)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.build_opener(db_import.NoRedirect).open(req, timeout=120) as r:
            return r.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description="发布 APK 应用内更新清单（直传对象桶）")
    ap.add_argument("--channel", required=True, choices=["tv", "mobile"])
    ap.add_argument("--apk", required=True, help="APK 文件路径")
    ap.add_argument("--version-code", required=True, type=int)
    ap.add_argument("--version-name", required=True)
    ap.add_argument("--cert", required=True, help="签名证书 SHA-256（可逗号分隔多条）")
    ap.add_argument("--min-supported", type=int, default=0)
    ap.add_argument("--title", default="发现新版本")
    ap.add_argument("--note", action="append", default=[], help="更新说明，可重复")
    ap.add_argument("--force", action="store_true", help="强制更新")
    ap.add_argument("--offline", action="store_true", help="只上传 APK，不写公开清单")
    ap.add_argument("--file-name", default="", help="对象里的文件名，默认取 APK 原名")
    ap.add_argument("--manifest-name", default="", help="清单对象名（默认取渠道名）；联调可用 test-tv 走独立路径")
    ap.add_argument("--part-size-mb", type=int, default=96,
                    help=f"单分片大小（MiB，1–{PART_MAX_BYTES // 1048576}）；APK 超过该值即分片发布")
    ap.add_argument("--dry-run", action="store_true", help="只校验并打印，不写桶")
    args = ap.parse_args()

    apk_path = pathlib.Path(args.apk)
    if not apk_path.is_file():
        sys.exit(f"找不到 APK：{apk_path}")
    raw = apk_path.read_bytes()
    validate_apk_bytes(raw)
    sha256 = hashlib.sha256(raw).hexdigest()
    file_name = safe_apk_filename(args.file_name or apk_path.name)
    certs = normalize_certificates(args.cert)

    # 对象桶单对象上限 100 MiB：包体超过分片阈值就拆成多段，客户端按序拼接后校验整包。
    try:
        plan = plan_parts(len(raw), args.part_size_mb * 1048576)
    except ValueError as exc:
        sys.exit(str(exc))
    part_blobs = split_apk_parts(raw, args.part_size_mb * 1048576) if len(plan) > 1 else []
    key_apk = f"{PREFIX}/{args.channel}/v{args.version_code}/{file_name}"
    part_keys = [
        f"{PREFIX}/{args.channel}/v{args.version_code}/parts/part-{i + 1:02d}-of-{len(plan):02d}"
        for i in range(len(plan))
    ]
    parts_desc = [
        {"url": obj_url(key), "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
        for key, blob in zip(part_keys, part_blobs)
    ]
    manifest = build_manifest(
        channel=args.channel,
        version_code=args.version_code,
        version_name=args.version_name,
        min_supported_version_code=args.min_supported,
        force_update=args.force,
        title=args.title,
        changelog=args.note,
        file_name=file_name,
        apk_url="" if parts_desc else obj_url(key_apk),
        size_bytes=len(raw),
        sha256=sha256,
        certificate_sha256=certs,
        parts=parts_desc,
    )

    manifest_name = args.manifest_name or args.channel
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,60}", manifest_name):
        sys.exit("--manifest-name 只能用小写字母、数字、点、下划线、短横线")
    key_manifest = f"{PREFIX}/{manifest_name}.json"
    current_raw = get_object(key_manifest)
    if current_raw:
        current = json.loads(current_raw.decode("utf-8"))
        current_code = int(current.get("versionCode") or 0)
        if not args.offline and args.version_code <= current_code:
            sys.exit(f"versionCode 必须大于当前已发布版本 v{current_code}（当前清单仍生效）")
        print(f"当前公开版本 v{current_code}，本次 v{args.version_code}")

    print(f"APK {file_name} · {len(raw) / 1048576:.1f} MB · sha256 {sha256}")
    print(f"签名证书 {certs[0]}")
    if parts_desc:
        print(f"分片 {len(parts_desc)} 段（单段 {args.part_size_mb} MiB 上限）")
        for desc in parts_desc:
            print(f"  {desc['size'] / 1048576:7.1f} MB  {desc['url']}")
    else:
        print(f"对象 {key_apk}")
    print(f"清单 {key_manifest} -> {obj_url(key_manifest)}")
    if args.dry_run:
        print("[dry-run] 未写入任何对象")
        return

    token = db_import.signin()
    if parts_desc:
        for index, (key, blob) in enumerate(zip(part_keys, part_blobs), start=1):
            print(f"上传分片 {index}/{len(part_blobs)}…")
            put_object(key, blob, "application/octet-stream", token)
    else:
        print("上传 APK 对象…")
        put_object(key_apk, raw, "application/vnd.android.package-archive", token)

    if args.offline:
        print("已上传（下线态，未写公开清单）")
    else:
        manifest_raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), indent=2).encode("utf-8")
        put_object(key_manifest, manifest_raw, "application/json; charset=utf-8", token)
        print("已写入公开清单")

    print(json.dumps({
        "channel": args.channel,
        "versionCode": args.version_code,
        "versionName": args.version_name,
        "size": len(raw),
        "sha256": sha256,
        "parts": len(parts_desc),
        "apkUrl": obj_url(key_apk) if not parts_desc else "",
        "manifestUrl": manifest_url(manifest_name),
        "status": "offline" if args.offline else "published",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
