"""发布器的迁移门禁契约（Task 1 审查 Important-4）。

要求：
1. `up -d --build` 之前必须先显式跑一次性 `schema-migrations`；
2. 迁移失败必须阻止 `up`（API 不启动）；
3. 启动前检查发布器依赖的每个脚本/编排文件真实存在。
"""

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class FakeRunner:
    """记录调用顺序；fail_on 命中的命令返回 False，用来模拟迁移失败。"""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, cmd, cwd, input_text=None):
        rendered = " ".join(str(part) for part in cmd) if not isinstance(cmd, str) else cmd
        self.calls.append((rendered.replace("\\", "/").lower(), str(cwd)))
        if self.fail_on and self.fail_on in rendered:
            return False
        return True


@pytest.fixture
def release_module():
    return importlib.import_module("tools.release_all")


@pytest.fixture
def gate_only(release_module, monkeypatch):
    """只测迁移顺序时跳过存在性检查。

    `tools/build_web_assets.py` 由并行的网页 agent 分支落地，本分支尚未包含；
    那是存在性检查该报的东西，由下面的存在性用例负责，不该污染顺序断言。
    """
    monkeypatch.setattr(release_module, "required_scripts", lambda root: [])
    monkeypatch.setattr(release_module, "required_deploy_files", lambda root: [])
    return release_module


def run_release(release_module, runner, deploy=True, argv=None):
    old_root = release_module.ROOT
    old_py = release_module.PY
    old_run = release_module.run
    release_module.ROOT = str(ROOT)
    release_module.PY = "python"
    release_module.run = runner
    old_argv = sys.argv
    sys.argv = argv or ["release_all.py"] + (["--deploy"] if deploy else [])
    try:
        release_module.main()
    finally:
        release_module.ROOT = old_root
        release_module.PY = old_py
        release_module.run = old_run
        sys.argv = old_argv


def _first_index(calls, needle):
    for index, (rendered, _cwd) in enumerate(calls):
        if needle in rendered:
            return index
    return None


def test_release_runs_schema_migrations_before_compose_up(gate_only):
    runner = FakeRunner()
    run_release(gate_only, runner)

    migration = _first_index(runner.calls, "run --rm schema-migrations")
    up = _first_index(runner.calls, "up -d --build")
    assert migration is not None, runner.calls
    assert up is not None, runner.calls
    assert migration < up, f"迁移必须先于 up 执行：{runner.calls}"
    assert "--remove-orphans" in runner.calls[up][0], \
        f"已从 Compose 移除的后台服务必须随发布停止：{runner.calls[up]}"


def test_migration_failure_blocks_compose_up(gate_only):
    runner = FakeRunner(fail_on="run --rm schema-migrations")
    with pytest.raises(SystemExit) as excinfo:
        run_release(gate_only, runner)

    assert "schema-migrations" in str(excinfo.value)
    assert _first_index(runner.calls, "up -d --build") is None, \
        f"迁移失败后不应再执行 up：{runner.calls}"


def test_release_check_covers_every_script_it_invokes(release_module):
    """存在性检查必须覆盖 main() 真正会执行的每一个仓库内脚本。"""
    required = {Path(path).name for path in release_module.required_scripts(ROOT)}
    source = (ROOT / "tools" / "release_all.py").read_text(encoding="utf-8")
    invoked = set(re.findall(r'script_path\(root,\s*"([^"]+)"\)', source))
    assert invoked, "发布器应通过 script_path 声明它调用的脚本"
    assert invoked <= required, f"这些脚本没进存在性检查：{invoked - required}"
    # 关键脚本确实存在。
    for name in ("build_backend_zip.py", "build_web_assets.py"):
        assert name in required
        assert (ROOT / "tools" / name).is_file(), f"{name} 必须存在"


def test_release_deploy_entry_files_exist(release_module):
    """部署入口依赖的编排文件与迁移脚本必须真实存在。"""
    for path in release_module.required_deploy_files(ROOT):
        assert Path(path).is_file(), f"部署入口引用了不存在的文件：{path}"


def test_missing_script_fails_before_running_anything(release_module, tmp_path, monkeypatch):
    """缺脚本时必须在跑任何东西之前就报清楚，而不是等 subprocess 失败。"""
    fake_root = tmp_path / "repo"
    (fake_root / "tools").mkdir(parents=True)
    (fake_root / "deploy" / "selfhosted").mkdir(parents=True)
    (fake_root / "deploy" / "selfhosted" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(release_module, "run", lambda *a, **k: calls.append(a) or True)
    monkeypatch.setattr(sys, "argv", ["release_all.py", "--deploy"])
    old_root = release_module.ROOT
    release_module.ROOT = str(fake_root)
    try:
        with pytest.raises(SystemExit) as excinfo:
            release_module.main()
    finally:
        release_module.ROOT = old_root
    assert "build_web_assets.py" in str(excinfo.value)
    assert calls == [], f"存在性检查失败时不应执行任何命令：{calls}"


def test_deploy_reloads_nginx_so_new_routes_take_effect(gate_only, monkeypatch):
    """Validate and remount nginx's bind-mounted file before reloading it."""
    runner = FakeRunner()
    run_release(gate_only, runner, deploy=True)

    rendered = [c for c, _cwd in runner.calls]
    config_test_index = next(
        (i for i, c in enumerate(rendered) if "run --rm --no-deps nginx nginx -t" in c), None)
    recreate_index = next(
        (i for i, c in enumerate(rendered)
         if "up -d --force-recreate --no-deps nginx" in c), None)
    reload_index = next(
        (i for i, c in enumerate(rendered) if "nginx" in c and "reload" in c), None)
    assert config_test_index is not None, f"发布流程没有检查新 nginx 配置：{rendered}"
    assert recreate_index is not None, f"发布流程没有重新挂载 nginx 配置：{rendered}"
    assert reload_index is not None, f"发布流程没有 reload nginx：{rendered}"
    up_index = next(i for i, c in enumerate(rendered) if " up " in f" {c} ")
    assert up_index < config_test_index < recreate_index < reload_index, \
        f"nginx 检查、重建和 reload 顺序错误：{rendered}"


@pytest.mark.parametrize(
    ("fail_on", "error_message", "later_command"),
    [
        ("nginx -t", "nginx 配置检查失败", "up -d --force-recreate"),
        ("up -d --force-recreate --no-deps nginx", "nginx 容器重建失败", "nginx -s reload"),
    ],
)
def test_nginx_preflight_or_recreate_failure_stops_switch(gate_only, fail_on,
                                                           error_message, later_command):
    runner = FakeRunner(fail_on=fail_on)
    with pytest.raises(SystemExit, match=error_message):
        run_release(gate_only, runner)
    assert _first_index(runner.calls, later_command) is None, runner.calls


def test_nginx_reload_failure_marks_release_failed(gate_only):
    runner = FakeRunner(fail_on="nginx -s reload")
    with pytest.raises(SystemExit, match="nginx reload 失败"):
        run_release(gate_only, runner)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
