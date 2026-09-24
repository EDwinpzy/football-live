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


def test_release_deploy_never_invokes_cloudbase_cli(release_root, fake_runner, monkeypatch):
    run_release(release_root, deploy=True, fake_runner=fake_runner, monkeypatch=monkeypatch)

    assert not any(
        "tcb" in str(call).lower() or "cloudbase" in str(call).lower()
        for call in fake_runner.calls
    )


def test_release_build_inventory_and_deploy_target_are_selfhosted(release_root, fake_runner, monkeypatch):
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


def test_one_time_migration_helpers_are_not_mounted_into_runtime_containers():
    compose = (ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8")
    admin_dockerfile = (ROOT / "deploy/selfhosted/Dockerfile.admin").read_text(encoding="utf-8")
    assert "../../tools/selfhosted:/app/tools/selfhosted" not in compose
    assert "import_cloudbase.py" not in compose
    assert "database_reconcile.py" not in compose
    assert "import_cloudbase.py" not in admin_dockerfile
    assert "database_reconcile.py" not in admin_dockerfile
