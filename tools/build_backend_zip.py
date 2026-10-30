"""Rebuild the embedded TV/mobile backend and its native-only assets.

The archive contains the live Python runtime plus the football artwork and
logo used by both native clients.  The retired standalone Web UI is never
packaged: no HTML, application JavaScript, stylesheet, Web manifest or hashed
Web bundle belongs in this archive.

Usage: python tools/build_backend_zip.py
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

try:
    from .build_web_assets import build_assets
except ImportError:  # direct `python tools/build_backend_zip.py` invocation
    from build_web_assets import build_assets


ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "tools" / "backend-src"
SRC_ZIP = ROOT / "android" / "app" / "src" / "main" / "assets" / "backend" / "python-backend.zip"
MOBILE_ZIP = ROOT / "internal" / "mobile" / "app" / "src" / "main" / "assets" / "backend" / "python-backend.zip"
TMP_ZIP = ROOT / "tools" / "python-backend-new.zip"

# Embedded Python runtime: live API, HLS relay and team fallback artwork only.
# Keep this list aligned with tests/test_backend_zip_completeness.py.
REPL = ("proxy.py", "vlc_hls.py", "team_backdrop.py")
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
NATIVE_ASSET_PREFIX = "www/assets/"
NATIVE_MANIFEST = "www/manifest.json"


def write_entry(zout: zipfile.ZipFile, filename: str, data: bytes) -> None:
    info = zipfile.ZipInfo(filename, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    zout.writestr(info, data)


def atomic_copy_file(source: Path, destination: Path) -> None:
    """Replace a generated archive without truncating the current package."""
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                prefix=destination.name + ".",
                suffix=".tmp",
                dir=destination.parent,
                delete=False) as output:
            staged = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, destination)
        staged = None
    finally:
        if staged is not None and staged.exists():
            staged.unlink()


def build_archive(source_dir: Path, output_zip: Path) -> tuple[str, ...]:
    """Build a deterministic archive and return its sorted entry names."""
    source_dir = source_dir.resolve()
    output_zip = output_zip.resolve()
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    for name in REPL:
        if not (source_dir / name).is_file():
            raise FileNotFoundError(f"missing backend runtime: {source_dir / name}")

    with tempfile.TemporaryDirectory(prefix="qiuji-live-native-assets-") as build_temp:
        build_root = Path(build_temp)
        asset_root = build_root / "www"
        result = build_assets(source_dir / "www", asset_root)
        static_paths = [asset_root.joinpath(*name.split("/")) for name in result.copied_files]
        manifest_path = asset_root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("native asset manifest was not generated")

        staged_zip = build_root / output_zip.name
        with zipfile.ZipFile(staged_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in REPL:
                write_entry(archive, name, (source_dir / name).read_bytes())
            for name, path in zip(result.copied_files, static_paths):
                write_entry(archive, NATIVE_ASSET_PREFIX + name.removeprefix("assets/"), path.read_bytes())
            write_entry(archive, NATIVE_MANIFEST, manifest_path.read_bytes())

        with zipfile.ZipFile(staged_zip, "r") as archive:
            names = tuple(sorted(archive.namelist()))
        atomic_copy_file(staged_zip, output_zip)
    return names


def main() -> None:
    if TMP_ZIP.exists():
        TMP_ZIP.unlink()
    build_archive(SRC_DIR, TMP_ZIP)
    atomic_copy_file(TMP_ZIP, SRC_ZIP)
    atomic_copy_file(TMP_ZIP, MOBILE_ZIP)
    TMP_ZIP.unlink()
    print(f"TV 后端包：{SRC_ZIP} ({SRC_ZIP.stat().st_size} bytes)")
    print(f"移动版后端包：{MOBILE_ZIP} ({MOBILE_ZIP.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
