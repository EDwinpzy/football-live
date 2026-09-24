#!/usr/bin/env python3
"""Snapshot, clear, and regenerate My TV license inventory for protocol v2.

The destructive operation is intentionally separate from the schema migration.
Credentials are read from the self-hosted runtime configuration and are never printed.
"""
import argparse
import hashlib
import hmac
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

from selfhosted import runtime_client


ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "out" / "license-v2-reset"
ENVIRONMENT = "mytv-selfhosted"
CONFIRM = "CLEAR-ALL-LICENSES"
PAGE_SIZE = 200
PLANS = ("weekly", "monthly", "quarterly", "yearly", "lifetime")


def _rpc(name: str, payload: dict, token: str):
    url = runtime_client.REST_BASE + "/rpc/" + name
    runtime_client.check_url(url)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.build_opener(runtime_client.NoRedirect).open(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"RPC {name} HTTP {exc.code}: {detail}") from exc


def _all_rows(token: str, rpc_name: str):
    rows = []
    offset = 0
    expected = None
    while expected is None or offset < expected:
        page = _rpc(rpc_name, {"p_limit": PAGE_SIZE, "p_offset": offset}, token)
        if not isinstance(page, dict) or not isinstance(page.get("rows"), list):
            raise RuntimeError(f"RPC {rpc_name} 返回格式异常")
        expected = int(page.get("total", 0))
        rows.extend(page["rows"])
        if not page["rows"]:
            break
        offset += len(page["rows"])
    if expected != len(rows):
        raise RuntimeError(f"RPC {rpc_name} 分页不完整: 期望 {expected}，实际 {len(rows)}")
    return rows


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_snapshot(environment: str) -> pathlib.Path:
    if environment != ENVIRONMENT:
        raise SystemExit(f"环境不匹配，只允许 {ENVIRONMENT}")
    token = runtime_client.signin()
    licenses = _all_rows(token, "admin_license_v2_export")
    logs = _all_rows(token, "admin_license_v2_log_export")
    counts = Counter(str(row.get("plan", "")) for row in licenses)
    unknown = sorted(set(counts) - set(PLANS))
    if unknown:
        raise RuntimeError("发现未知套餐，拒绝生成快照: " + ", ".join(unknown))
    body = {
        "schema": 1,
        "environment": environment,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "planCounts": {plan: counts.get(plan, 0) for plan in PLANS},
        "licenses": licenses,
        "activateLog": logs,
    }
    snapshot = {**body, "snapshotSha256": _digest(body)}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = OUT / stamp
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"快照已保存: {path}")
    print("套餐数量: " + json.dumps(snapshot["planCounts"], ensure_ascii=False))
    print("SHA-256: " + snapshot["snapshotSha256"])
    return path


def load_snapshot(path: pathlib.Path, environment: str):
    if not path.is_file():
        raise SystemExit(f"快照不存在: {path}")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    digest = snapshot.pop("snapshotSha256", "")
    if not digest or not hmac.compare_digest(digest, _digest(snapshot)):
        raise SystemExit("快照 SHA-256 校验失败，拒绝继续")
    snapshot["snapshotSha256"] = digest
    if snapshot.get("environment") != environment or environment != ENVIRONMENT:
        raise SystemExit("快照环境与目标环境不一致，拒绝继续")
    counts = snapshot.get("planCounts")
    if not isinstance(counts, dict) or sum(int(v) for v in counts.values()) != len(snapshot.get("licenses", [])):
        raise SystemExit("快照套餐统计与卡密行数不一致，拒绝继续")
    return snapshot


def clear_inventory(environment: str, snapshot_path: pathlib.Path, confirmation: str):
    snapshot = load_snapshot(snapshot_path, environment)
    if confirmation != CONFIRM:
        raise SystemExit(f"确认短语错误；必须显式传入 --confirm {CONFIRM}")
    if not snapshot.get("licenses"):
        raise SystemExit("快照中没有卡密；为防止误操作，拒绝清空")
    token = runtime_client.signin()
    result = _rpc(
        "admin_license_v2_clear",
        {"p_environment": environment, "p_confirm": confirmation},
        token,
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("清空失败: " + json.dumps(result, ensure_ascii=False))
    expected_licenses = len(snapshot["licenses"])
    if int(result.get("deletedLicenses", -1)) != expected_licenses:
        raise RuntimeError(
            f"清空数量异常: 快照 {expected_licenses}，数据库删除 {result.get('deletedLicenses')}"
        )
    print(f"已清空卡密 {result['deletedLicenses']} 条、激活日志 {result.get('deletedLogs', 0)} 条")


def regenerate_inventory(environment: str, snapshot_path: pathlib.Path):
    snapshot = load_snapshot(snapshot_path, environment)
    generated = []
    for plan in PLANS:
        count = int(snapshot["planCounts"].get(plan, 0))
        if count <= 0:
            continue
        subprocess.run(
            [sys.executable, str(TOOLS / "license_gen.py"), "gen", "--plan", plan,
             "--count", str(count), "--note", "卡密协议v2重置批次"],
            check=True,
            cwd=TOOLS,
        )
        batch = pathlib.Path(os.environ.get("OTV_OUT_DIR") or (TOOLS / "out")) / f"{datetime.now().strftime('%Y%m%d')}-{plan}-{count}"
        subprocess.run(
            [sys.executable, str(TOOLS / "selfhosted" / "import_licenses.py"), str(batch)],
            check=True,
            cwd=TOOLS,
        )
        generated.append({"plan": plan, "count": count, "batch": str(batch)})
    manifest = snapshot_path.parent / "regenerated.json"
    manifest.write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"新卡已按原套餐数量生成并导入，批次清单: {manifest}")


def main():
    parser = argparse.ArgumentParser(description="卡密协议 v2 存量重置工具")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="导出卡密与激活日志并统计套餐数量")
    snap.add_argument("--environment", required=True)
    clear = sub.add_parser("clear", help="使用已校验快照清空旧卡和激活日志")
    clear.add_argument("--environment", required=True)
    clear.add_argument("--snapshot", required=True, type=pathlib.Path)
    clear.add_argument("--confirm", required=True)
    regen = sub.add_parser("regenerate", help="按快照套餐数量生成并导入全新卡密")
    regen.add_argument("--environment", required=True)
    regen.add_argument("--snapshot", required=True, type=pathlib.Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        create_snapshot(args.environment)
    elif args.command == "clear":
        clear_inventory(args.environment, args.snapshot, args.confirm)
    else:
        regenerate_inventory(args.environment, args.snapshot)


if __name__ == "__main__":
    main()
