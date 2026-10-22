"""Windows 打包契约：backend.zip 内容、hls.js 本地化、图标与 PyInstaller 参数。

这些测试不跑 PyInstaller。bundle 模式用真实的 tools/backend-src 源码跑一遍
build_backend_zip()，锁定：
  1. zip 内含全部足球直播后端必需文件与完整 www；
  2. 不含 tests / 缓存 / Linux .so / 密钥；
  3. hls.js@1.5.13 落到 zip 内并在 index.html 改写为相对引用；
  4. logo.png 转换出多尺寸 .ico；
  5. exe 构建参数是 --onefile --windowed 并带上数据与 hidden-import。
"""
import hashlib
import importlib.util
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


DESKTOP_DIR = Path(__file__).resolve().parents[1]
ROOT = DESKTOP_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DESKTOP_DIR))

spec = importlib.util.spec_from_file_location(
    "desktop_build", DESKTOP_DIR / "build.py")
build = importlib.util.module_from_spec(spec)
sys.modules["desktop_build"] = build
spec.loader.exec_module(build)


class BackendZipContractTest(unittest.TestCase):
    """backend.zip 是 Windows 版唯一的内嵌后端来源，坏一个文件 EXE 就残废。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "backend.zip"
        cls.result = build.build_backend_zip(cls.out, allow_network=True)
        cls.names = set(zipfile.ZipFile(cls.out).namelist())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_contains_every_backend_module_and_data_file(self):
        # BACKEND_FILES 复用 tools/build_backend_zip.py 的清单，任何硬依赖新增
        # 必须同步登记（与 Android 热更包同一约束）。
        for name in build.BACKEND_FILES:
            self.assertIn(name, self.names, f"backend.zip 缺少 {name}")

    def test_excludes_nonfootball_runtime_modules(self):
        runtime_modules = {
            name for name in self.names
            if "/" not in name and name.lower().endswith((".py", ".json", ".js"))
        }
        self.assertEqual(runtime_modules, {"proxy.py", "vlc_hls.py", "team_backdrop.py"})

    def test_contains_whole_web_client(self):
        for name in ("www/index.html", "www/app.js", "www/style.css",
                     "www/manifest.json", "www/manifest.webmanifest",
                     "www/assets/logo.png"):
            self.assertIn(name, self.names, f"backend.zip 缺少 {name}")

    def test_index_html_uses_content_hashed_bundle(self):
        html = zipfile.ZipFile(self.out).read("www/index.html").decode("utf-8")
        self.assertNotIn('src="app.js', html)
        self.assertNotIn('href="style.css', html)
        self.assertRegex(html, r'src="assets/app\.[0-9a-f]{16}\.js"')
        self.assertRegex(html, r'href="assets/style\.[0-9a-f]{16}\.css"')

    def test_bundles_hls_js_locally_and_rewrites_index(self):
        self.assertIn("www/assets/hls.min.js", self.names)
        data = zipfile.ZipFile(self.out).read("www/assets/hls.min.js")
        self.assertEqual(hashlib.sha256(data).hexdigest(), build.HLS_SPEC.sha256)
        html = zipfile.ZipFile(self.out).read("www/index.html").decode("utf-8")
        self.assertIn('src="assets/hls.min.js"', html)
        # 打包产物不允许还留着 CDN 引用，否则断网/无墙即无 HLS 播放能力。
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertIn("1.5.13", data.decode("utf-8"))

    def test_source_www_is_never_mutated(self):
        source = ROOT / "tools" / "backend-src" / "www"
        html = (source / "index.html").read_text(encoding="utf-8")
        self.assertIn("cdn.jsdelivr.net/npm/hls.js@1.5.13", html)
        self.assertFalse((source / "assets" / "hls.min.js").exists())

    def test_excludes_tests_caches_linux_binaries_and_secrets(self):
        for name in self.names:
            self.assertNotIn("__pycache__", name)
            self.assertNotIn("tests/", name)
            self.assertFalse(name.endswith((".so", ".pyd", ".pem", ".key")), name)
        # 运行期缓存永不进包
        self.assertNotIn("team_icon_cache.json", self.names)
        # Runtime dependencies are imported from Python source; no vendored platform wheel is needed.
        self.assertFalse(any(name.startswith("vendor/") for name in self.names))

    def test_zip_entries_are_deterministic(self):
        with TemporaryDirectory() as tmp:
            again = Path(tmp) / "backend.zip"
            build.build_backend_zip(again, allow_network=True,
                                    hls_cache=self.result.hls_cache)
            self.assertEqual(hashlib.sha256(self.out.read_bytes()).hexdigest(),
                             hashlib.sha256(again.read_bytes()).hexdigest())


class HlsFetchContractTest(unittest.TestCase):
    """缺网络必须报错，不能产出播放残废的 EXE。"""

    def test_missing_cdn_fails_the_build(self):
        with self.assertRaises(build.HlsDownloadError):
            build.fetch_hls(build.HLS_SPEC, cache_path=Path("unused"),
                            opener=mock.Mock(side_effect=OSError("no network")))

    def test_wrong_version_is_rejected(self):
        payload = b'!function(){a={key:"version",get:function(){return"9.9.9"}}}();'
        with self.assertRaises(build.HlsDownloadError):
            build.fetch_hls(build.HlsSpec(
                url="https://example.invalid/hls.min.js", version="1.5.13",
                sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)),
                cache_path=Path("unused"),
                opener=mock.Mock(return_value=payload))

    def test_html_error_page_is_rejected(self):
        with self.assertRaises(build.HlsDownloadError):
            build._verify_hls_version(b"<html>404 Not Found</html>")

    def test_cached_copy_is_verified_before_use(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "hls.min.js"
            cache.write_bytes(b"tampered")
            with self.assertRaises(build.HlsDownloadError):
                build.fetch_hls(build.HLS_SPEC, cache_path=cache,
                                opener=mock.Mock(return_value=b"fresh"))

    def test_version_is_read_from_the_bundle_itself(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "hls.min.js"
            # 与真实 minified 产物同形：版本号就在 version getter 里。
            payload = b'!function(){a={key:"version",get:function(){return"1.5.13"}}}();'
            spec = build.HlsSpec(
                url="https://example.invalid/hls.min.js", version="1.5.13",
                sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
            asset = build.fetch_hls(spec, cache_path=cache,
                                    opener=mock.Mock(return_value=payload))
            self.assertEqual(cache.read_bytes(), payload)
            self.assertEqual(asset.version, "1.5.13")


class IconContractTest(unittest.TestCase):
    def test_logo_becomes_a_multi_size_ico(self):
        with TemporaryDirectory() as tmp:
            ico = build.write_icon(Path(tmp) / "FootballLive.ico",
                                   ROOT / "tools" / "backend-src" / "www" / "assets" / "logo.png")
            data = ico.read_bytes()
            self.assertEqual(data[:4], b"\x00\x00\x01\x00")
            # 16/24/32/48/64/128/256 七档，Windows 资源管理器各缩放档都要有。
            self.assertEqual(int.from_bytes(data[4:6], "little"), 7)


class PyInstallerCommandContractTest(unittest.TestCase):
    def test_command_matches_the_launcher_data_contract(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle = tmp_path / "desktop"
            bundle.mkdir()
            (bundle / "backend.zip").write_bytes(b"PK\x03\x04")
            (bundle / "mytv_migration_ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")
            launcher = DESKTOP_DIR / "launcher.py"
            launcher.touch()
            ico = build.write_icon(tmp_path / "FootballLive.ico",
                                   ROOT / "tools" / "backend-src" / "www" / "assets" / "logo.png")
            argv = build.pyinstaller_argv(
                launcher=launcher, dist_dir=tmp_path / "dist",
                work_dir=tmp_path / "build", bundle_dir=bundle, icon=ico)
            joined = " ".join(argv)
            # PyInstaller 的开关与值是两个 token，这里解析成字典再断言，
            # 比按固定下标切片稳。
            options: dict[str, list[str]] = {}
            for index, token in enumerate(argv[:argv.index(str(launcher))]):
                if token.startswith("--"):
                    options.setdefault(token, [])
                    for value in argv[index + 1:]:
                        if value.startswith("--") or value == str(launcher):
                            break
                        options[token].append(value)
            self.assertIn("--onefile", argv)
            self.assertIn("--windowed", argv)
            self.assertEqual(options["--name"], ["FootballLive"])
            self.assertNotIn("--collect-all", options)
            self.assertIn("webview.platforms.qt", options["--exclude-module"])
            self.assertIn("webview.platforms.gtk", options["--exclude-module"])
            hidden = options["--hidden-import"]
            for module in ("webview.platforms.edgechromium",
                           "urllib.request", "html"):
                self.assertIn(module, hidden)
            self.assertNotIn("quickjs", hidden)
            self.assertNotIn("proxy", hidden)
            self.assertEqual(len(options["--icon"]), 1)
            self.assertEqual(sorted(part.split(";")[-1] for part in options["--add-data"]),
                             ["desktop", "desktop"])
            self.assertIn("backend.zip", options["--add-data"][0])
            self.assertIn("mytv_migration_ca.pem", options["--add-data"][1])
            self.assertIn("FootballLive.ico", options["--icon"][0])
            self.assertNotIn("tools/keys", joined)
            self.assertNotIn(".learnings", joined)
            self.assertEqual(options["--distpath"], [str(tmp_path / "dist")])
            self.assertEqual(options["--workpath"], [str(tmp_path / "build")])
            self.assertEqual(argv[-1], str(launcher))

    def test_missing_runtime_dependencies_are_reported(self):
        with self.assertRaises(SystemExit) as ctx:
            build.require_runtime_dependencies(["definitely_not_installed_pkg"])
        self.assertIn("definitely_not_installed_pkg", str(ctx.exception))

    def test_pinned_runtime_versions(self):
        text = (DESKTOP_DIR / "requirements-build.txt").read_text(encoding="utf-8")
        for pin in ("pywebview==6.2.1", "pyinstaller==6.19.0"):
            self.assertIn(pin, text)
        self.assertNotIn("quickjs", text)


if __name__ == "__main__":
    unittest.main()
