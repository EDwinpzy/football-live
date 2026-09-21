import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from apk_update import (  # noqa: E402
    PART_MAX_BYTES,
    build_manifest,
    normalize_certificates,
    plan_parts,
    split_apk_parts,
    validate_apk_bytes,
)


def apk_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"binary manifest")
        archive.writestr("classes.dex", b"dex")
    return out.getvalue()


def test_build_manifest_matches_android_client_shape():
    manifest = build_manifest(
        channel="tv",
        version_code=35,
        version_name="1.33",
        min_supported_version_code=34,
        force_update=True,
        title="发现新版本",
        changelog="修复播放\n优化启动",
        file_name="MyTV-TV版.apk",
        apk_url="https://example.test/apk-update/tv/v35/MyTV-TV.apk",
        size_bytes=123,
        sha256="a" * 64,
        certificate_sha256="AA:" + "BB" * 31,
    )
    assert manifest["packageName"] == "com.qiubo.optimaltv.public"
    assert manifest["versionCode"] == 35
    assert manifest["forceUpdate"] is True
    assert manifest["changelog"] == ["修复播放", "优化启动"]
    assert manifest["apk"]["sha256"] == "a" * 64
    assert manifest["apk"]["certificateSha256"] == ["AA" + "BB" * 31]


def test_validate_apk_requires_manifest_and_dex():
    assert "AndroidManifest.xml" in validate_apk_bytes(apk_bytes())
    with pytest.raises(ValueError, match="有效"):
        validate_apk_bytes(b"not-an-apk")


def test_certificate_normalization_rejects_bad_digest():
    assert normalize_certificates("aa" * 32) == ["AA" * 32]
    with pytest.raises(ValueError, match="64 位十六进制"):
        normalize_certificates("not-a-digest")


def test_manifest_rejects_non_https_and_invalid_floor():
    with pytest.raises(ValueError, match="HTTPS"):
        build_manifest(
            channel="mobile", version_code=35, version_name="1.33",
            min_supported_version_code=34, force_update=False, title="",
            changelog=[], file_name="MyTV.apk", apk_url="http://example.test/a.apk",
            size_bytes=1, sha256="b" * 64,
        )
    with pytest.raises(ValueError, match="最低支持版本"):
        build_manifest(
            channel="mobile", version_code=35, version_name="1.33",
            min_supported_version_code=36, force_update=False, title="",
            changelog=[], file_name="MyTV.apk", apk_url="https://example.test/a.apk",
            size_bytes=1, sha256="b" * 64,
        )


def test_plan_parts_covers_whole_file_in_order():
    # 157.6 MiB 包按 96 MiB 拆段：2 段，末段收尾，长度之和等于整包
    size = 165_283_200
    plan = plan_parts(size, PART_MAX_BYTES)
    assert [length for _, length in plan] == [PART_MAX_BYTES, size - PART_MAX_BYTES]
    assert [offset for offset, _ in plan] == [0, PART_MAX_BYTES]
    assert sum(length for _, length in plan) == size

    raw = bytes(range(256)) * 12288  # 3 MiB
    blobs = split_apk_parts(raw, 1024 * 1024)
    assert b"".join(blobs) == raw
    assert len(blobs) == 3


def test_plan_parts_rejects_bad_part_size_and_too_many_parts():
    with pytest.raises(ValueError, match="分片大小"):
        plan_parts(10, 512)
    with pytest.raises(ValueError, match="分片大小"):
        plan_parts(10, PART_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="APK 文件大小无效"):
        plan_parts(0, PART_MAX_BYTES)
    # 段数上限保护清单体积
    with pytest.raises(ValueError, match="分片数量超过"):
        plan_parts(PART_MAX_BYTES * 17, PART_MAX_BYTES)


def part(url: str, size: int, digest: str = "c" * 64) -> dict:
    return {"url": url, "size": size, "sha256": digest}


def test_manifest_with_parts_uses_sharded_schema():
    manifest = build_manifest(
        channel="tv", version_code=36, version_name="1.34",
        min_supported_version_code=0, force_update=False, title="发现新版本",
        changelog=[], file_name="MyTV-TV版.apk",
        apk_url="", size_bytes=100,
        sha256="a" * 64, certificate_sha256="aa" * 32,
        parts=[part("https://bucket.test/parts/part-01-of-02", 60),
               part("https://bucket.test/parts/part-02-of-02", 40, "d" * 64)],
    )
    assert manifest["schema"] == 2
    assert manifest["apk"]["url"] == ""
    assert [p["size"] for p in manifest["apk"]["parts"]] == [60, 40]
    # 整包口径不变：客户端仍按 apk.size/sha256 校验拼好的文件
    assert manifest["apk"]["size"] == 100
    assert manifest["apk"]["sha256"] == "a" * 64


def test_single_object_manifest_keeps_schema_one_and_empty_parts():
    manifest = build_manifest(
        channel="mobile", version_code=35, version_name="1.33",
        min_supported_version_code=0, force_update=False, title="",
        changelog=[], file_name="MyTV.apk",
        apk_url="https://example.test/a.apk", size_bytes=10, sha256="b" * 64,
    )
    assert manifest["schema"] == 1
    assert manifest["apk"]["parts"] == []


def test_manifest_rejects_inconsistent_or_insecure_parts():
    base = dict(
        channel="tv", version_code=36, version_name="1.34",
        min_supported_version_code=0, force_update=False, title="",
        changelog=[], file_name="MyTV-TV版.apk",
        size_bytes=100, sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="分片总大小"):
        build_manifest(apk_url="", parts=[part("https://bucket.test/p1", 60)], **base)
    with pytest.raises(ValueError, match="分片下载地址"):
        build_manifest(apk_url="", parts=[part("http://bucket.test/p1", 100)], **base)
    with pytest.raises(ValueError, match="分片 SHA-256"):
        build_manifest(apk_url="", parts=[{"url": "https://bucket.test/p1", "size": 100, "sha256": "zz"}], **base)
    with pytest.raises(ValueError, match="分片大小无效"):
        build_manifest(apk_url="", parts=[part("https://bucket.test/p1", 0)], **base)
    # 没有分片时仍然必须有 HTTPS 单对象地址
    with pytest.raises(ValueError, match="HTTPS"):
        build_manifest(apk_url="", parts=[], **base)
