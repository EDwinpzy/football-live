"""Service-layer contracts for the Web assets the real proxy Handler serves."""
import hashlib
import http.client
import importlib
import json
import os
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = ROOT / "tools" / "backend-src"
SOURCE_WWW = BACKEND_SRC / "www"


def _load_proxy():
    if str(BACKEND_SRC) not in sys.path:
        sys.path.insert(0, str(BACKEND_SRC))
    return importlib.import_module("proxy")


def _prepare_web_assets():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.selfhosted.web_asset_archive").prepare_web_assets


def _build_assets():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.build_web_assets").build_assets


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve(directory: Path):
    """Serve ``directory`` through the shipped Handler's real request logic.

    The root comes from ``OTV_WEB_ROOT`` - the same environment variable
    ``web_entrypoint`` already uses - so the served tree, the cache table and
    the health field all resolve identically. Every decision under test
    (``do_GET``, ``send_response``, ``send_header``, ``end_headers``) is the
    unmodified production implementation, and assertions read real headers.
    """
    proxy = _load_proxy()

    previous = os.environ.get("OTV_WEB_ROOT")
    os.environ["OTV_WEB_ROOT"] = str(directory)
    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), proxy.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"127.0.0.1:{server.server_address[1]}"

    def request(path: str):
        host, _, port = address.partition(":")
        connection = http.client.HTTPConnection(host, int(port), timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def restore():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if previous is None:
            os.environ.pop("OTV_WEB_ROOT", None)
        else:
            os.environ["OTV_WEB_ROOT"] = previous

    return request, server, thread, restore


def _write_source(source: Path, revision: int) -> None:
    (source / "assets").mkdir(parents=True, exist_ok=True)
    (source / "index.html").write_text(
        '<!doctype html><html><head>'
        '<link rel="icon" href="assets/logo.png">'
        '<link rel="apple-touch-icon" href="assets/logo.png">'
        '<link rel="stylesheet" href="style.css?v=131">'
        f'</head><body data-revision="{revision}">'
        '<script src="app.js?v=134" defer></script></body></html>',
        encoding="utf-8",
    )
    (source / "app.js").write_text(f"window.revision = {revision};\n", encoding="utf-8")
    (source / "style.css").write_text(f"body {{ --revision: {revision}; }}\n", encoding="utf-8")
    (source / "manifest.webmanifest").write_text('{"name":"My TV"}\n', encoding="utf-8")
    (source / "assets" / "logo.png").write_bytes(f"LOGO-{revision}".encode())


def _build_dist(tmp_path: Path, revision: int) -> Path:
    source = tmp_path / f"source-{revision}"
    dist = tmp_path / f"image-{revision}" / "www"
    source.mkdir(parents=True)
    _write_source(source, revision)
    _build_assets()(source, dist)
    return dist


def _referenced(dist: Path) -> dict:
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    cache_control = manifest["cacheControl"]
    return {path: cache_control[f"/{path}"] for path in manifest["referencedFiles"]}


@pytest.fixture
def archived_dist(tmp_path, monkeypatch):
    """A generated dist with a real two-generation archive behind it."""
    prepare_web_assets = _prepare_web_assets()
    first = _build_dist(tmp_path, 1)
    second = _build_dist(tmp_path, 2)
    archive = tmp_path / "persistent-web-assets"
    prepare_web_assets(first, archive)
    prepare_web_assets(second, archive)
    monkeypatch.setenv("OTV_WEB_ASSET_ARCHIVE", str(archive))
    return second, archive


@pytest.fixture
def dist_request(archived_dist):
    dist, _archive = archived_dist
    request, _server, _thread, restore = _serve(dist)
    try:
        yield request
    finally:
        restore()


@pytest.fixture
def source_request():
    request, _server, _thread, restore = _serve(SOURCE_WWW)
    try:
        yield request
    finally:
        restore()


def test_content_hashed_assets_are_served_immutable(dist_request, archived_dist):
    """Critical 1: the manifest contract must reach the real response headers."""
    dist, _archive = archived_dist
    referenced = _referenced(dist)
    assert referenced, "the generated dist must reference content-hashed bundles"
    for path, declared in referenced.items():
        status, headers, _body = dist_request(f"/{path}")
        assert status == 200, path
        served = headers.get("Cache-Control")
        assert served == declared, f"{path} served {served!r}, manifest declares {declared!r}"
        assert "immutable" in served, path


def test_unhashed_source_assets_keep_the_extension_defaults(source_request):
    """The legacy no-hash URLs must not regress when the manifest takes over."""
    for path, expected in (
        ("/app.js", "public, max-age=300"),
        ("/style.css", "public, max-age=300"),
        ("/app.js?v=134", "public, max-age=31536000, immutable"),
    ):
        status, headers, _body = source_request(path)
        assert status == 200, path
        assert headers["Cache-Control"] == expected, path


def test_html_is_never_cached_so_a_rollout_is_visible_immediately(dist_request):
    for path in ("/", "/index.html"):
        status, headers, _body = dist_request(path)
        assert status == 200, path
        assert headers["Cache-Control"] == "no-store", path


def test_health_reports_the_active_build_and_retained_generations(dist_request, archived_dist):
    """Important 5 follow-through: archive degradation has to be observable."""
    dist, archive = archived_dist
    status, _headers, body = dist_request("/api/health")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is True
    assets = payload["assets"]
    assert assets["ok"] is True
    expected_build = hashlib.sha256((dist / "manifest.json").read_bytes()).hexdigest()[:32]
    assert assets["build"] == expected_build
    assert sorted(assets["retained"]) == sorted(
        path.name for path in (archive / "builds").iterdir() if path.is_dir()
    )
    assert expected_build in assets["retained"]


def test_health_reports_missing_bundle_metadata_instead_of_crashing(source_request):
    """The source tree has no generated manifest; health must flag it, not fail."""
    status, _headers, body = source_request("/api/health")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is True
    assert payload["assets"] == {"ok": False, "build": None, "retained": []}
