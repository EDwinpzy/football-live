#!/usr/bin/env python3
"""把 media_matrix.json 里指向已删文档的 source 引用改到现存文档。

docs/superpowers/ 下的 plans 与 specs 已按"全项目只保留两份文档"的要求
删除，media_matrix.json 里的 source 字段因此全部断链——验收矩阵的每条
阈值/规格都失去出处。这个脚本做一次性重定向，并保证可重复执行。
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tools" / "qa" / "media_matrix.json"

REDIRECT = {
    "docs/superpowers/specs/2026-09-24-mytv-android-playback-design.md":
        "docs/项目文档.md#3-端侧功能模块",
    "docs/superpowers/specs/2026-09-24-mytv-web-performance-and-acceptance-design.md":
        "docs/项目文档.md#5-网页版",
    "docs/superpowers/plans/2026-09-24-mytv-vod-resolver.md":
        "docs/项目文档.md#3-端侧功能模块",
    "docs/superpowers/plans/2026-09-24-mytv-selfhosted-retirement.md":
        "docs/项目文档.md#7-自建-vps-基础设施",
    "docs/superpowers/plans/2026-09-24-existing-vps-vod-performance.md":
        "docs/项目文档.md#14-运维排障手册",
    "docs/superpowers/plans/2026-09-24-metadata-center-selfhosted.md":
        "docs/项目文档.md#7-自建-vps-基础设施",
    "docs/superpowers/plans/2026-09-24-mytv-selfhosted-retirement-design.md":
        "docs/项目文档.md#7-自建-vps-基础设施",
}


def rewrite(node) -> int:
    changed = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "source" and isinstance(value, str) and value in REDIRECT:
                node[key] = REDIRECT[value]
                changed += 1
            else:
                changed += rewrite(value)
    elif isinstance(node, list):
        for item in node:
            changed += rewrite(item)
    return changed


def main() -> int:
    payload = json.loads(MATRIX.read_text(encoding="utf-8"))
    changed = rewrite(payload)
    MATRIX.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("重定向 %d 处 source 引用" % changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
