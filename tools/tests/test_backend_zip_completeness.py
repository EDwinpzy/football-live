import ast
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tools" / "backend-src"


def _whitelisted_names():
    """读出 build_backend_zip.py 里手工维护的 REPL 白名单。"""
    import importlib.util
    import sys
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    spec = importlib.util.spec_from_file_location(
        "bbz", ROOT / "tools" / "build_backend_zip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name.split(".")[0] for name in module.REPL}


def _local_imports(path: Path) -> set:
    """模块**顶层**的同包 import 目标（只保留 SRC 下确实存在的 .py）。

    只看顶层是刻意的：函数体内的 import 是延迟加载，通常带降级路径
    （如 proxy.py 里 `import decrypt_python` 失败时退回 quickjs），
    不打进 zip 不会让后端起不来。顶层 import 则是一旦缺失就直接
    ModuleNotFoundError 崩在启动阶段。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name.split(".")[0])
    return {n for n in names if (SRC / (n + ".py")).exists()}


BACKEND_ZIPS = (
    ROOT / "android/app/src/main/assets/backend/python-backend.zip",
    ROOT / "internal/mobile/app/src/main/assets/backend/python-backend.zip",
)


class BackendZipCompletenessTests(unittest.TestCase):
    """客户端后端 ZIP 只包含足球直播所需模块及其静态网页。"""

    def test_runtime_whitelist_contains_only_live_modules(self):
        self.assertEqual(_whitelisted_names(), {"proxy", "vlc_hls", "team_backdrop"})

    def test_every_local_import_of_a_whitelisted_module_is_packaged(self):
        whitelist = _whitelisted_names()
        missing = {}
        for name in sorted(whitelist):
            path = SRC / (name + ".py")
            if not path.exists():
                continue
            for dependency in sorted(_local_imports(path)):
                if dependency not in whitelist:
                    missing.setdefault(name, []).append(dependency)
        self.assertEqual(
            missing, {},
            "这些同包依赖没进白名单，打出来的 zip 会在真机上 ImportError：%s" % missing)

    def test_embedded_backend_zips_contain_no_nonfootball_runtime_modules(self):
        allowed = {name + ".py" for name in _whitelisted_names()}
        for archive in BACKEND_ZIPS:
            if not archive.exists():
                continue
            with zipfile.ZipFile(archive) as runtime:
                root_modules = {
                    name for name in runtime.namelist()
                    if "/" not in name and name.lower().endswith((".py", ".json", ".js"))
                }
            self.assertEqual(root_modules, allowed, f"{archive} 含有非直播 runtime 文件")


if __name__ == "__main__":
    unittest.main()
