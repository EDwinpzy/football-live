import ast
import io
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


APK_PATHS = ("apk/TV版/MyTV-TV版.apk", "apk/移动版/MyTV-移动版.apk")


class BackendZipCompletenessTests(unittest.TestCase):
    """打包白名单必须覆盖所有被导入的同包模块。

    build_backend_zip.py 的 REPL 是手工维护的文件白名单。hhkan.py 里有
    `import source_dns`，但白名单漏了它，于是打出来的 python-backend.zip
    缺这个文件。App 内嵌后端启动时直接
    `ModuleNotFoundError: No module named 'source_dns'`，整个 Python 后端
    起不来，影视详情、播放、片源全部失效——而包能正常构建、能发布，
    崩溃只在真机启动时才出现。
    """

    def test_source_dns_is_in_the_whitelist(self):
        self.assertIn(
            "source_dns", _whitelisted_names(),
            "source_dns.py 被 hhkan.py 导入，必须打进后端 zip")

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

    def test_published_apk_embeds_a_complete_backend_zip(self):
        """直接查已发布的 APK，别只验证白名单。"""
        for relative in APK_PATHS:
            apk = ROOT / relative
            if not apk.exists():
                continue
            with zipfile.ZipFile(apk) as outer:
                with outer.open("assets/backend/python-backend.zip") as handle:
                    with zipfile.ZipFile(io.BytesIO(handle.read())) as inner:
                        names = inner.namelist()
            self.assertIn(
                "source_dns.py", names,
                "%s 的内嵌后端 zip 缺 source_dns.py" % apk.name)


if __name__ == "__main__":
    unittest.main()
