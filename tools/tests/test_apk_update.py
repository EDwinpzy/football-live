import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from apk_update import build_manifest, normalize_certificates, validate_apk_bytes  # noqa: E402


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
