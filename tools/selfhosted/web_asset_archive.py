"""Keep the current and immediately previous immutable Web bundles on disk."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


_ASSET_NAMES = {
    "app": re.compile(r"^app\.[0-9a-f]{16}\.js$"),
    "style": re.compile(r"^style\.[0-9a-f]{16}\.css$"),
}
_BUILD_ID = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_NAME = "manifest.json"


def _fail(message: str) -> ValueError:
    return ValueError(f"Web asset archive integrity error: {message}")


def _load_bundle(web_root: Path) -> tuple[bytes, dict[str, bytes]]:
    manifest_path = web_root / _MANIFEST_NAME
    index_path = web_root / "index.html"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        index_html = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(f"cannot read generated index/manifest: {exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise _fail("unsupported or malformed manifest")
    referenced = manifest.get("referencedFiles")
    if not isinstance(referenced, list) or len(referenced) != 2 or not all(
        isinstance(value, str) for value in referenced
    ):
        raise _fail("manifest must reference exactly the generated JS and CSS bundles")

    by_kind: dict[str, str] = {}
    for value in referenced:
        relative = PurePosixPath(value)
        if relative.is_absolute() or relative.parts[0:1] != ("assets",) or len(relative.parts) != 2:
            raise _fail(f"unsafe bundle path: {value!r}")
        for kind, pattern in _ASSET_NAMES.items():
            if pattern.fullmatch(relative.name):
                if kind in by_kind:
                    raise _fail(f"duplicate {kind} bundle reference")
                by_kind[kind] = value
                break
        else:
            raise _fail(f"unexpected bundle filename: {value!r}")
    if set(by_kind) != {"app", "style"}:
        raise _fail("manifest does not reference one JS bundle and one CSS bundle")
    if f'src="{by_kind["app"]}"' not in index_html:
        raise _fail("index.html does not reference the manifest JS bundle")
    if f'href="{by_kind["style"]}"' not in index_html:
        raise _fail("index.html does not reference the manifest CSS bundle")

    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) != 2:
        raise _fail("manifest asset metadata is incomplete")
    metadata: dict[str, dict[str, Any]] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            raise _fail("manifest asset metadata is malformed")
        asset_path = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(asset_path, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise _fail("manifest asset hash or size is malformed")
        metadata[asset_path] = {"sha256": digest, "size": size}
    expected_paths = {f"/{value}" for value in referenced}
    if set(metadata) != expected_paths:
        raise _fail("manifest hashes do not match the referenced bundles")

    files: dict[str, bytes] = {}
    for relative in referenced:
        descriptor = metadata[f"/{relative}"]
        source_path = web_root.joinpath(*PurePosixPath(relative).parts)
        if source_path.is_symlink() or not source_path.is_file():
            raise _fail(f"bundle file is missing or is a symlink: {relative}")
        data = source_path.read_bytes()
        if len(data) != descriptor["size"] or hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
            raise _fail(f"bundle bytes do not match manifest: {relative}")
        files[relative] = data
    return manifest_bytes, files


def _write_new_build(builds_dir: Path, build_id: str, manifest_bytes: bytes, files: dict[str, bytes]) -> Path:
    final_dir = builds_dir / build_id
    if final_dir.exists():
        if final_dir.is_symlink() or not final_dir.is_dir():
            raise _fail(f"archive entry is not a directory: {build_id}")
        _validate_archived_build(final_dir, manifest_bytes, files)
        return final_dir

    temporary_dir = Path(tempfile.mkdtemp(prefix=".tmp-", dir=builds_dir))
    try:
        (temporary_dir / _MANIFEST_NAME).write_bytes(manifest_bytes)
        for relative, data in files.items():
            target = temporary_dir.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _validate_archived_build(temporary_dir, manifest_bytes, files)
        os.replace(temporary_dir, final_dir)
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    return final_dir


def _validate_archived_build(build_dir: Path, manifest_bytes: bytes, expected_files: dict[str, bytes] | None = None) -> None:
    manifest_path = build_dir / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.read_bytes() != manifest_bytes:
        raise _fail(f"archived manifest is missing or changed: {build_dir.name}")
    _, actual_files = _load_bundle_from_archive(build_dir)
    if expected_files is not None and actual_files != expected_files:
        raise _fail(f"archived bundles differ from image: {build_dir.name}")


def _load_bundle_from_archive(build_dir: Path) -> tuple[bytes, dict[str, bytes]]:
    try:
        manifest_bytes = (build_dir / _MANIFEST_NAME).read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail(f"cannot read archived manifest in {build_dir.name}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise _fail(f"unsupported archived manifest in {build_dir.name}")
    references = manifest.get("referencedFiles")
    metadata = manifest.get("assets")
    if not isinstance(references, list) or len(references) != 2 or not isinstance(metadata, list):
        raise _fail(f"archived manifest is incomplete: {build_dir.name}")
    descriptors = {item.get("path"): item for item in metadata if isinstance(item, dict)}
    if len(descriptors) != 2 or set(descriptors) != {f"/{path}" for path in references}:
        raise _fail(f"archived manifest asset paths are invalid: {build_dir.name}")

    files: dict[str, bytes] = {}
    kinds: set[str] = set()
    for relative in references:
        if not isinstance(relative, str):
            raise _fail(f"archived manifest path is malformed: {build_dir.name}")
        posix_path = PurePosixPath(relative)
        if posix_path.is_absolute() or posix_path.parts[0:1] != ("assets",) or len(posix_path.parts) != 2:
            raise _fail(f"archived asset path is unsafe: {relative!r}")
        matching_kinds = {
            kind for kind, pattern in _ASSET_NAMES.items() if pattern.fullmatch(posix_path.name)
        }
        if len(matching_kinds) != 1 or matching_kinds & kinds:
            raise _fail(f"archived asset filename is invalid: {relative!r}")
        kinds.update(matching_kinds)
        descriptor = descriptors[f"/{relative}"]
        digest = descriptor.get("sha256")
        size = descriptor.get("size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise _fail(f"archived asset hash is invalid: {relative!r}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise _fail(f"archived asset size is invalid: {relative!r}")
        asset_path = build_dir.joinpath(*posix_path.parts)
        if asset_path.is_symlink() or not asset_path.is_file():
            raise _fail(f"archived asset is missing or is a symlink: {relative}")
        data = asset_path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise _fail(f"archived asset bytes do not match manifest: {relative}")
        files[relative] = data
    if kinds != {"app", "style"}:
        raise _fail(f"archived build must contain one JS and one CSS bundle: {build_dir.name}")
    return manifest_bytes, files


def _prune_builds(builds_dir: Path, current_build_id: str, keep_builds: int) -> list[Path]:
    for temporary in builds_dir.iterdir():
        if temporary.name.startswith(".tmp-") and temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary)

    candidates = [
        path
        for path in builds_dir.iterdir()
        if _BUILD_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
    ]
    current = builds_dir / current_build_id
    if current not in candidates:
        raise _fail("current bundle was not durably archived")
    ordered_previous = sorted(
        (path for path in candidates if path != current),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    # A retained generation can be damaged in the persistent volume (partial
    # write, disk error, out-of-band edit). It then used to raise from
    # _materialize on *every* boot, so the container crash-looped and nginx
    # never passed its `service_healthy` gate - an outage only an SSH session
    # could clear. Validate each historical generation and discard the damaged
    # ones until `keep_builds - 1` healthy previous builds remain. The current
    # bundle stays fail-fast: that is an image build problem and must surface.
    previous = []
    discarded = set()
    for candidate in ordered_previous:
        try:
            _load_bundle_from_archive(candidate)
        except ValueError:
            shutil.rmtree(candidate, ignore_errors=True)
            discarded.add(candidate.name)
            continue
        previous.append(candidate)
        if len(previous) >= keep_builds - 1:
            break
    retained = [current, *previous]
    retained_names = {path.name for path in retained}
    for candidate in candidates:
        if candidate.name not in retained_names and candidate.name not in discarded:
            shutil.rmtree(candidate, ignore_errors=True)
    return retained


def _materialize(retained: list[Path], web_root: Path) -> None:
    retained_files: set[str] = set()
    for build_dir in retained:
        _, files = _load_bundle_from_archive(build_dir)
        for relative, data in files.items():
            retained_files.add(relative)
            destination = web_root.joinpath(*PurePosixPath(relative).parts)
            if destination.exists():
                if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != data:
                    raise _fail(f"immutable URL already contains different bytes: {relative}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    assets_root = web_root / "assets"
    if assets_root.is_symlink():
        raise _fail("Web assets directory is a symlink")
    if assets_root.is_dir():
        stale_temp = re.compile(r"^\.(?:app\.[0-9a-f]{16}\.js|style\.[0-9a-f]{16}\.css)\.tmp-\d+$")
        for entry in assets_root.iterdir():
            if entry.is_file() or entry.is_symlink():
                if stale_temp.fullmatch(entry.name):
                    entry.unlink(missing_ok=True)
                elif any(pattern.fullmatch(entry.name) for pattern in _ASSET_NAMES.values()):
                    relative = f"assets/{entry.name}"
                    if relative not in retained_files:
                        entry.unlink(missing_ok=True)


def prepare_web_assets(
    web_root: str | Path = "/app/www",
    archive_root: str | Path = "/var/lib/mytv/web-assets",
    *,
    keep_builds: int = 2,
) -> tuple[str, ...]:
    """Archive the image bundle, restore one prior bundle, then return ready.

    The Web Compose service is intentionally a single replica. The persistent
    archive is a bind mount and must survive image/container replacement. The
    entrypoint calls this function before starting the HTTP server, so an
    incomplete archive prevents the health endpoint from becoming available.
    """
    if keep_builds < 2:
        raise ValueError("keep_builds must retain the current and previous build")
    web = Path(web_root).resolve()
    archive = Path(archive_root).resolve()
    if not web.is_dir():
        raise _fail(f"Web root does not exist: {web}")

    manifest_bytes, current_files = _load_bundle(web)
    build_id = hashlib.sha256(manifest_bytes).hexdigest()[:32]
    builds_dir = archive / "builds"
    _prepare_builds_dir(builds_dir)
    current_build = _write_new_build(builds_dir, build_id, manifest_bytes, current_files)
    os.utime(current_build, None)
    retained = _prune_builds(builds_dir, build_id, keep_builds)
    _materialize(retained, web)
    return tuple(sorted({relative for build in retained for relative in _load_bundle_from_archive(build)[1]}))


def _prepare_builds_dir(builds_dir: Path) -> None:
    """Ensure the archive can actually be written, with an operator-readable error.

    ``mkdir(exist_ok=True)`` only raises when the directory is missing, so a
    read-only bind mount, a wrong SELinux label or a path that is a plain file
    slipped through as a bare PermissionError/FileExistsError from a later
    ``mkdtemp``. Those surface during startup, before the health port opens,
    where the raw errno tells the operator nothing about which volume to fix.
    """
    try:
        builds_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail(f"Web asset archive directory is not writable: {builds_dir} ({exc})") from exc
    if builds_dir.is_symlink() or not builds_dir.is_dir():
        raise _fail(f"Web asset archive directory is not writable: {builds_dir} is not a directory")
    if not os.access(builds_dir, os.W_OK | os.X_OK):
        raise _fail(f"Web asset archive directory is not writable: {builds_dir}")
