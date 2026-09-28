#!/usr/bin/env python3
"""把要进容器的脚本/psql/sql 规范成 LF，并可校验已同步到 VPS 的副本。

背景：Windows 上编辑或用 scp 直传这些文件时会带上 CRLF。容器里
/bin/sh 会直接报 "set: line 2: illegal option -"，psql 的 \\ir 会把行尾
的 \\r 当成 SQL 一部分（曾表现为 `ERROR: column "f" does not exist`）。
仓库根的 .gitattributes 只约束 git 往返，绕开 git 的 scp 不受它管。

用法：
    python tools/selfhosted/normalize_line_endings.py            # 就地修正
    python tools/selfhosted/normalize_line_endings.py --check    # 只检查，退出码 1 表示有 CRLF
    python tools/selfhosted/normalize_line_endings.py --check --root /srv/mytv/tools
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PATTERNS = ("*.sh", "*.psql", "*.sql")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "out", "dist", ".venv"}


def targets(root: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in PATTERNS:
        for path in root.rglob(pattern):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            found.append(path)
    return found


def has_crlf(path: Path) -> bool:
    return b"\r\n" in path.read_bytes()


def fix(path: Path) -> None:
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"\r\n", b"\n"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--check", action="store_true",
                    help="只报告，不修改；有 CRLF 时退出码 1")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    offenders = [p for p in targets(root) if has_crlf(p)]
    if not offenders:
        print(f"OK: {root} 下没有 CRLF 脚本")
        return 0

    for path in offenders:
        print(("CRLF: " if args.check else "fix:   ") + str(path))
    if args.check:
        print(f"\n{len(offenders)} 个文件是 CRLF（容器内会出错）")
        return 1
    for path in offenders:
        fix(path)
    print(f"\n已修正 {len(offenders)} 个文件为 LF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
