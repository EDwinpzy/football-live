import ast
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tools" / "backend-src"
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _whitelisted_names():
    module = _load_module("bbz", TOOLS / "build_backend_zip.py")
    return {name.removesuffix(".py") for name in module.REPL}


def _local_imports(path: Path) -> set:
    """Return top-level same-package imports that must be in the runtime ZIP."""
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
    return {name for name in names if (SRC / (name + ".py")).exists()}


class BackendZipCompletenessTests(unittest.TestCase):
    """The native archive contains only live runtime code and client assets."""

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
            missing,
            {},
            "these same-package imports are absent from the runtime ZIP: %s" % missing,
        )

    def test_native_asset_builder_excludes_retired_web_ui(self):
        build_assets = _load_module(
            "native_assets", TOOLS / "build_web_assets.py"
        ).build_assets
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dist"
            result = build_assets(SRC / "www", output)
            self.assertEqual(37, len(result.copied_files))
            self.assertEqual(
                {f"assets/football/fb-{index:02d}.jpg" for index in range(1, 37)}
                | {"assets/logo.png"},
                set(result.copied_files),
            )
            for retired in (
                "index.html",
                "app.js",
                "style.css",
                "manifest.webmanifest",
                "assets/app.77db0bc59c3ee053.js",
                "assets/style.072301a4b36d4b31.css",
            ):
                self.assertFalse((output / retired).exists(), retired)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("native-compatibility-assets", manifest["kind"])

    def test_backend_archive_contains_only_live_runtime_and_native_assets(self):
        builder = _load_module("bbz_build", TOOLS / "build_backend_zip.py")
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "python-backend.zip"
            entries = builder.build_archive(SRC, archive_path)
            allowed_runtime = set(builder.REPL)
            allowed_static = {
                name for name in entries
                if name == "www/manifest.json" or name.startswith("www/assets/")
            }
            self.assertEqual(set(entries), allowed_runtime | allowed_static)
            self.assertEqual(36, sum(
                1 for name in entries if name.startswith("www/assets/football/fb-")
            ))
            self.assertIn("www/assets/logo.png", entries)
            with zipfile.ZipFile(archive_path) as archive:
                for retired in (
                    "www/index.html",
                    "www/app.js",
                    "www/style.css",
                    "www/manifest.webmanifest",
                ):
                    self.assertNotIn(retired, archive.namelist())


if __name__ == "__main__":
    unittest.main()
