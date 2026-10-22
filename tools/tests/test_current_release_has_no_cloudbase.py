import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, cwd, input_text=None):
        self.calls.append((cmd, str(cwd), input_text))
        return True


@pytest.fixture

def release_root():
    return ROOT


@pytest.fixture

def fake_runner():
    return FakeRunner()


def run_release(release_root, deploy, fake_runner, monkeypatch):
    release = importlib.import_module("tools.release_all")
    monkeypatch.setattr(release, "ROOT", str(release_root))
    monkeypatch.setattr(release, "PY", "python")
    monkeypatch.setattr(release, "run", fake_runner)
    monkeypatch.setattr(sys, "argv", ["release_all.py", *( ["--deploy"] if deploy else [])])
    release.main()
    return release


@pytest.fixture
def real_script_runner(monkeypatch):
    """保留真实存在性检查，只把耗时的构建子进程换成记录器。"""
    runner = FakeRunner()
    release = importlib.import_module("tools.release_all")
    monkeypatch.setattr(release, "run", runner)
    return runner


def test_release_deploy_never_invokes_cloudbase_cli(release_root, fake_runner, monkeypatch):
    # 跳过存在性检查：它由 test_release_migration_gate.py 单独覆盖。
    monkeypatch.setattr(importlib.import_module("tools.release_all"),
                        "required_scripts", lambda root: [])
    monkeypatch.setattr(importlib.import_module("tools.release_all"),
                        "required_deploy_files", lambda root: [])
    run_release(release_root, deploy=True, fake_runner=fake_runner, monkeypatch=monkeypatch)

    assert not any(
        "tcb" in str(call).lower() or "cloudbase" in str(call).lower()
        for call in fake_runner.calls
    )


def test_release_build_inventory_and_deploy_target_are_selfhosted(release_root, fake_runner, monkeypatch):
    monkeypatch.setattr(importlib.import_module("tools.release_all"),
                        "required_scripts", lambda root: [])
    monkeypatch.setattr(importlib.import_module("tools.release_all"),
                        "required_deploy_files", lambda root: [])
    run_release(release_root, deploy=True, fake_runner=fake_runner, monkeypatch=monkeypatch)
    rendered = [
        " ".join([*(str(part) for part in command), str(cwd), str(input_text or "")])
        .replace("\\", "/").lower()
        for command, cwd, input_text in fake_runner.calls
    ]

    assert any("build_backend_zip.py" in call for call in rendered)
    assert any("build_web_assets.py" in call for call in rendered)
    assert not any("build_web_function.py" in call for call in rendered)
    assert not any("cloudfunctions/" in call for call in rendered)
    assert any("deploy/selfhosted/compose.yaml" in call and "compose" in call and "up" in call
               for call in rendered), rendered


def test_release_actually_rejects_a_missing_build_script(release_root, monkeypatch, tmp_path):
    """存在性检查必须真的执行：拿掉一个脚本就必须在跑任何命令前失败。"""
    release = importlib.import_module("tools.release_all")
    runner = FakeRunner()
    monkeypatch.setattr(release, "run", runner)
    monkeypatch.setattr(release, "required_scripts",
                        lambda root: [str(tmp_path / "definitely_missing_script.py")])
    monkeypatch.setattr(release, "required_deploy_files", lambda root: [])
    monkeypatch.setattr(sys, "argv", ["release_all.py", "--deploy"])

    with pytest.raises(SystemExit) as excinfo:
        release.main()

    assert "definitely_missing_script.py" in str(excinfo.value)
    assert runner.calls == [], f"缺脚本时不应执行任何构建命令：{runner.calls}"


def test_current_release_and_apk_runtime_config_has_no_cloudbase_paths():
    paths = (
        ROOT / "tools/release_all.py",
        ROOT / "tools/apk_update.py",
        ROOT / "android/app/src/main/python/proxy_runner.py",
        ROOT / "internal/mobile/app/src/main/python/proxy_runner.py",
        ROOT / "deploy/selfhosted/compose.yaml",
        ROOT / "deploy/selfhosted/.env.example",
    )
    forbidden = ("tcb ", "tcloudbase", "otv_cloud:",
                 "cloudbase api", "cloudbase admin", "cloudbase relay")
    for path in paths:
        content = path.read_text(encoding="utf-8").lower()
        for marker in forbidden:
            assert marker not in content, f"{path.relative_to(ROOT)} still contains {marker!r}"

    compose = (ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8")
    assert "OTV_APK_UPDATE_OBJECT_BASE: http://storage:8092/v1/storages/object/hotupdate" in compose
    assert 'OTV_CLOUD_RELAY: "0"' in compose


def test_football_proxy_contains_no_cloudbase_fallbacks():
    """Removed application features must not leave CloudBase fallbacks in the live proxy."""
    web_proxy = (ROOT / "tools/backend-src/proxy.py").read_text(encoding="utf-8")
    assert "service.tcloudbase.com" not in web_proxy
    assert "OTV_LICENSE_ENDPOINT" not in web_proxy
    assert "OTV_IPTV_LATEST_URL" not in web_proxy
    assert "OTV_VOD_CLOUD_FALLBACK" not in web_proxy


def test_non_football_admin_and_metadata_runtime_surfaces_are_removed():
    compose = (ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8")
    nginx = (ROOT / "deploy/selfhosted/nginx.conf").read_text(encoding="utf-8")
    assert "admin:" not in compose
    assert "metadata-api:" not in compose
    assert "metadata-builder:" not in compose
    assert "metadata-refresh:" not in compose
    assert "/admin/" not in nginx
    assert "/metadata/" not in nginx
    assert not (ROOT / "tools/admin_server.py").exists()
    assert not (ROOT / "tools/admin.html").exists()
