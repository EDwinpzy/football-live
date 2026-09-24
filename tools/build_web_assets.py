"""Build a deterministic, content-addressed bundle for the My TV Web client."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
DEFAULT_CACHE_CONTROL = "public, max-age=300"
_HASHED_ASSET_RE = re.compile(r"^(?:app\.[0-9a-f]{16}\.js|style\.[0-9a-f]{16}\.css)$")


@dataclass(frozen=True)
class BuildResult:
    manifest: dict[str, Any]
    referenced_files: tuple[str, ...]


def _rewrite_single_reference(html: str, pattern: str, replacement: str, description: str) -> str:
    rewritten, count = re.subn(
        pattern,
        lambda match: match.group(1) + replacement + match.group(2),
        html,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError(f"index.html must reference exactly one {description}; found {count}")
    return rewritten


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _manifest_files(manifest: dict[str, Any], key: str) -> set[str]:
    files = manifest.get(key, [])
    if not isinstance(files, list):
        return set()
    return {item for item in files if isinstance(item, str)}


def _output_file(output: Path, relative: str) -> Path | None:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    candidate = output.joinpath(*path.parts)
    return candidate if candidate.resolve().is_relative_to(output) else None


def build_assets(source_dir: str | Path, output_dir: str | Path) -> BuildResult:
    """Build an index and content-hashed JS/CSS files into ``output_dir``.

    The output is intentionally retained between builds: current URLs and the
    prior build's JS/CSS URLs coexist so a browser with an already-open page can
    finish loading its previous bundle. Older generated bundles are pruned on
    the next build.
    """
    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("source_dir and output_dir must not overlap")

    required = {name: source / name for name in ("index.html", "app.js", "style.css")}
    missing = [path.name for path in required.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing Web source files: {', '.join(missing)}")

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    previous_manifest = _read_manifest(manifest_path)
    previous_refs = _manifest_files(previous_manifest, "referencedFiles")
    previous_copies = _manifest_files(previous_manifest, "copiedFiles")

    source_bytes = {
        name: required[name].read_bytes()
        for name in ("app.js", "style.css")
    }
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
    names = {
        "app.js": f"app.{hashes['app.js'][:16]}.js",
        "style.css": f"style.{hashes['style.css'][:16]}.css",
    }
    referenced_files = tuple(f"assets/{names[name]}" for name in ("app.js", "style.css"))

    html = required["index.html"].read_text(encoding="utf-8")
    html = _rewrite_single_reference(
        html,
        r'(<link\b[^>]*\bhref=["\'])(?:\./)?style\.css(?:\?[^"\']*)?(["\'])',
        referenced_files[1],
        "stylesheet",
    )
    html = _rewrite_single_reference(
        html,
        r'(<script\b[^>]*\bsrc=["\'])(?:\./)?app\.js(?:\?[^"\']*)?(["\'])',
        referenced_files[0],
        "application script",
    )

    source_assets = source / "assets"
    copied_files = []
    if source_assets.is_dir():
        for asset in sorted(path for path in source_assets.rglob("*") if path.is_file()):
            relative = f"assets/{asset.relative_to(source_assets).as_posix()}"
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(asset, target)
            copied_files.append(relative)
    web_manifest = source / "manifest.webmanifest"
    if web_manifest.is_file():
        shutil.copyfile(web_manifest, output / "manifest.webmanifest")
        copied_files.append("manifest.webmanifest")

    (output / "index.html").write_text(html, encoding="utf-8", newline="\n")
    for source_name, relative_name in zip(("app.js", "style.css"), referenced_files):
        target = output / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source_bytes[source_name])

    current_refs = set(referenced_files)
    current_copies = set(copied_files)
    for old_copy in previous_copies - current_copies - current_refs - previous_refs:
        target = _output_file(output, old_copy)
        if target is not None:
            target.unlink(missing_ok=True)

    keep_refs = previous_refs | current_refs
    output_assets = output / "assets"
    for old in output_assets.rglob("*") if output_assets.is_dir() else ():
        if old.is_file() and _HASHED_ASSET_RE.fullmatch(old.name):
            relative = old.relative_to(output).as_posix()
            if relative not in keep_refs and relative not in current_copies:
                old.unlink()

    cache_control = {
        "/": "no-store",
        "/index.html": "no-store",
        **{f"/{path}": IMMUTABLE_CACHE_CONTROL for path in referenced_files},
    }
    manifest = {
        "format": 1,
        "referencedFiles": list(referenced_files),
        "copiedFiles": sorted(copied_files),
        "assets": [
            {
                "path": f"/{path}",
                "sha256": hashes[source_name],
                "size": len(source_bytes[source_name]),
                "cacheControl": IMMUTABLE_CACHE_CONTROL,
            }
            for source_name, path in zip(("app.js", "style.css"), referenced_files)
        ],
        "cacheControl": cache_control,
        "defaultCacheControl": DEFAULT_CACHE_CONTROL,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "asset-manifest.json").unlink(missing_ok=True)
    return BuildResult(manifest=manifest, referenced_files=referenced_files)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=root / "tools" / "backend-src" / "www")
    parser.add_argument("output", nargs="?", type=Path, default=root / "tools" / "web-dist")
    args = parser.parse_args()
    result = build_assets(args.source, args.output)
    print(f"已生成 Web 资源 {len(result.referenced_files)} 个：{', '.join(result.referenced_files)}")
    print(f"输出目录：{args.output.resolve()}")


if __name__ == "__main__":
    main()
