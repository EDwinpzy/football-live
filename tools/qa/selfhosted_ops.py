"""Read-only self-hosted VPS checks used by the full-chain optimisation work.

Credentials come from the environment so nothing secret lives in the repo:

    MYTV_VPS_HOST, MYTV_VPS_PORT, MYTV_VPS_USER, MYTV_VPS_PASSWORD

Commands
    exit-check    probe every configured VOD source API from the production host
    endpoints     health / web / vod / metadata / activate through nginx
    resolve       exercise /web/vod/resolve with ids from the live metadata db
    tree-check    verify every path the compose file mounts exists on the host
    compose-diff  web-api environment differences, repo vs host
    web-runtime   how the live container serves the Web front end

Nothing here writes to the host.
"""
import json
import os
import re
import sys

import paramiko

HOST = os.environ.get("MYTV_VPS_HOST", "")
PORT = int(os.environ.get("MYTV_VPS_PORT", "55560"))
USER = os.environ.get("MYTV_VPS_USER", "root")
PASSWORD = os.environ.get("MYTV_VPS_PASSWORD", "")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCES = os.path.join(REPO, "tools", "backend-src", "vod_sources.default.json")
COMPOSE = os.path.join(REPO, "deploy", "selfhosted", "compose.yaml")
REMOTE_COMPOSE = "/srv/mytv/deploy/selfhosted/compose.yaml"

BASE = "https://127.0.0.1:8443"

ENDPOINTS = [
    "/health",
    "/web/",
    "/web/vod/home?category=movie",
    "/web/vod/resolve/10000001?episode=1",
    "/metadata/health",
    "/activate",
]

EXIT_SCRIPT = r"""
import json, time, urllib.request, urllib.error


def probe(url, timeout=6):
    started = time.time()
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
            code = getattr(response, "status", 200)
            return {"ok": True, "code": code, "ms": int((time.time() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        return {"ok": True, "code": exc.code, "ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        name = type(exc).__name__
        return {"ok": False, "err": name + ":" + str(exc)[:90],
                "ms": int((time.time() - started) * 1000)}


targets = json.load(open("/tmp/mytv_probe_targets.json", encoding="utf-8"))
rows = []
for item in targets:
    rows.append({"id": item["id"], "url": item["url"], "r": probe(item["url"])})
print(json.dumps(rows, ensure_ascii=False))
""".strip()

LIST_MEDIA = (
    "import sqlite3\n"
    "conn = sqlite3.connect('/srv/metadata/db/metadata-latest.sqlite')\n"
    "for row in conn.execute('select id, title from media order by id limit 5'):\n"
    "    print('%s\\t%s' % row)\n"
)

TREE_CHECK = (
    "import os, sys\n"
    "items = sys.stdin.read().split()\n"
    "missing = [i for i in items if not os.path.exists(os.path.join('/srv/mytv', i))]\n"
    "print('checked %d, missing %d' % (len(items), len(missing)))\n"
    "for item in missing:\n"
    "    print('MISS ' + item)\n"
)


def connect():
    if not HOST or not PASSWORD:
        sys.exit("set MYTV_VPS_HOST and MYTV_VPS_PASSWORD (see .learnings/SERVER.md)")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASSWORD,
                   look_for_keys=False, allow_agent=False)
    return client


def run(client, command, timeout=300):
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    return (stdout.read().decode("utf-8", "replace").strip()
            or stderr.read().decode("utf-8", "replace").strip())


def put(client, remote_path, text):
    sftp = client.open_sftp()
    with sftp.open(remote_path, "w") as handle:
        handle.write(text.encode("utf-8"))
    sftp.close()


def exit_check(client):
    handle = open(SOURCES, encoding="utf-8")
    sources = json.load(handle)["sources"]
    handle.close()
    targets = []
    for source in sources:
        base = source.get("api_url") or source["site_url"]
        url = base if base.endswith("/") else base + "/"
        if "provide/vod" in url:
            url = url + "?ac=list"
        targets.append({"id": source["id"], "url": url})
    put(client, "/tmp/mytv_probe_targets.json", json.dumps(targets, ensure_ascii=False))
    put(client, "/tmp/mytv_probe_sources.py", EXIT_SCRIPT)
    raw = run(client, "python3 /tmp/mytv_probe_sources.py", timeout=300)
    if not raw.strip():
        print("no output from the probe")
        return 1
    results = json.loads(raw.strip().splitlines()[-1])
    reachable = 0
    for item in results:
        result = item["r"]
        if result.get("ok"):
            reachable += 1
            row = "%-9s OK   %4s %6sms  %s" % (item["id"], result["code"],
                                               result["ms"], item["url"])
            print(row)
        else:
            row = "%-9s FAIL     %6sms  %s" % (item["id"], result["ms"], result["err"])
            print(row)
    print("")
    print("reachable %d/%d" % (reachable, len(results)))
    return 0


def endpoints(client):
    for path in ENDPOINTS:
        command = ("curl -sk -m 25 -o /dev/null -w '%{http_code} %{time_total}s' '"
                   + BASE + path + "'")
        print("%-42s %s" % (path, run(client, command)))
    print("")
    print("resolve body:")
    print(run(client, "curl -sk -m 25 '" + BASE + "/web/vod/resolve/10000001?episode=1'")[:400])
    return 0


def resolve(client):
    print("== containers")
    print(run(client, "docker ps --format '{{.Names}}\t{{.Status}}' | sort"))
    put(client, "/tmp/mytv_list_media.py", LIST_MEDIA)
    rows = run(client,
               "docker cp /tmp/mytv_list_media.py selfhosted-metadata-api-1:/tmp/mytv_l.py"
               " && docker exec selfhosted-metadata-api-1 python /tmp/mytv_l.py")
    print("")
    print("== resolve for real metadata ids")
    for line in rows.splitlines():
        if "\t" not in line:
            continue
        mytv_id = line.split("\t")[0].strip()
        url = BASE + "/web/vod/resolve/" + mytv_id + "?episode=1"
        body = run(client, "curl -sk -m 30 '" + url + "'")
        print("  %-10s -> %s" % (mytv_id, body[:220]))
    return 0


def tree_check(client):
    handle = open(COMPOSE, encoding="utf-8")
    text = handle.read()
    handle.close()
    wanted = sorted({m.group(1) for m in re.finditer(r"\.\./\.\./([^\s:]+)", text)})
    put(client, "/tmp/mytv_tree_paths.txt", "\n".join(wanted))
    put(client, "/tmp/mytv_tree_check.py", TREE_CHECK)
    print("checked %d referenced paths" % len(wanted))
    print(run(client, "python3 /tmp/mytv_tree_check.py < /tmp/mytv_tree_paths.txt"))
    return 0


def compose_diff(client):
    import difflib
    sftp = client.open_sftp()
    with sftp.open(REMOTE_COMPOSE, "r") as handle:
        remote = handle.read().decode("utf-8", "replace")
    sftp.close()
    handle = open(COMPOSE, encoding="utf-8")
    local = handle.read()
    handle.close()

    def block(text):
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip() == "web-api:"), None)
        if start is None:
            return []
        collected = []
        for line in lines[start + 1:]:
            if line and not line[0].isspace():
                break
            collected.append(line)
        return collected

    diff = list(difflib.unified_diff(block(remote), block(local),
                                     fromfile="host", tofile="repo", lineterm="", n=1))
    print("\n".join(diff) if diff else "web-api blocks are identical")
    return 0


def web_runtime(client):
    checks = [
        ("cmd", "docker inspect selfhosted-web-api-1"
                " --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}'"),
        ("web root", "docker exec selfhosted-web-api-1 ls -l /app/www"),
        ("index head", "docker exec selfhosted-web-api-1"
                       " sh -c 'head -c 200 /app/www/index.html'"),
    ]
    for label, command in checks:
        print("== " + label)
        print(run(client, command)[:900])
    return 0


COMMANDS = {
    "exit-check": exit_check,
    "endpoints": endpoints,
    "resolve": resolve,
    "tree-check": tree_check,
    "compose-diff": compose_diff,
    "web-runtime": web_runtime,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit("usage: selfhosted_ops.py <%s>" % "|".join(sorted(COMMANDS)))
    client = connect()
    try:
        return COMMANDS[sys.argv[1]](client)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
