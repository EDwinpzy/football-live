#!/usr/bin/env python3
"""Pack a CloudBase bridge function into a Linux-correct zip.

The CloudBase CLI rewrites line endings on Windows, which turns a
``#!/bin/bash`` bootstrap into ``#!/bin/bash\\r`` and makes SCF fail with
``[./scf_bootstrap] no such file or directory``. This packer keeps scripts at
LF and marks them executable so the platform can start the container.

Usage:
    python tools/pack_bridge_function.py --dir cloudfunctions/activate \\
        --output /tmp/activate.zip
"""

from __future__ import annotations

import argparse
import pathlib
import stat
import zipfile

SCRIPT_NAMES = frozenset({"scf_bootstrap", "scf_bootstrap.sh"})
SKIP_DIRECTORIES = frozenset({"__pycache__", ".git"})


def _is_script(path: pathlib.Path) -> bool:
    return path.name in SCRIPT_NAMES or path.suffix == ".sh"


def pack(directory: pathlib.Path, output: pathlib.Path) -> dict[str, object]:
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError("function directory does not exist")
    entries: list[tuple[str, bytes, bool]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.suffix == ".pyc":
            continue
        raw = path.read_bytes()
        script = _is_script(path)
        if script:
            raw = raw.replace(b"\r\n", b"\n")
        entries.append((relative.as_posix(), raw, script))
    if not any(name == "scf_bootstrap" for name, _raw, _script in entries):
        raise ValueError("function directory has no scf_bootstrap")

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw, script in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if script else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, raw)
    return {
        "output": str(output),
        "files": len(entries),
        "executables": [name for name, _raw, script in entries if script],
        "bytes": output.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pack a CloudBase bridge function zip")
    parser.add_argument("--dir", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    import json

    print(json.dumps(pack(args.dir, args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
