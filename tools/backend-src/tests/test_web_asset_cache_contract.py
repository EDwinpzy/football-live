"""Behavioral contracts for deterministic Web assets and APK packaging."""
import hashlib
import importlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_builder():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.build_web_assets")


def _write_source(source_dir: Path, *, app: bytes, css: bytes) -> None:
    (source_dir / "assets").mkdir(parents=True, exist_ok=True)
    (source_dir / "index.html").write_text(
        '<!doctype html><html><head>'
        '<link rel="stylesheet" href="style.css?v=131">'
        '</head><body><script src="app.js?v=134" defer></script>'
        '</body></html>',
        encoding="utf-8",
    )
    (source_dir / "app.js").write_bytes(app)
    (source_dir / "style.css").write_bytes(css)
    (source_dir / "manifest.webmanifest").write_text('{"name":"My TV"}\n', encoding="utf-8")
    (source_dir / "assets" / "logo.svg").write_text("<svg/>\n", encoding="utf-8")


def test_build_assets_is_stable_and_emits_a_cache_contract(tmp_path):
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    _write_source(source, app=b"console.log('one');\n", css=b"body { color: white; }\n")

    first = build_assets(source, dist)
    first_files = {path: (dist / path).read_bytes() for path in first.referenced_files}
    second = build_assets(source, dist)

    assert first.manifest == second.manifest
    assert first.referenced_files == second.referenced_files
    assert all((dist / path).is_file() for path in second.referenced_files)
    assert {path: (dist / path).read_bytes() for path in second.referenced_files} == first_files

    app_path = next(path for path in second.referenced_files if path.endswith(".js"))
    css_path = next(path for path in second.referenced_files if path.endswith(".css"))
    app_digest = hashlib.sha256(b"console.log('one');\n").hexdigest()[:16]
    css_digest = hashlib.sha256(b"body { color: white; }\n").hexdigest()[:16]
    assert app_path == f"assets/app.{app_digest}.js"
    assert css_path == f"assets/style.{css_digest}.css"

    html = (dist / "index.html").read_text(encoding="utf-8")
    assert f'href="{css_path}"' in html
    assert f'src="{app_path}"' in html
    assert "style.css?v=" not in html
    assert "app.js?v=" not in html

    manifest = json.loads((dist / "asset-manifest.json").read_text(encoding="utf-8"))
    assert manifest["cacheControl"]["/index.html"] == "no-store"
    assert manifest["cacheControl"][f"/{app_path}"] == "public, max-age=31536000, immutable"
    assert manifest["cacheControl"][f"/{css_path}"] == "public, max-age=31536000, immutable"
    assert manifest["defaultCacheControl"] == "public, max-age=300"
    assert (dist / "assets" / "logo.svg").is_file()
    assert (dist / "manifest.webmanifest").is_file()


def test_changed_source_gets_a_new_url_without_overwriting_the_old_asset(tmp_path):
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    _write_source(source, app=b"window.revision = 1;\n", css=b"body { color: white; }\n")

    old_build = build_assets(source, dist)
    old_app = next(path for path in old_build.referenced_files if path.endswith(".js"))
    old_bytes = (dist / old_app).read_bytes()
    (source / "app.js").write_bytes(b"window.revision = 2;\n")

    new_build = build_assets(source, dist)
    new_app = next(path for path in new_build.referenced_files if path.endswith(".js"))

    assert new_app != old_app
    assert (dist / old_app).read_bytes() == old_bytes
    assert (dist / new_app).read_bytes() == b"window.revision = 2;\n"
    assert new_app in (dist / "index.html").read_text(encoding="utf-8")
    assert old_app not in (dist / "index.html").read_text(encoding="utf-8")

    (source / "app.js").write_bytes(b"window.revision = 3;\n")
    latest_build = build_assets(source, dist)
    latest_app = next(path for path in latest_build.referenced_files if path.endswith(".js"))
    assert latest_app != new_app
    assert (dist / new_app).is_file()
    assert not (dist / old_app).exists()


def test_backend_zip_contains_the_hashed_web_bundle(tmp_path, monkeypatch):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    build_zip = importlib.import_module("tools.build_backend_zip")
    source = tmp_path / "backend-src"
    (source / "www" / "assets").mkdir(parents=True)
    (source / "www" / "index.html").write_text(
        '<link rel="stylesheet" href="style.css?v=old">'
        '<script src="app.js?v=old"></script>', encoding="utf-8")
    (source / "www" / "app.js").write_text("window.app = 'current';\n", encoding="utf-8")
    (source / "www" / "style.css").write_text("body { color: white; }\n", encoding="utf-8")
    (source / "www" / "manifest.webmanifest").write_text('{"name":"My TV"}\n', encoding="utf-8")
    (source / "www" / "assets" / "logo.svg").write_text("<svg/>\n", encoding="utf-8")
    for name in build_zip.REPL:
        (source / name).write_text(f"{name}:current\n", encoding="utf-8")

    input_zip = tmp_path / "input.zip"
    mobile_zip = tmp_path / "mobile.zip"
    temp_zip = tmp_path / "temporary.zip"
    with zipfile.ZipFile(input_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in build_zip.REPL:
            archive.writestr(name, f"{name}:old\n")
        archive.writestr("www/index.html", "old html")
        archive.writestr("www/app.js", "old app")
        archive.writestr("www/style.css", "old css")
        archive.writestr("www/assets/app.0000000000000000.js", "stale app")
        archive.writestr("www/assets/style.0000000000000000.css", "stale css")

    monkeypatch.setattr(build_zip, "SRC_DIR", str(source))
    monkeypatch.setattr(build_zip, "SRC_ZIP", str(input_zip))
    monkeypatch.setattr(build_zip, "MOBILE_ZIP", str(mobile_zip))
    monkeypatch.setattr(build_zip, "TMP_ZIP", str(temp_zip))
    monkeypatch.setattr(build_zip, "POSTER_DIR", str(tmp_path / "no-posters"))

    build_zip.main()
    first_archive = input_zip.read_bytes()
    build_zip.main()

    with zipfile.ZipFile(input_zip) as archive:
        names = set(archive.namelist())
        html = archive.read("www/index.html").decode("utf-8")
        app_refs = [part.split('"')[0] for part in html.split('src="')[1:]]
        css_refs = [part.split('"')[0] for part in html.split('href="')[1:]]
        app_path = next(path for path in app_refs if path.endswith(".js"))
        css_path = next(path for path in css_refs if path.endswith(".css"))
        assert app_path.startswith("assets/app.")
        assert css_path.startswith("assets/style.")
        assert f"www/{app_path}" in names
        assert f"www/{css_path}" in names
        assert archive.read(f"www/{app_path}") == (source / "www" / "app.js").read_bytes()
        assert "www/assets/app.0000000000000000.js" not in names
        assert "www/assets/style.0000000000000000.css" not in names
        assert "www/asset-manifest.json" in names
    assert input_zip.read_bytes() == first_archive
    assert mobile_zip.read_bytes() == input_zip.read_bytes()
