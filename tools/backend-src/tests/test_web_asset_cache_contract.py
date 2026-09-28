"""Behavioral contracts for deterministic Web assets and APK packaging."""
import hashlib
import importlib
import json
import re
import sys
import subprocess
import zipfile
from pathlib import Path

import pytest

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
    dist.mkdir()
    (dist / "asset-manifest.json").write_text("legacy manifest\n", encoding="utf-8")

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

    assert (dist / "manifest.json").is_file()
    assert not (dist / "asset-manifest.json").exists()
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
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


def test_source_hash_named_assets_cannot_replace_generated_bundle_bytes(tmp_path):
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    app_bytes = b"window.app = 'source';\n"
    css_bytes = b"body { color: white; }\n"
    _write_source(source, app=app_bytes, css=css_bytes)
    app_digest = hashlib.sha256(app_bytes).hexdigest()[:16]
    css_digest = hashlib.sha256(css_bytes).hexdigest()[:16]
    (source / "assets" / f"app.{app_digest}.js").write_bytes(b"poison app\n")
    (source / "assets" / f"style.{css_digest}.css").write_bytes(b"poison css\n")

    result = build_assets(source, dist)

    app_path = next(path for path in result.referenced_files if path.endswith(".js"))
    css_path = next(path for path in result.referenced_files if path.endswith(".css"))
    assert (dist / app_path).read_bytes() == app_bytes
    assert (dist / css_path).read_bytes() == css_bytes
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    hashes = {asset["path"]: asset["sha256"] for asset in manifest["assets"]}
    assert hashes[f"/{app_path}"] == hashlib.sha256((dist / app_path).read_bytes()).hexdigest()
    assert hashes[f"/{css_path}"] == hashlib.sha256((dist / css_path).read_bytes()).hexdigest()


def test_source_hash_named_assets_cannot_replace_previous_immutable_urls(tmp_path):
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    _write_source(source, app=b"window.revision = 1;\n", css=b"body { color: white; }\n")

    first = build_assets(source, dist)
    old_app = next(path for path in first.referenced_files if path.endswith(".js"))
    old_css = next(path for path in first.referenced_files if path.endswith(".css"))
    old_bytes = {old_app: (dist / old_app).read_bytes(), old_css: (dist / old_css).read_bytes()}
    (source / "app.js").write_bytes(b"window.revision = 2;\n")
    (source / "style.css").write_bytes(b"body { color: black; }\n")
    (source / "assets" / Path(old_app).name).write_bytes(b"poison old app URL\n")
    (source / "assets" / Path(old_css).name).write_bytes(b"poison old css URL\n")

    build_assets(source, dist)

    assert {path: (dist / path).read_bytes() for path in old_bytes} == old_bytes


def test_self_hosted_web_image_builds_and_uses_hashed_dist(tmp_path):
    dockerfile = (ROOT / "deploy" / "selfhosted" / "Dockerfile.web").read_text(encoding="utf-8")
    for contract in (
        "FROM python:3.12-slim AS web-assets",
        "COPY tools/backend-src/www/ /build/www/",
        "COPY tools/build_web_assets.py /build/build_web_assets.py",
        "RUN python /build/build_web_assets.py /build/www /build/web-dist",
        "COPY tools/backend-src/*.py tools/backend-src/*.json /app/",
        "COPY --from=web-assets /build/web-dist/ /app/www/",
    ):
        assert contract in dockerfile
    assert "COPY tools/backend-src/ /app/" not in dockerfile

    source = tmp_path / "www"
    dist = tmp_path / "web-dist"
    _write_source(source, app=b"window.dockerBuild = true;\n", css=b"body { color: navy; }\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_web_assets.py"), str(source), str(dist)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "输出目录：" in result.stdout
    html = (dist / "index.html").read_text(encoding="utf-8")
    for reference in re.findall(r'(?:src|href)="(assets/[^\"]+)"', html):
        asset = dist / reference
        assert asset.is_file()
        expected_prefix = Path(reference).name.split(".")[1]
        assert hashlib.sha256(asset.read_bytes()).hexdigest().startswith(expected_prefix)


def test_web_image_decrypt_stream_js_is_either_copied_or_explicitly_excluded():
    """Important 6: narrowing the COPY list must not silently drop a runtime dep.

    proxy.py shells out to `node decrypt_stream.js` and falls back to the
    quickjs path in decrypt_python when node is missing. The image is
    python:3.12-slim, so node is never there: the file is dead weight. But
    dropping it without a word makes the next person debug quickjs instead of
    noticing the file never shipped. Whichever way this goes, say so.
    """
    dockerfile = (ROOT / "deploy" / "selfhosted" / "Dockerfile.web").read_text(encoding="utf-8")
    copied = "COPY tools/backend-src/decrypt_stream.js" in dockerfile
    explained = "decrypt_stream.js" in dockerfile and "decrypt_python" in dockerfile
    assert copied or explained, (
        "decrypt_stream.js must either be copied into the image or have a comment "
        "explaining that the container has no node and decryption uses decrypt_python"
    )


def test_output_nested_in_source_is_rejected_without_changing_source(tmp_path):
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    output = source / "dist"
    _write_source(source, app=b"window.app = 1;\n", css=b"body {}\n")
    before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}

    with pytest.raises(ValueError):
        build_assets(source, output)

    assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before


def test_source_nested_in_output_is_rejected_before_stale_asset_cleanup(tmp_path):
    build_assets = _load_builder().build_assets
    output = tmp_path / "output"
    source = output / "source"
    _write_source(source, app=b"window.app = 1;\n", css=b"body {}\n")
    before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    stale = output / "assets" / "app.0000000000000000.js"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old generated asset")
    previous = {"referencedFiles": ["assets/app.0000000000000000.js"]}
    (output / "manifest.json").write_text(json.dumps(previous), encoding="utf-8")
    (output / "asset-manifest.json").write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(ValueError):
        build_assets(source, output)

    assert stale.read_bytes() == b"old generated asset"
    assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before


def test_removed_source_asset_is_removed_from_persistent_dist(tmp_path):
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    _write_source(source, app=b"window.app = 1;\n", css=b"body {}\n")
    source_asset = source / "assets" / "logo.svg"
    build_assets(source, dist)
    assert (dist / "assets" / "logo.svg").is_file()

    source_asset.unlink()
    build_assets(source, dist)

    assert not (dist / "assets" / "logo.svg").exists()


def test_index_reference_forms_the_rewriter_cannot_see_are_rejected(tmp_path):
    """Minor 9: the rewriter only matches a few HTML shapes, so guard the rest.

    The stylesheet/script rewriters fail fast on the shapes they cannot match,
    but a *second* reference the rewriter never inspects - another icon, a
    traversal - would sail through and leave the index pointing at an asset
    that is not in the dist. Absolute and protocol-remote URLs are exempt: the
    hls.js CDN reference is deliberate and is not served from dist, so only
    local paths have to resolve.
    """
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    _write_source(source, app=b"window.app = 1;\n", css=b"body {}\n")
    index = source / "index.html"

    # A CDN reference is legitimate and must not be resolved against dist.
    index.write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="style.css">'
        '</head><body><script src="app.js"></script>'
        '<script src="https://cdn.example.com/hls.min.js"></script></body></html>',
        encoding="utf-8",
    )
    result = build_assets(source, dist)
    assert result.referenced_files

    # A local path that escapes the dist is still rejected.
    index.write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="style.css">'
        '</head><body><script src="app.js"></script>'
        '<link rel="icon" href="../outside.png"></body></html>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside the dist"):
        build_assets(source, dist)

    # So is a second local reference the rewriter never inspects and that
    # silently does not exist in the dist.
    index.write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="style.css">'
        '<link rel="apple-touch-icon" href="assets/absent.png">'
        '</head><body><script src="app.js"></script></body></html>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing asset"):
        build_assets(source, dist)


def test_index_referencing_a_missing_asset_is_rejected(tmp_path):
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    _write_source(source, app=b"window.app = 1;\n", css=b"body {}\n")
    (source / "index.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="style.css">'
        '<link rel="icon" href="assets/absent.png">'
        '</head><body><script src="app.js"></script></body></html>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing asset"):
        build_assets(source, dist)


def test_manifest_describes_every_copied_asset_with_its_hash(tmp_path):
    """The archive verifies copiedFiles against these records."""
    build_assets = _load_builder().build_assets
    source = tmp_path / "source"
    dist = tmp_path / "dist"
    _write_source(source, app=b"window.app = 1;\n", css=b"body {}\n")
    (source / "assets" / "logo.png").write_bytes(b"logo-bytes")

    build_assets(source, dist)
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))

    assert {item["path"] for item in manifest["copiedAssetMeta"]} == set(manifest["copiedFiles"])
    for descriptor in manifest["copiedAssetMeta"]:
        data = (dist / descriptor["path"]).read_bytes()
        assert descriptor["sha256"] == hashlib.sha256(data).hexdigest()
        assert descriptor["size"] == len(data)


def _make_backend_zip_fixture(tmp_path, monkeypatch):
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
        archive.writestr("www/asset-manifest.json", "stale manifest")
        archive.writestr("www/assets/football/fb-01.jpg", b"existing poster")

    monkeypatch.setattr(build_zip, "SRC_DIR", str(source))
    monkeypatch.setattr(build_zip, "SRC_ZIP", str(input_zip))
    monkeypatch.setattr(build_zip, "MOBILE_ZIP", str(mobile_zip))
    monkeypatch.setattr(build_zip, "TMP_ZIP", str(temp_zip))
    poster_dir = tmp_path / "posters"
    monkeypatch.setattr(build_zip, "POSTER_DIR", str(poster_dir))
    return build_zip, source, input_zip, mobile_zip, poster_dir


def test_backend_zip_contains_the_hashed_web_bundle(tmp_path, monkeypatch):
    build_zip, source, input_zip, mobile_zip, _poster_dir = _make_backend_zip_fixture(tmp_path, monkeypatch)
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
        assert "www/asset-manifest.json" not in names
        assert "www/manifest.json" in names
        assert archive.read("www/assets/football/fb-01.jpg") == b"existing poster"
    assert input_zip.read_bytes() == first_archive
    assert mobile_zip.read_bytes() == input_zip.read_bytes()


def test_removed_source_asset_is_removed_from_next_backend_zip(tmp_path, monkeypatch):
    build_zip, source, input_zip, _mobile_zip, _poster_dir = _make_backend_zip_fixture(tmp_path, monkeypatch)
    build_zip.main()
    with zipfile.ZipFile(input_zip) as archive:
        assert "www/assets/logo.svg" in archive.namelist()

    (source / "www" / "assets" / "logo.svg").unlink()
    build_zip.main()
    with zipfile.ZipFile(input_zip) as archive:
        assert "www/assets/logo.svg" not in archive.namelist()
        assert archive.read("www/assets/football/fb-01.jpg") == b"existing poster"


def test_backend_zip_retains_previous_hashed_bundle_for_one_build(tmp_path, monkeypatch):
    build_zip, source, input_zip, _mobile_zip, _poster_dir = _make_backend_zip_fixture(tmp_path, monkeypatch)
    build_zip.main()

    def packaged_app_path():
        with zipfile.ZipFile(input_zip) as archive:
            html = archive.read("www/index.html").decode("utf-8")
        match = re.search(r'src="(assets/app\.[0-9a-f]{16}\.js)"', html)
        assert match
        return match.group(1)

    first = packaged_app_path()
    (source / "www" / "app.js").write_text("window.app = 'revision 2';\n", encoding="utf-8")
    build_zip.main()
    second = packaged_app_path()
    with zipfile.ZipFile(input_zip) as archive:
        names = set(archive.namelist())
    assert second != first
    assert f"www/{first}" in names
    assert f"www/{second}" in names

    (source / "www" / "app.js").write_text("window.app = 'revision 3';\n", encoding="utf-8")
    build_zip.main()
    third = packaged_app_path()
    with zipfile.ZipFile(input_zip) as archive:
        names = set(archive.namelist())
    assert third != second
    assert f"www/{first}" not in names
    assert f"www/{second}" in names
    assert f"www/{third}" in names


def test_football_pool_is_preserved_or_replaced_by_existing_rules(tmp_path, monkeypatch):
    build_zip, source, input_zip, _mobile_zip, poster_dir = _make_backend_zip_fixture(tmp_path, monkeypatch)
    source_pool = source / "www" / "assets" / "football" / "fb-01.jpg"
    source_pool.parent.mkdir(parents=True)
    source_pool.write_bytes(b"source poster")
    build_zip.main()
    with zipfile.ZipFile(input_zip) as archive:
        assert archive.read("www/assets/football/fb-01.jpg") == b"existing poster"

    source_pool.unlink()
    build_zip.main()
    with zipfile.ZipFile(input_zip) as archive:
        assert archive.read("www/assets/football/fb-01.jpg") == b"existing poster"

    poster_dir.mkdir()
    (poster_dir / "new-poster.jpg").write_bytes(b"replacement poster")
    build_zip.main()
    with zipfile.ZipFile(input_zip) as archive:
        assert archive.read("www/assets/football/fb-01.jpg") == b"replacement poster"
