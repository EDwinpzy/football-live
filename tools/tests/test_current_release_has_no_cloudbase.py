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
    assert 'OTV_VOD_CLOUD_FALLBACK: "0"' in compose


def test_web_proxy_keeps_the_internal_metadata_api_endpoint():
    compose = (ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8")
    assert "OTV_METADATA_API_URL: http://metadata-api:8094" in compose


def test_every_cloudbase_default_in_web_proxy_is_overridden_by_the_selfhosted_stack():
    """proxy.py 的每个 CloudBase 默认值都必须被自建栈显式覆盖。

    `test_current_release_and_apk_runtime_config_has_no_cloudbase_paths` 把
    compose/.env.example/proxy_runner 都纳入了 CloudBase 检查，**唯独漏了 proxy.py**，
    而它至今还有 4 处 CloudBase 兜底地址（通用中继、APK 更新对象存储、卡密激活、
    IPTV 直播列表）。

    目前这 4 处在自建栈下都不会被用到：要么 compose 配了内网地址，要么被
    `OTV_CLOUD_RELAY=0` 关掉。但它们是「万一漏配就静默回落到已退役云端」的隐患
    默认值——自建栈少配一个环境变量，激活请求就会悄悄打到要下线的 CloudBase 上，
    而且日志看起来完全正常。

    本测试不要求删掉这些默认值（设备/本地形态仍可能需要），而是要求
    **自建栈必须显式覆盖每一处**，把「依赖默认值」变成「不可能发生」。
    """
    compose = (ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8")

    # 每处默认值都必须在 compose 里有对应的内网覆盖。
    required_overrides = {
        "OTV_LICENSE_ENDPOINT": "http://business-api:8082/activate",
        "OTV_APK_UPDATE_OBJECT_BASE": "http://storage:8092/v1/storages/object/hotupdate",
        "OTV_IPTV_LATEST_URL": "http://business-api:8082/iptv/latest",
    }
    for key, expected in required_overrides.items():
        assert f"{key}: {expected}" in compose, (
            f"compose.yaml 缺少 {key}={expected}；"
            f"漏配会让 proxy.py 回落到已退役的 CloudBase 默认地址"
        )

    # 通用中继默认是开（"1"），自建栈必须显式关掉。
    assert 'OTV_CLOUD_RELAY: "0"' in compose, (
        "compose.yaml 必须显式设置 OTV_CLOUD_RELAY=0，"
        "否则 proxy.py 的 CLOUD_RELAY_ENABLED 默认值为 true，"
        "会把比赛/直播请求中继到已退役的 CloudBase。"
    )
    assert 'OTV_VOD_CLOUD_FALLBACK: "0"' in compose, (
        "compose.yaml 必须显式设置 OTV_VOD_CLOUD_FALLBACK=0，"
        "否则片源回退会沿用通用中继开关并请求已退役的 CloudBase。"
    )

    # proxy.py 确实还有这些默认值——本测试是防回归的护栏，不是描述现状。
    # 若将来有人彻底删掉这些默认值，应同步更新本测试的意图说明。
    web_proxy = (ROOT / "tools/backend-src/proxy.py").read_text(encoding="utf-8")
    assert web_proxy.count("service.tcloudbase.com") > 0, (
        "proxy.py 里已无 CloudBase 默认地址；若这是有意清理，"
        "请把本测试改为直接断言 proxy.py 不含任何 CloudBase 地址。"
    )


def test_one_time_migration_helpers_are_not_mounted_into_runtime_containers():
    compose = (ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8")
    admin_dockerfile = (ROOT / "deploy/selfhosted/Dockerfile.admin").read_text(encoding="utf-8")
    assert "../../tools/selfhosted:/app/tools/selfhosted" not in compose
    assert "import_cloudbase.py" not in compose
    assert "database_reconcile.py" not in compose
    assert "import_cloudbase.py" not in admin_dockerfile
    assert "database_reconcile.py" not in admin_dockerfile
