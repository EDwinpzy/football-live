from pathlib import Path
import re


ROOT = Path(__file__).parents[2]
HTML = ROOT / "tools" / "admin.html"


def _nav_source() -> str:
    source = HTML.read_text(encoding="utf-8")
    match = re.search(r'<nav class="rail verbs">(.*?)</nav>', source, re.S)
    assert match, "后台左侧导航不存在"
    return match.group(1)


def test_update_entries_share_one_release_menu():
    nav = _nav_source()
    assert nav.count('data-tab="release"') == 1
    assert "版本发布" in nav
    assert 'data-tab="hu"' not in nav
    assert 'data-tab="apk"' not in nav


def test_release_menu_keeps_two_internal_publish_tabs():
    source = HTML.read_text(encoding="utf-8")
    assert 'data-release-tab="hu"' in source
    assert 'data-release-tab="apk"' in source
    assert 'id="tab-release"' in source
    assert "function switchReleaseTab" in source
