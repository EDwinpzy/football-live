#!/usr/bin/env python3
"""Stage only My TV's CloudBase Storage objects for a self-hosted migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Mapping


SOURCE_BUCKET = "hotupdate"
ROOT = Path(__file__).resolve().parents[2]


def _tcb_command_prefix() -> list[str]:
    if os.name == "nt":
        app_data = Path(os.environ.get("APPDATA", ""))
        cli_entry = app_data / "npm" / "node_modules" / "@cloudbase" / "cli" / "bin" / "tcb"
        node = shutil.which("node")
        if cli_entry.is_file() and node:
            return [node, str(cli_entry)]
        return ["tcb.cmd"]
    return ["tcb"]


def select_mytv_objects(objects: list[Mapping[str, object]]) -> tuple[list[dict[str, object]], int]:
    """Select explicitly My TV-owned storage keys, excluding all other prefixes."""
    selected: list[dict[str, object]] = []
    excluded = 0
    seen: set[str] = set()
    for item in objects:
        key = item.get("name")
        if not isinstance(key, str):
            raise ValueError("invalid object key")
        mytv_key = key.startswith(("apk-update/", "iptv/")) or bool(
            re.fullmatch(r"backend-hu[0-9]+\.zip", key)
        )
        if not mytv_key:
            excluded += 1
            continue
        path = PurePosixPath(key)
        if (
            not key
            or key.startswith("/")
            or "\\" in key
            or any(part in ("", ".", "..") for part in key.split("/"))
            or path.is_absolute()
        ):
            raise ValueError("unsafe object key")
        if key in seen:
            raise ValueError("duplicate object key")
        seen.add(key)
        metadata = item.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("invalid object metadata")
        size = metadata.get("size", metadata.get("contentLength"))
        if isinstance(size, bool) or not isinstance(size, (int, str)):
            raise ValueError("invalid object size")
        try:
            size = int(size)
        except ValueError as exc:
            raise ValueError("invalid object size") from exc
        if size < 0:
            raise ValueError("invalid object size")
        selected.append(
            {
                "key": key,
                "size": size,
                "content_type": metadata.get("mimetype"),
                "cache_control": metadata.get("cacheControl"),
            }
        )
    return selected, excluded


def _parse_json_output(output: str) -> dict[str, object]:
    start = output.find("{")
    if start < 0:
        raise ValueError("CloudBase CLI returned invalid JSON")
    result = json.loads(output[start:])
    if not isinstance(result, dict):
        raise ValueError("CloudBase CLI returned invalid JSON")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_mytv_key(key: str) -> bool:
    return key.startswith(("apk-update/", "iptv/")) or bool(
        re.fullmatch(r"backend-hu[0-9]+\.zip", key)
    )


def _validate_mytv_key(key: object) -> str:
    """Require a relative, unencoded object key within My TV's storage scope."""
    if not isinstance(key, str) or not _is_mytv_key(key):
        raise ValueError("object key is outside My TV scope")
    if (
        key.startswith("/")
        or "\\" in key
        or "%" in key
        or any(part in ("", ".", "..") for part in key.split("/"))
        or any(ord(char) < 32 or char in '<>:"|?*' for char in key)
    ):
        raise ValueError("unsafe object key")
    return key


def reconcile_storage_manifest(
    source_manifest: Mapping[str, object], target_root: str | Path
) -> dict[str, object]:
    """Compare staged My TV metadata with the target hotupdate object root.

    The result contains aggregate counts and only mismatched keys; it never
    includes local paths, source environment identifiers, or object contents.
    Target files outside My TV's key allowlist are ignored.
    """
    if source_manifest.get("bucket") != SOURCE_BUCKET:
        raise ValueError("source manifest must describe the hotupdate bucket")
    files = source_manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("invalid source manifest files")

    expected: dict[str, tuple[int, str]] = {}
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("invalid source manifest file")
        key = _validate_mytv_key(item.get("key"))
        size = item.get("size")
        digest = item.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("invalid source object size")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ValueError("invalid source object sha256")
        if key in expected:
            raise ValueError("duplicate source object key")
        expected[key] = (size, digest.lower())

    root = Path(target_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("target object root must be a real directory")

    actual: dict[str, tuple[int, str]] = {}

    def record(path: Path, key: str) -> None:
        _validate_mytv_key(key)
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe target object")
        actual[key] = (path.stat().st_size, _sha256(path))

    for prefix in ("apk-update", "iptv"):
        scope = root / prefix
        if scope.is_symlink():
            raise ValueError("unsafe target object")
        if not scope.exists():
            continue
        if not scope.is_dir():
            raise ValueError("unsafe target object")
        for current, dirs, names in os.walk(scope, followlinks=False):
            for name in dirs:
                if (Path(current) / name).is_symlink():
                    raise ValueError("unsafe target object")
            for name in names:
                path = Path(current) / name
                record(path, path.relative_to(root).as_posix())

    for path in root.iterdir():
        if re.fullmatch(r"backend-hu[0-9]+\.zip", path.name):
            record(path, path.name)

    mismatched = sorted(
        key for key in expected.keys() | actual.keys() if expected.get(key) != actual.get(key)
    )
    manifest_bytes = json.dumps(
        source_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    canonical_files = [
        {"key": key, "size": size, "sha256": digest}
        for key, (size, digest) in sorted(expected.items())
    ]
    canonical_manifest = json.dumps(
        canonical_files, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "ok": not mismatched,
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_sha256": hashlib.sha256(canonical_manifest).hexdigest(),
        "source_objects": len(expected),
        "source_bytes": sum(size for size, _ in expected.values()),
        "target_objects": len(actual),
        "target_bytes": sum(size for size, _ in actual.values()),
        "matched_objects": len(expected) - sum(key in expected for key in mismatched),
        "mismatched_keys": mismatched,
    }


def stage_mytv_objects(
    environment_id: str,
    output_dir: str | Path,
    *,
    runner=None,
) -> dict[str, object]:
    if not environment_id or any(ch.isspace() for ch in environment_id):
        raise ValueError("CloudBase environment id is required")
    output_dir = Path(output_dir).resolve()
    if output_dir.is_relative_to(ROOT):
        raise ValueError("storage exports must be stored outside the repository")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("storage export directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except OSError:
        pass

    run = runner or subprocess.run
    tcb = _tcb_command_prefix()
    inventory_result = run(
        [*tcb, "-e", environment_id, "storage", "objects", "list", "-b", SOURCE_BUCKET, "--limit", "1000", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if inventory_result.returncode:
        raise RuntimeError("CloudBase storage inventory query failed")
    try:
        data = _parse_json_output(inventory_result.stdout).get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("objects"), list):
            raise ValueError("invalid object inventory")
        if data.get("hasNext"):
            raise ValueError("storage inventory exceeds the supported page size")
        objects, excluded = select_mytv_objects(data["objects"])
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("CloudBase storage inventory is invalid") from exc

    files: list[dict[str, object]] = []
    for item in objects:
        key = str(item["key"])
        local_path = output_dir.joinpath(*PurePosixPath(key).parts)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        download_result = run(
            [*tcb, "-e", environment_id, "storage", "objects", "download", key, str(local_path), "-b", SOURCE_BUCKET],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if download_result.returncode or not local_path.is_file():
            raise RuntimeError(f"CloudBase object download failed: {key}")
        size = local_path.stat().st_size
        if size != item["size"]:
            raise RuntimeError(f"CloudBase object size mismatch: {key}")
        files.append(
            {
                **item,
                "sha256": _sha256(local_path),
            }
        )

    manifest: dict[str, object] = {
        "schema_version": 1,
        "source_env": environment_id,
        "bucket": SOURCE_BUCKET,
        "excluded_objects": excluded,
        "files": files,
    }
    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    try:
        os.chmod(manifest_path, 0o600)
    except OSError:
        pass
    return {
        "manifest": str(manifest_path),
        "objects": len(files),
        "bytes": sum(int(item["size"]) for item in files),
        "excluded_objects": excluded,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage only My TV CloudBase Storage objects")
    parser.add_argument("--env-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    summary = stage_mytv_objects(args.env_id, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
