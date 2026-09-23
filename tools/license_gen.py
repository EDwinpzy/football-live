#!/usr/bin/env python3
"""My TV 卡密签发工具（v1.17）

用法:
  python license_gen.py init                          # 首次生成密钥对（已存在则拒绝，防覆盖）
  python license_gen.py gen --plan monthly --count 100 [--note "首发批次"]
  python license_gen.py verify --dir out/20260901-...  # 自校验批次票据签名
  python license_gen.py pubkey                        # 打印公钥（供 App 内嵌）

输出（out/<批次目录>/）:
  cards.csv       发卡平台导入用（code,plan,days,price,note）
  tickets.jsonl   票据原文（含签名），供 verify 与入库核对
  import.sql      PG licenses 表导入脚本（INSERT ... ON CONFLICT DO NOTHING）

私钥 tools/keys/private_key.pem 是整个体系的命根子：不进仓库、不贴进任何会话，请离线备份一份。
私钥来源优先级：环境变量 OTV_PRIV_KEY，其次运行时文件 OTV_PRIV_KEY_FILE，最后本机
tools/keys/private_key.pem。私钥不得放进镜像或仓库。
"""
import argparse
import base64
import json
import os
import secrets
import stat
import sys
from datetime import date, datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature

ROOT = Path(__file__).resolve().parent
KEY_DIR = ROOT / "keys"
PRIV_PATH = KEY_DIR / "private_key.pem"
PUB_PATH = KEY_DIR / "public_key.pem"
# 输出目录：云端 /var/user 只读，云函数里用 OTV_OUT_DIR 指到 /tmp（见 build_admin_function.py）
OUT_DIR = Path(os.environ.get("OTV_OUT_DIR") or (ROOT / "out"))
LEDGER_PATH = OUT_DIR / "used_codes.txt"

# 32 字符表：剔除 0/O/1/I，遥控器输入与人工辨认均无歧义
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
PLANS = {
    "monthly":   {"days": 30,  "price": "9.9"},
    "quarterly": {"days": 90,  "price": "29.9"},
    "yearly":    {"days": 365, "price": "99.9"},
    "lifetime":  {"days": 0,   "price": "128"},
    # v1.20 周卡（2026-09-03 用户需求）：仅后台签发赠送、不在 app 内售卖，
    # price 仅入 cards.csv 备注口径（0=非卖品）
    "weekly":    {"days": 7,   "price": "0"},
}
TICKET_V = 1


def canonical(code: str, plan: str, days: int, issued: str, nonce: str) -> str:
    return f"{TICKET_V}|{code}|{plan}|{days}|{issued}|{nonce}"


def load_priv():
    env = os.environ.get("OTV_PRIV_KEY")
    if env:
        return serialization.load_pem_private_key(
            env.replace("\\n", "\n").encode("utf-8"), password=None)
    path = Path(os.environ.get("OTV_PRIV_KEY_FILE") or PRIV_PATH)
    if not path.exists():
        sys.exit("私钥不存在，先执行: python license_gen.py init")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        sys.exit("私钥文件权限不安全，必须限制为 0600 或更严格")
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def load_pub():
    env = os.environ.get("OTV_PRIV_KEY")
    if env or os.environ.get("OTV_PRIV_KEY_FILE"):
        # 云端没有公钥文件：从私钥推导（同一把钥匙，结果与本机 keys/public_key.pem 一致）
        return load_priv().public_key()
    if not PUB_PATH.exists():
        sys.exit("公钥不存在，先执行: python license_gen.py init")
    return serialization.load_pem_public_key(PUB_PATH.read_bytes())


def cmd_init(_args):
    KEY_DIR.mkdir(exist_ok=True)
    if PRIV_PATH.exists():
        sys.exit(f"私钥已存在（{PRIV_PATH}），拒绝覆盖。确认要重造体系请手工删除。")
    key = ec.generate_private_key(ec.SECP256R1())
    PRIV_PATH.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    PUB_PATH.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    print(f"密钥对已生成:\n  {PRIV_PATH}\n  {PUB_PATH}\n"
          "请立即离线备份私钥，且不要提交到任何仓库或上传到云端。")


def gen_code(rng: secrets.SystemRandom) -> str:
    body = "".join(rng.choice(ALPHABET) for _ in range(10))
    return f"OTV-{body[:5]}-{body[5:]}"


def cmd_gen(args):
    if args.plan not in PLANS:
        sys.exit(f"plan 必须是 {list(PLANS)}")
    plan_cfg = PLANS[args.plan]
    issued = date.today().isoformat()
    rng = secrets.SystemRandom()

    used = set()
    if LEDGER_PATH.exists():
        used = {ln.strip() for ln in LEDGER_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()}

    rows, tickets = [], []
    while len(rows) < args.count:
        code = gen_code(rng)
        if code in used:
            continue
        used.add(code)
        nonce = secrets.token_hex(8)
        days = plan_cfg["days"]
        sig = base64.b64encode(load_priv().sign(
            canonical(code, args.plan, days, issued, nonce).encode(),
            ec.ECDSA(hashes.SHA256()),
        )).decode()
        ticket = {"v": TICKET_V, "code": code, "plan": args.plan, "days": days,
                  "issued": issued, "nonce": nonce, "sig": sig}
        tickets.append(ticket)
        rows.append((code, args.plan, days, plan_cfg["price"], args.note or ""))

    batch = f"{issued.replace('-', '')}-{args.plan}-{args.count}"
    batch_dir = OUT_DIR / batch
    if batch_dir.exists():
        # 同名批次（同日 + 同套餐 + 同数量）不覆盖前一批的导入文件：
        # 云端 artifacts 落在 /tmp，同实例复用目录，直接覆盖会丢掉上一批的 cards.csv
        batch = f"{batch}-{datetime.now().strftime('%H%M%S')}"
        batch_dir = OUT_DIR / batch
    batch_dir.mkdir(parents=True, exist_ok=True)

    with (batch_dir / "cards.csv").open("w", encoding="utf-8-sig", newline="") as f:
        f.write("code,plan,days,price,note\n")
        for r in rows:
            note = (r[4] or "").replace('"', '""')
            f.write(f'{r[0]},{r[1]},{r[2]},{r[3]},"{note}"\n')

    (batch_dir / "tickets.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tickets), encoding="utf-8")

    def q(s: str) -> str:
        return s.replace("'", "''")

    sql_lines = [
        "-- 批次 %s 生成于 %s" % (batch, issued),
        "insert into licenses (code, plan, days, ticket, note) values",
        ",\n".join(
            "('{c}', '{p}', {d}, $t${j}$t$, '{n}')".format(
                c=t["code"], p=t["plan"], d=t["days"],
                j=json.dumps(t, ensure_ascii=False, separators=(",", ":")),
                n=q(args.note or ""))
            for t in tickets),
        "on conflict (code) do nothing;",
    ]
    (batch_dir / "import.sql").write_text("\n".join(sql_lines) + "\n", encoding="utf-8")

    LEDGER_PATH.parent.mkdir(exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(r[0] for r in rows) + "\n")

    print(f"批次 {batch}: 生成 {len(rows)} 个码 → {batch_dir}")
    print("  cards.csv    发卡平台导入")
    print("  import.sql   待授权后在 CloudBase PG 执行")


def cmd_verify(args):
    tickets_file = Path(args.dir) / "tickets.jsonl"
    if not tickets_file.exists():
        sys.exit(f"找不到 {tickets_file}")
    pub = load_pub()
    ok = bad = 0
    for line in tickets_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        t = json.loads(line)
        try:
            pub.verify(base64.b64decode(t["sig"]),
                       canonical(t["code"], t["plan"], t["days"], t["issued"], t["nonce"]).encode(),
                       ec.ECDSA(hashes.SHA256()))
            ok += 1
        except (InvalidSignature, KeyError):
            bad += 1
            print(f"  验签失败: {t.get('code', '?')}")
    print(f"{tickets_file}: 通过 {ok} / 失败 {bad}")
    sys.exit(1 if bad else 0)


def cmd_pubkey(_args):
    print(PUB_PATH.read_text(encoding="utf-8"))


def main():
    p = argparse.ArgumentParser(description="OptimalTV 卡密签发")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="首次生成密钥对").set_defaults(fn=cmd_init)
    g = sub.add_parser("gen", help="批量生成卡密")
    g.add_argument("--plan", required=True, choices=list(PLANS))
    g.add_argument("--count", type=int, required=True)
    g.add_argument("--note", default="", help="批次备注，写入 CSV 与 DB note")
    g.set_defaults(fn=cmd_gen)
    v = sub.add_parser("verify", help="校验批次票据签名")
    v.add_argument("--dir", required=True)
    v.set_defaults(fn=cmd_verify)
    sub.add_parser("pubkey", help="打印公钥 PEM").set_defaults(fn=cmd_pubkey)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
