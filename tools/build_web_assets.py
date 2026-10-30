"""Stage deterministic assets required by the TV and mobile clients.

This is intentionally not a Web application build.  The native clients fetch
only the football artwork and logo through their embedded backend; standalone
HTML, JavaScript, CSS, service-worker manifest and hashed Web bundles must not
be produced or copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


IMAGE_CACHE_CONTROL = "public, max-age=86400"
REQUIRED_FOOTBALL_POSTERS = tuple(f"fb-{index:02d}.jpg" for index in range(1, 37))
_LEGACY_WEB_ROOT_FILES = (
    "index.html",
    "app.js",
    "style.css",
    "manifest.webmanifest",
    "asset-manifest.json",
)


@dataclass(frozen=True)
class BuildResult:
    copied_files: tuple[str, ...]
    copied_metadata: tuple[dict[str, Any], ...]


def _safe_relative(path: Path, root: Path) -> str:
    relative = PurePosixPath(path.relative_to(root).as_posix())
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe asset path: {relative}")
    return str(relative)


def _remove_legacy_web_output(output: Path) -> None:
    """Remove outputs from the retired standalone Web UI build."""
    for name in _LEGACY_WEB_ROOT_FILES:
        target = output / name
        if target.is_file() or target.is_symlink():
            target.unlink()
    web_manifest = output / "manifest.json"
    if web_manifest.is_file() or web_manifest.is_symlink():
        web_manifest.unlink()


def build_assets(source_dir: str | Path, output_dir: str | Path) -> BuildResult:
    """Copy only the native compatibility asset tree into ``output_dir``."""
    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("source_dir and output_dir must not overlap")

    assets_root = source / "assets"
    football_root = assets_root / "football"
    if not assets_root.is_dir():
        raise FileNotFoundError(f"missing native asset directory: {assets_root}")
    missing_posters = [
        name for name in REQUIRED_FOOTBALL_POSTERS if not (football_root / name).is_file()
    ]
    if missing_posters:
        raise FileNotFoundError(
            "missing football posters: " + ", ".join(missing_posters)
        )

    output.mkdir(parents=True, exist_ok=True)
    _remove_legacy_web_output(output)

    source_files = sorted(path for path in assets_root.rglob("*") if path.is_file())
    copied_files: list[str] = []
    copied_metadata: list[dict[str, Any]] = []
    output_assets = output / "assets"
    output_assets.mkdir(parents=True, exist_ok=True)

    wanted = {_safe_relative(path, source) for path in source_files}
    for existing in sorted(output_assets.rglob("*"), reverse=True):
        if existing.is_file() or existing.is_symlink():
            relative = _safe_relative(existing, output)
            if relative not in wanted:
                existing.unlink()
        elif existing.is_dir():
            try:
                existing.rmdir()
            except OSError:
                pass

    for asset in source_files:
        relative = _safe_relative(asset, source)
        target = output.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, target)
        data = target.read_bytes()
        copied_files.append(relative)
        copied_metadata.append({
            "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })

    manifest = {
        "format": 2,
        "kind": "native-compatibility-assets",
        "copiedFiles": copied_files,
        "copiedAssetMeta": copied_metadata,
        "cacheControl": {
            f"/{relative}": IMAGE_CACHE_CONTROL for relative in copied_files
        },
        "defaultCacheControl": IMAGE_CACHE_CONTROL,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return BuildResult(tuple(copied_files), tuple(copied_metadata))


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=root / "tools" / "backend-src" / "www")
    parser.add_argument("output", nargs="?", type=Path, default=root / "tools" / "web-dist")
    args = parser.parse_args()
    result = build_assets(args.source, args.output)
    print(f"已同步原生兼容资源 {len(result.copied_files)} 个")
    print(f"输出目录：{args.output.resolve()}")


if __name__ == "__main__":
    main()
