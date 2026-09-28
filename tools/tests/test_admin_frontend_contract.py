"""后台管理页面的前后端静态契约测试（纯静态，无网络 / 无 VPS 依赖）。

背景：tools/admin.html 是单文件后台页面（CSS + HTML + JS 合一），后端接口由
tools/admin_server.py 的 ROUTES 字典提供。切栈（CloudBase → 自建 VPS）期间出过
真 bug：/api/logs 还在调 CloudBase 的 admin_logs RPC，自建库没有该函数，后台
"激活流水"页永远报错，而前端此前一行测试都没有。

本文件用静态解析把两侧契约钉死：
  1. 前端每个 api("xxx") 都必须在 ROUTES 里（防 404）
  2. 直接 fetch() 的上传 / 分片 / 登录路径在服务端必须有处理分支
  3. nav data-tab、容器 id、tab 切换列表三者对齐
  4. loadLogs / loadList / loadApkUpdates 等用到的字段与 SQL 导出一致
  5. 前端不得硬编码 CloudBase 域名、正式域名或写死绝对 API 地址
  6. 无重复 id、悬空 getElementById、未定义的 inline handler
"""
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parents[2]
HTML_PATH = ROOT / "tools" / "admin.html"
SERVER_PATH = ROOT / "tools" / "admin_server.py"
LICENSE_SQL = (ROOT / "tools" / "license_v2_migration.sql").read_text(encoding="utf-8")
BASE_SQL = (ROOT / "tools" / "selfhosted" / "base_schema.sql").read_text(encoding="utf-8")

SOURCE = HTML_PATH.read_text(encoding="utf-8")
SERVER = SERVER_PATH.read_text(encoding="utf-8")

TOP_TABS = ["overview", "licenses", "logs", "gen", "release", "ann", "iptv", "vod-sources"]


def _block(text: str, pattern: str, group: int = 1, flags: int = re.S) -> str:
    """按正则取一段内容，失败时直接给出可读断言。

    group=0 表示取整个匹配（pattern 不带捕获组时用）。
    flags 默认 DOTALL，pattern 里用到 ^ 锚定行首时需自行传 re.M。
    """
    match = re.search(pattern, text, flags)
    assert match, f"未匹配到：{pattern}"
    return match.group(group) if group else match.group(0)


def _frontend_api_names() -> set:
    """前端 api("xxx") 调用的全部接口名（排除 api 函数自身的定义行）。"""
    return set(re.findall(r'\bapi\(\s*"([a-z0-9_]+)"', SOURCE))


def _server_routes() -> set:
    """admin_server.py 的 ROUTES 字典里的全部 /api/xxx 路径。"""
    body = _block(SERVER, r"^ROUTES\s*=\s*\{(.*?)^\}", flags=re.S | re.M)
    return set(re.findall(r'"(/api/[a-z0-9_]+)"', body))


# ---------------------------------------------------------------- 路由契约


def test_frontend_api_calls_cover_at_least_40_endpoints():
    """反向保护：解析规则本身要有效，不能因为正则失配而"零调用全绿"。"""
    names = _frontend_api_names()
    assert len(names) >= 40, f"只解析出 {len(names)} 个 api() 调用，正则可能已失效：{sorted(names)}"
    assert "logs" in names and "apk_update_get" in names


def test_every_frontend_api_call_has_a_route():
    """前端调用了但 ROUTES 里没有 → 线上必然 404。"""
    names = _frontend_api_names()
    routes = _server_routes()
    missing = sorted(n for n in names if f"/api/{n}" not in routes)
    assert not missing, (
        "前端调用了 ROUTES 中不存在的接口（会 404）：\n  "
        + "\n  ".join(f'api("{n}")  →  缺 /api/{n}' for n in missing)
    )


def test_frontend_api_calls_match_route_suffixes():
    """抽查几个关键接口，确认前缀拼接方式（api("<name>") → /api/<name>）没被改坏。"""
    routes = _server_routes()
    for name in ("stats", "health", "licenses", "logs", "gen", "hu_list",
                 "apk_update_get", "ann_get", "iptv_list", "vod_source_list"):
        assert f"/api/{name}" in routes, f"缺少 /api/{name}"


def test_direct_fetch_upload_paths_are_implemented_server_side():
    """上传/分片/logo 走 fetch 而非 api()，服务端用 if path == ... 显式分支处理。"""
    direct = set(re.findall(r'API_BASE \+ "api/([a-z0-9_]+)\?', SOURCE))
    # 登录与 whoami 走固定字面量，不带 query string，单独断言
    assert "hu_upload" in direct and "apk_update_upload" in direct, (
        f"未解析到上传路径，解析规则可能失效：{sorted(direct)}"
    )
    missing = sorted(p for p in direct if f'"/api/{p}"' not in SERVER)
    assert not missing, (
        "前端直接 fetch 的路径在 admin_server.py 里没有对应处理分支：\n  "
        + "\n  ".join(f'api/{p}  →  服务端未实现' for p in missing)
    )


def test_login_and_whoami_use_relative_api_base():
    """登录与 whoami 必须走同源相对地址，不能写死域名。"""
    assert 'fetch(API_BASE + "api/login"' in SOURCE
    assert 'fetch(API_BASE + "api/whoami"' in SOURCE


# ---------------------------------------------------------------- tab 结构


def test_every_nav_button_has_a_matching_section():
    """每个 data-tab 按钮都要有对应容器。"""
    buttons = re.findall(r'data-tab="([a-z\-]+)"', SOURCE)
    assert set(buttons) == set(TOP_TABS), (
        f"导航按钮与 TOP_TABS 不一致：多余={sorted(set(buttons)-set(TOP_TABS))} "
        f"缺失={sorted(set(TOP_TABS)-set(buttons))}"
    )
    for tab in TOP_TABS:
        assert f'id="tab-{tab}"' in SOURCE, f'data-tab="{tab}" 缺少对应容器 id="tab-{tab}"'


def test_release_tab_contains_hu_and_apk_panels():
    """版本发布是二级 tab：外层 tab-release 内含 tab-hu / tab-apk 两个子面板。"""
    assert 'id="tab-release"' in SOURCE
    assert 'id="tab-hu"' in SOURCE
    assert 'id="tab-apk"' in SOURCE
    release_pos = SOURCE.index('id="tab-release"')
    hu_pos = SOURCE.index('id="tab-hu"')
    apk_pos = SOURCE.index('id="tab-apk"')
    assert release_pos < hu_pos < apk_pos, "tab-hu / tab-apk 应嵌套在 tab-release 之内"


def test_tab_switch_list_matches_top_tabs():
    """切换逻辑硬编码了一份 tab 列表，必须与导航按钮保持一致。"""
    listed = re.findall(r'"([a-z\-]+)"', _block(
        SOURCE, r'\["overview","licenses".*?\.forEach\(t =>', group=0))
    assert set(listed) == set(TOP_TABS), (
        f"切换列表与导航按钮不一致：{sorted(listed)} vs {sorted(TOP_TABS)}"
    )


def test_hash_deeplink_targets_known_tabs():
    """hash 深链白名单里的 tab 必须都在 TOP_TABS 内，避免点深链切到不存在的区块。"""
    listed = re.findall(r'"([a-z\-]+)"', _block(
        SOURCE, r'const h = location\.hash\.slice\(1\);(.*?)\.includes\(h\)', group=0))
    unknown = sorted(set(listed) - set(TOP_TABS))
    assert not unknown, f"深链白名单含未知 tab：{unknown}"


def test_no_duplicate_element_ids():
    """重复 id 会让 getElementById 拿到错误元素。"""
    ids = re.findall(r'\bid="([^"]+)"', SOURCE)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert not dup, f"存在重复 id：{dup}"


def test_every_getelementbyid_reference_exists():
    """JS 里 getElementById 的每个目标都必须在 HTML 中存在，否则运行时 TypeError。"""
    ids = set(re.findall(r'\bid="([^"]+)"', SOURCE))
    refs = set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', SOURCE))
    missing = sorted(refs - ids)
    assert not missing, (
        "JS 引用了不存在的元素 id（运行时会 null 报错）：\n  "
        + "\n  ".join(missing)
    )


def test_inline_handlers_only_call_defined_functions():
    """inline onclick 里的函数名必须在本文件里定义过。"""
    defined = set(re.findall(r"function\s+([A-Za-z0-9_$]+)\s*\(", SOURCE))
    defined |= set(re.findall(
        r"(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\(", SOURCE))
    defined |= set(re.findall(
        r"(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*async\b", SOURCE))
    called = set()
    for handler in re.findall(r'on(?:click|change|input|error|load)="([^"]*)"', SOURCE):
        called |= set(re.findall(r"\b([A-Za-z0-9_$]+)\s*\(", handler))
    # 内建 / 语言关键字，不是本文件定义的
    builtins = {"if", "for", "while", "switch", "catch", "return", "typeof",
                "this", "scrollIntoView", "click", "getElementById", "esc"}
    unknown = sorted(called - defined - builtins)
    assert not unknown, f"inline handler 调用了未定义的函数：{unknown}"


# ---------------------------------------------------------------- 字段契约


def test_activate_log_table_has_the_columns_frontend_renders():
    """loadLogs 渲染 at/code/result/device_id/ip，底层表必须有这些列。"""
    body = _block(BASE_SQL, r"CREATE TABLE IF NOT EXISTS public\.activate_log \((.*?)\n\);")
    cols = set(re.findall(r"^\s*([a-z_]+)\s+\w", body, re.M))
    needed = {"id", "at", "code", "result", "device_id", "ip"}
    assert needed <= cols, f"activate_log 缺列：{sorted(needed - cols)}（实际 {sorted(cols)}）"


def test_log_export_function_returns_activate_log_rows():
    """h_logs 依赖 admin_license_v2_log_export 返回 {rows, total}，行来自 activate_log。"""
    body = _block(
        LICENSE_SQL,
        r"(create or replace function public\.admin_license_v2_log_export.*?\$function\$;)",
    )
    assert "from public.activate_log" in body
    assert "'rows'" in body and "'total'" in body


def test_h_logs_calls_selfhosted_rpc_not_cloudbase():
    """历史 bug 回归守卫：/api/logs 曾调 CloudBase 的 admin_logs，自建库无此函数，
    "激活流水"页永久报错。这里锁死为 admin_license_v2_log_export。"""
    body = _block(SERVER, r"def h_logs\(d: dict\) -> dict:(.*?)\n\ndef ")
    assert 'rpc("admin_license_v2_log_export"' in body, (
        "h_logs 必须走自建栈的 admin_license_v2_log_export"
    )
    assert 'rpc("admin_logs"' not in body, (
        "h_logs 又调回 CloudBase 的 admin_logs —— 自建库没有该函数，"
        "激活流水页会永远报错"
    )


def test_h_logs_supports_code_filter():
    """前端有"按码过滤"输入框，后端需接受 code 参数。"""
    body = _block(SERVER, r"def h_logs\(d: dict\) -> dict:(.*?)\n\ndef ")
    assert 'd.get("code")' in body
    assert 'api("logs", {code:' in SOURCE


def test_renderlogs_reads_only_real_activate_log_columns():
    """renderLogs 引用的 r.<field> 必须都在 activate_log 表里存在。"""
    render = _block(SOURCE, r"function renderLogs\(\) \{(.*?)\n\}")
    used = set(re.findall(r"\br\.([a-z_]+)", render))
    body = _block(BASE_SQL, r"CREATE TABLE IF NOT EXISTS public\.activate_log \((.*?)\n\);")
    cols = set(re.findall(r"^\s*([a-z_]+)\s+\w", body, re.M))
    unknown = sorted(used - cols)
    assert not unknown, (
        f"renderLogs 读取了 activate_log 不存在的字段：{unknown}（表列 {sorted(cols)}）"
    )


def test_renderlogs_passes_result_into_result_tag():
    """resultTag(r.result) 是结果着色入口，result 值来自后端。"""
    assert 'resultTag(r.result)' in SOURCE


def test_licenses_rpc_returns_every_field_the_ledger_renders():
    """loadList / 总览台账渲染的字段必须都在 admin_licenses 的 select 里。"""
    body = _block(LICENSE_SQL, r"create or replace function public\.admin_licenses\(.*?select l\.code(.*?)\n\s*from public\.licenses l")
    cols = set(re.findall(r"l\.([a-z_]+)", "l.code" + body))
    needed = {"code", "plan", "status", "activated_at", "expire_at", "sold_at",
              "redeemed_at", "redeemed_to", "devices", "note", "created_at", "updated_at"}
    assert needed <= cols, (
        f"admin_licenses 少返回字段：{sorted(needed - cols)}（实际 {sorted(cols)}）"
    )


def test_ledger_renders_real_license_fields():
    """前端确实用到了这些字段（防"断言太松"）。"""
    table = _block(SOURCE, r"function ledgerTable\(.*?\n\}", group=0)
    for field in ("code", "plan", "status", "activated_at", "expire_at",
                  "redeemed_at", "redeemed_to", "devices", "sold_at",
                  "note", "created_at", "updated_at"):
        assert f"r.{field}" in table, f"ledgerTable 未渲染 r.{field}"


def test_stats_returns_plan_totals_frontend_sums():
    """loadStats 对 d.plans 求和 total/unsold/sold/banned。"""
    assert 'api("stats")' in SOURCE
    stats = _block(SOURCE, r"async function loadStats\(\) \{(.*?)\n\}")
    for key in ("total", "unsold", "sold", "banned"):
        assert f'"{key}"' in stats, f"loadStats 未统计 {key}"
    assert "d.plans" in stats


def test_apk_update_get_is_always_called_with_a_channel():
    """回归守卫：apk_update_get 后端强制要求 channel（tv|mobile），
    漏传直接返回 {"error": "渠道必须是 tv 或 mobile"}。"""
    assert 'api("apk_update_get", {channel})' in SOURCE, (
        "apk_update_get 调用必须带 channel 参数"
    )
    for line in ("tv", "mobile"):
        assert f'"{line}"' in SOURCE


def test_apk_channel_select_offers_exactly_tv_and_mobile():
    """渠道下拉的取值集合与后端 APK_CHANNELS 一致。"""
    options = re.findall(r'<select id="apk-channel"[^>]*>(.*?)</select>', SOURCE, re.S)
    assert options, "找不到 #apk-channel 下拉"
    values = set(re.findall(r'value="([a-z]+)"', options[0]))
    assert values == {"tv", "mobile"}, f"渠道下拉取值与后端不符：{sorted(values)}"


def test_apk_update_get_backend_validates_channel():
    """后端必须真的校验 channel，否则"漏传就报错"的契约就不成立。"""
    body = _block(SERVER, r"def h_apk_update_get\(d: dict\) -> dict:(.*?)\n\ndef ")
    assert 'd.get("channel")' in body
    assert "APK_CHANNELS" in body


def test_announcement_fields_use_camel_case_updatedAt():
    """前端读 a.updatedAt（驼峰），后端 SQL 也必须返回驼峰键。"""
    assert "a.updatedAt" in SOURCE
    ann_sql = (ROOT / "tools" / "announcement_migration.sql").read_text(encoding="utf-8")
    body = _block(ann_sql, r"create or replace function public\.announcement_admin_get\(\).*?end \$\$;", group=0)
    for key in ("'id'", "'title'", "'content'", "'status'", "'updatedAt'"):
        assert key in body, f"announcement_admin_get 缺键 {key}"


def test_iptv_list_returns_channels_and_latest():
    """loadIptv 读 d.channels / d.mode / d.latest.version。"""
    body = _block(SERVER, r"def h_iptv_list\(_d: dict\) -> dict:(.*?)\n\ndef ")
    assert "iptv_admin_list" in body
    assert '"mode"' in body
    iptv_sql = (ROOT / "tools" / "iptv_migration.sql").read_text(encoding="utf-8")
    fn = _block(iptv_sql, r"create or replace function public\.iptv_admin_list\(\).*?end \$\$;", group=0)
    for key in ("'channels'", "'latest'", "'version'", "'createdAt'"):
        assert key in fn, f"iptv_admin_list 缺键 {key}"
    assert "d.channels || []" in SOURCE


def test_iptv_group_list_returns_array_frontend_expects_array():
    """前端 Array.isArray(d)，SQL 侧必须 json_agg 成数组。"""
    iptv_sql = (ROOT / "tools" / "iptv_migration.sql").read_text(encoding="utf-8")
    fn = _block(iptv_sql, r"create or replace function public\.iptv_admin_group_list\(\).*?end \$\$;", group=0)
    assert "json_agg" in fn
    assert "Array.isArray(d)" in SOURCE


def test_iptv_save_accepts_plain_url_array():
    """前端传 urls: [string]，后端负责包成 [{"url": u}]。"""
    assert "urls:" in SOURCE
    body = _block(SERVER, r"def h_iptv_save\(d: dict\) -> dict:(.*?)\n\ndef ")
    assert 'd.get("urls")' in body


def test_vod_source_test_frontend_and_backend_agree_on_latency_field():
    """回归守卫：前端 vodSourceTestRun 用 d.latency_ms 写测速记录，
    h_vod_source_test 必须返回该字段，否则该源被记成 0ms、按延时排序时假值排最前。"""
    handler = _block(SERVER, r"def h_vod_source_test\(d: dict\) -> dict:(.*?)\n\ndef ")
    uses_latency = "d.latency_ms" in SOURCE
    returns_latency = "latency_ms" in handler
    assert not (uses_latency and not returns_latency), (
        "前端读 d.latency_ms，但 h_vod_source_test 不返回该字段 —— "
        "测速记录会写成 0ms 并在延时排序中被当作最快源"
    )


def test_vod_source_speed_does_return_latency_ms():
    """对照组：测速接口确实返回 latency_ms，说明字段名本身没问题，缺口只在 test 接口。"""
    handler = _block(SERVER, r"def h_vod_source_speed\(d: dict\) -> dict:(.*?)\n\ndef ")
    assert "latency_ms" in handler


# ------------------------------------------------------------ 域名硬编码


FORBIDDEN_HOSTS = [
    "tcloudbase.com",
    "cloudbase.net",
    "appletv-d5ge1bth794873f76",
    "mytv-cloud.pengzhiyuan0724.chatgpt.site",
    "pengzhiyuan0724",
]


@pytest.mark.parametrize("host", FORBIDDEN_HOSTS)
def test_frontend_has_no_hardcoded_backend_host(host):
    """已退役的 CloudBase 与正式域名都不该出现在后台页面里。
    后台通过同源相对路径访问后端，写死域名会让 IP 入口 / 自建域名切换直接失效。"""
    hits = [
        f"{path}:{i}: {line.strip()}"
        for path, text in (("admin.html", SOURCE), ("admin_server.py", SERVER))
        for i, line in enumerate(text.splitlines(), 1)
        if host in line and not line.lstrip().startswith(("#", "*", "//"))
    ]
    assert not hits, f"发现硬编码域名 {host}：\n  " + "\n  ".join(hits)


def test_frontend_external_urls_are_limited_to_cdn_or_placeholder():
    """前端允许出现的绝对 URL 只有 hls.js CDN 和 placeholder，其余一律视为写死地址。"""
    allowed = ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com",
               "https://…", "https://example.com")
    offenders = []
    for i, line in enumerate(SOURCE.splitlines(), 1):
        for url in re.findall(r"https?://[^\s\"'<>)]+", line):
            if not any(a in url for a in allowed):
                offenders.append(f"admin.html:{i}: {url}")
    assert not offenders, "存在非白名单绝对 URL：\n  " + "\n  ".join(offenders)


def test_api_base_is_derived_from_location_not_hardcoded():
    """API_BASE 必须由 location.pathname 推导（根入口 / 与子路径 /admin/ 都能用）。"""
    body = _block(SOURCE, r"const API_BASE = (.*?);")
    assert "location.pathname" in body
    assert "http://" not in body and "https://" not in body


# ---------------------------------------------------- 死代码（只记录不断言）


DEAD_CODE_EXPECTATION = {
    # ROUTES 里存在、但 admin.html 从未调用。仅登记，供后续决定去留，
    # 断言只为"防止清单与实际脱节"而不是要求删除。
    "expected_unused_routes": {"iptv_probe", "iptv_refresh_token", "reset", "reset_preview"},
}


def test_known_unused_routes_are_still_unused():
    """登记当前"ROUTES 有、前端不调"的接口集合，防止清单悄悄失真。"""
    names = _frontend_api_names()
    for route in DEAD_CODE_EXPECTATION["expected_unused_routes"]:
        assert route not in names, (
            f"/api/{route} 此前是未使用的死代码，现在前端开始调用了；"
            "请更新 DEAD_CODE_EXPECTATION 清单"
        )
