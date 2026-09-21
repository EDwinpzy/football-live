"""后台“控制塔”重设计的静态契约。

两类断言：
1. 原契约 —— 业务接口 / 路由 / DOM id / 表格单行规则在重设计中不能变；
2. 新结构 —— 命令轨四分组与总览事件时间线，按设计规格新增。

沿用 tools/tests/test_admin_release_menu_contract.py 的读文件 + 正则风格。
"""
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parents[2]
HTML = ROOT / "tools" / "admin.html"
CLOUD_HTML = ROOT / "cloudfunctions" / "otv-admin" / "admin.html"

SOURCE = HTML.read_text(encoding="utf-8")

TABS = ["overview", "licenses", "logs", "gen", "release", "ann", "iptv", "vod-sources"]
SECTIONS = ["tab-overview", "tab-licenses", "tab-logs", "tab-gen", "tab-release",
            "tab-hu", "tab-apk", "tab-ann", "tab-iptv", "tab-vod-sources"]
GROUPS = ["Observe", "Control", "Publish", "Sources"]


def _block(source: str, pattern: str) -> str:
    match = re.search(pattern, source, re.S)
    assert match, f"未找到：{pattern}"
    return match.group(1)


def _nav() -> str:
    return _block(SOURCE, r'<nav class="rail verbs">(.*?)</nav>')


def _nav_groups() -> dict:
    """命令轨分组：分组标题 -> 该分组内的按钮 HTML。"""
    parts = re.split(r'<div class="rail-grp">([^<]*)</div>', _nav())
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _overview() -> str:
    return _block(SOURCE, r'<section id="tab-overview">(.*?)</section>')


# ---- 原契约：重设计不得改动 ----

@pytest.mark.parametrize("tab", TABS)
def test_every_tab_entry_survives(tab):
    assert _nav().count(f'data-tab="{tab}"') == 1


@pytest.mark.parametrize("section_id", SECTIONS)
def test_every_content_section_survives(section_id):
    assert f'id="{section_id}"' in SOURCE


@pytest.mark.parametrize("node_id", ["pn-licenses", "k-total", "k-unsold", "k-sold", "k-banned",
                                     "kpi-hu", "kpi-husize", "ov-ledger", "health-card",
                                     "bd-stock", "bd-sold", "bd-ban", "bd-act-v", "bd-hu-v",
                                     "bd-bo-v", "bd-huver", "sys-activate", "sys-hu", "sys-blackout",
                                     "echo-msg", "echo-time"])
def test_status_nodes_survive(node_id):
    assert f'id="{node_id}"' in SOURCE


def test_data_tables_stay_single_line():
    rule = _block(SOURCE, r'table\.grid th,table\.grid td\{([^}]*)\}')
    assert "white-space:nowrap" in rule
    assert "text-overflow:ellipsis" in rule


def test_narrow_viewport_scrolls_tables_instead_of_wrapping():
    narrow = _block(SOURCE, r'@media \(max-width:920px\)\{(.*?)\n\}')
    assert ".gridwrap{overflow-x:auto}" in narrow


def test_release_menu_contract_is_untouched():
    assert 'data-release-tab="hu"' in SOURCE
    assert 'data-release-tab="apk"' in SOURCE
    assert "function switchReleaseTab" in SOURCE


def test_cloud_copy_is_byte_identical():
    assert HTML.read_bytes() == CLOUD_HTML.read_bytes()


# ---- 新结构：控制塔命令轨 ----

def test_command_rail_has_four_named_groups():
    assert list(_nav_groups()) == GROUPS


@pytest.mark.parametrize("group,tabs", [
    ("Observe", ["overview", "logs"]),
    ("Control", ["licenses", "gen"]),
    ("Publish", ["release", "ann"]),
    ("Sources", ["iptv", "vod-sources"]),
])
def test_each_group_holds_its_own_entries(group, tabs):
    body = _nav_groups()[group]
    for tab in tabs:
        assert f'data-tab="{tab}"' in body


# ---- 新结构：总览第一视口 ----

def test_overview_starts_with_the_event_timeline():
    overview = _overview()
    assert 'id="ov-timeline"' in overview
    assert overview.index('id="ov-timeline"') < overview.index('id="plans-cards"')


def test_timeline_renders_from_existing_log_interface():
    assert "function loadOvTimeline" in SOURCE
    assert 'api("logs"' in _block(SOURCE, r'async function loadOvTimeline\(\)\s*\{(.*?)\n\}')
    assert re.search(r'b\.dataset\.tab === "overview"\)\s*\{[^}]*loadOvTimeline\(\)', SOURCE)
