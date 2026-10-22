"""Behavioral coverage for hashed assets surviving a web image replacement."""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _write_source(source: Path, revision: int) -> None:
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="style.css">'
        f'</head><body data-revision="{revision}"><script src="app.js"></script>'
        '</body></html>',
        encoding="utf-8",
    )
    (source / "app.js").write_text(f"window.revision = {revision};\n", encoding="utf-8")
    (source / "style.css").write_text(f"body {{ --revision: {revision}; }}\n", encoding="utf-8")


def _build_dist(tmp_path: Path, revision: int) -> Path:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    build_assets = importlib.import_module("tools.build_web_assets").build_assets
    source = tmp_path / f"source-{revision}"
    dist = tmp_path / f"image-{revision}" / "www"
    source.mkdir(parents=True)
    _write_source(source, revision)
    build_assets(source, dist)
    return dist


def _app_and_css_paths(dist: Path) -> set[str]:
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    return set(manifest["referencedFiles"])


def test_previous_html_bundle_remains_readable_after_a_fresh_image_starts(tmp_path):
    old_image = _build_dist(tmp_path, 1)
    new_image = _build_dist(tmp_path, 2)
    old_paths = _app_and_css_paths(old_image)
    new_paths = _app_and_css_paths(new_image)
    assert old_paths.isdisjoint(new_paths)

    archive = tmp_path / "persistent-web-assets"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prepare_web_assets = importlib.import_module(
        "tools.selfhosted.web_asset_archive"
    ).prepare_web_assets

    prepare_web_assets(old_image, archive)
    prepare_web_assets(new_image, archive)

    # A browser that loaded the previous index just before the container restart
    # must still be able to GET both immutable URLs from the new container root.
    for relative in old_paths:
        assert (new_image / relative).read_bytes() == (old_image / relative).read_bytes()
    for relative in new_paths:
        assert (new_image / relative).is_file()


def test_rollover_keeps_only_the_current_and_immediately_previous_build(tmp_path):
    images = [_build_dist(tmp_path, revision) for revision in (1, 2, 3)]
    paths = [_app_and_css_paths(image) for image in images]
    archive = tmp_path / "persistent-web-assets"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prepare_web_assets = importlib.import_module(
        "tools.selfhosted.web_asset_archive"
    ).prepare_web_assets

    for image in images:
        prepare_web_assets(image, archive)

    assert paths[1] | paths[2] <= {
        path.relative_to(images[2]).as_posix()
        for path in (images[2] / "assets").iterdir()
        if path.is_file()
    }
    assert not any((images[2] / relative).exists() for relative in paths[0])
    build_directories = [path for path in (archive / "builds").iterdir() if path.is_dir()]
    assert len(build_directories) == 2


def test_corrupt_previous_build_is_discarded_instead_of_blocking_startup(tmp_path):
    """Critical 2: one damaged retained build must not crash-loop the site.

    The archive is a persistent bind mount, so a corrupt generation survives
    image replacement. A previously healthy build that got damaged there used
    to fail _materialize on every boot, and `restart: unless-stopped` plus
    nginx `service_healthy` turned it into a permanent outage needing an SSH
    session to fix. Historical generations are now validated one by one and
    dropped; only the current bundle stays fail-fast.
    """
    first_image = _build_dist(tmp_path, 1)
    second_image = _build_dist(tmp_path, 2)
    third_image = _build_dist(tmp_path, 3)
    archive = tmp_path / "persistent-web-assets"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prepare_web_assets = importlib.import_module(
        "tools.selfhosted.web_asset_archive"
    ).prepare_web_assets

    prepare_web_assets(first_image, archive)
    prepare_web_assets(second_image, archive)
    builds_dir = archive / "builds"
    older = next(
        path for path in builds_dir.iterdir() if path.name == _build_id(first_image)
    )
    damaged = next(
        path for path in builds_dir.iterdir() if path.name == _build_id(second_image)
    )
    # Once the third image starts, `damaged` is the generation the archive
    # would otherwise keep as "previous" - the one _materialize used to trip
    # over on every boot.
    (damaged / "manifest.json").write_bytes(b"{ not json")

    # A fresh image must come up on its own.
    prepare_web_assets(third_image, archive)

    assert not damaged.exists(), "the damaged generation must be cleaned up, not just skipped"
    retained = sorted(path.name for path in builds_dir.iterdir() if path.is_dir())
    assert retained == sorted([_build_id(third_image), older.name]), retained
    manifest = json.loads((third_image / "manifest.json").read_text(encoding="utf-8"))
    for relative in manifest["referencedFiles"]:
        assert (third_image / relative).is_file(), relative


def test_corrupt_previous_does_not_hide_a_healthy_older_generation(tmp_path):
    """Only the damaged generation is lost; the rest of the history survives."""
    images = [_build_dist(tmp_path, revision) for revision in (1, 2, 3, 4)]
    archive = tmp_path / "persistent-web-assets"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prepare_web_assets = importlib.import_module(
        "tools.selfhosted.web_asset_archive"
    ).prepare_web_assets

    for image in images[:3]:
        prepare_web_assets(image, archive)
    builds_dir = archive / "builds"
    damaged = builds_dir / _build_id(images[2])
    previous = builds_dir / _build_id(images[1])
    (damaged / "assets").joinpath(next((damaged / "assets").iterdir())).write_bytes(b"broken bytes")

    prepare_web_assets(images[3], archive)

    remaining = sorted(path.name for path in builds_dir.iterdir() if path.is_dir())
    assert damaged.name not in remaining
    assert remaining == sorted([_build_id(images[3]), previous.name]), remaining


def _build_id(dist: Path) -> str:
    import hashlib

    return hashlib.sha256((dist / "manifest.json").read_bytes()).hexdigest()[:32]


def test_unwritable_archive_reports_the_domain_error_not_a_bare_oserror(tmp_path):
    """Important 4: an unusable archive must say so in the operator's language.

    A raw FileExistsError/PermissionError is indistinguishable from a dozen
    other startup failures when the entrypoint aborts before the health port
    opens, which is exactly the moment an operator is reading the log.
    """
    image = _build_dist(tmp_path, 1)
    archive = tmp_path / "archive-is-a-file"
    archive.write_bytes(b"not a directory\n")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prepare_web_assets = importlib.import_module(
        "tools.selfhosted.web_asset_archive"
    ).prepare_web_assets

    with pytest.raises(ValueError, match="Web asset archive directory is not writable"):
        prepare_web_assets(image, archive)


@pytest.mark.skipif(os.name != "posix", reason="posix permission bits")
def test_read_only_archive_directory_is_reported_as_unwritable(tmp_path):
    """A read-only bind mount must fail with the same readable message."""
    image = _build_dist(tmp_path, 1)
    archive = tmp_path / "read-only-archive"
    archive.mkdir()
    archive.chmod(0o500)
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        prepare_web_assets = importlib.import_module(
            "tools.selfhosted.web_asset_archive"
        ).prepare_web_assets

        with pytest.raises(ValueError, match="Web asset archive directory is not writable"):
            prepare_web_assets(image, archive)
    finally:
        archive.chmod(0o700)


def test_corrupt_current_asset_aborts_before_entrypoint_starts_proxy(tmp_path, monkeypatch):
    image = _build_dist(tmp_path, 1)
    referenced = next(iter(_app_and_css_paths(image)))
    (image / referenced).write_bytes(b"bytes do not match the content hash")
    archive = tmp_path / "persistent-web-assets"
    monkeypatch.setenv("OTV_WEB_ROOT", str(image))
    monkeypatch.setenv("OTV_WEB_ASSET_ARCHIVE", str(archive))

    entrypoint = importlib.import_module("tools.selfhosted.web_entrypoint")
    proxy_was_started = False

    def fake_execvp(*_args):
        nonlocal proxy_was_started
        proxy_was_started = True

    monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
    with pytest.raises(ValueError, match="hash|manifest|integrity"):
        entrypoint.main()
    assert not proxy_was_started


def test_copied_files_are_archived_so_a_previous_page_keeps_its_own_bytes(tmp_path):
    """Important 3: copiedFiles used to vanish from the archive entirely.

    The hashed JS/CSS were preserved, but the 36 posters, the logo and the
    web manifest were not written to the archive at all. copiedFiles use
    unversioned fixed paths, so a browser that had just loaded the previous
    index would fetch the *new* poster and logo bytes under the old page.
    """
    old_image = _build_dist_with_copied_files(tmp_path, 1)
    new_image = _build_dist_with_copied_files(tmp_path, 2)
    archive = tmp_path / "persistent-web-assets"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prepare_web_assets = importlib.import_module(
        "tools.selfhosted.web_asset_archive"
    ).prepare_web_assets

    prepare_web_assets(old_image, archive)
    prepare_web_assets(new_image, archive)

    old_manifest = json.loads((old_image / "manifest.json").read_text(encoding="utf-8"))
    copied = old_manifest["copiedFiles"]
    assert copied, "the fixture must produce copiedFiles"
    for relative in copied:
        assert (new_image / relative).is_file(), relative
    # The archive kept the old generation's bytes for every copied file.
    for relative in copied:
        assert (archive / "builds" / _build_id(old_image) / relative).is_file(), relative


def test_previous_copied_files_are_restored_when_the_new_image_lacks_them(tmp_path):
    """A dropped asset from the new build must not vanish under the old page."""
    old_image = _build_dist_with_copied_files(tmp_path, 1)
    archive = tmp_path / "persistent-web-assets"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prepare_web_assets = importlib.import_module(
        "tools.selfhosted.web_asset_archive"
    ).prepare_web_assets
    prepare_web_assets(old_image, archive)

    # Second image has no copied assets at all.
    bare_source = tmp_path / "source-bare"
    bare_image = tmp_path / "image-bare" / "www"
    bare_source.mkdir(parents=True)
    _write_source(bare_source, 9)
    importlib.import_module("tools.build_web_assets").build_assets(bare_source, bare_image)

    prepare_web_assets(bare_image, archive)

    for relative in json.loads(
        (old_image / "manifest.json").read_text(encoding="utf-8")
    )["copiedFiles"]:
        assert (bare_image / relative).is_file(), (
            f"{relative} was restored from the archive for the previous page"
        )


def _build_dist_with_copied_files(tmp_path: Path, revision: int) -> Path:
    build_assets = importlib.import_module("tools.build_web_assets").build_assets
    source = tmp_path / f"source-assets-{revision}"
    dist = tmp_path / f"image-assets-{revision}" / "www"
    source.mkdir(parents=True)
    _write_source(source, revision)
    (source / "manifest.webmanifest").write_text(
        f'{{"name":"My TV rev {revision}"}}\n', encoding="utf-8"
    )
    (source / "assets").mkdir(exist_ok=True)
    (source / "assets" / "logo.png").write_bytes(f"LOGO-{revision}".encode())
    build_assets(source, dist)
    return dist


def test_web_service_mounts_persistent_archive_and_runs_archive_entrypoint():
    compose = yaml.safe_load((ROOT / "deploy/selfhosted/compose.yaml").read_text(encoding="utf-8"))
    web_api = compose["services"]["web-api"]
    assert any(
        isinstance(mount, str)
        and ":/var/lib/mytv/web-assets" in mount
        and "WEB_ASSET_ARCHIVE_HOST_PATH" in mount
        for mount in web_api.get("volumes", [])
    )
    # The entrypoint writes to OTV_WEB_ASSET_ARCHIVE. If that drifts from the
    # mount point, the "persistent" archive silently becomes a container-local
    # directory that dies with the container.
    assert web_api["environment"]["OTV_WEB_ASSET_ARCHIVE"] == "/var/lib/mytv/web-assets"
    assert web_api["environment"]["PORT"] == "8090"

    dockerfile = (ROOT / "deploy/selfhosted/Dockerfile.web").read_text(encoding="utf-8")
    assert "web_asset_archive.py" in dockerfile
    assert "web_entrypoint.py" in dockerfile
    assert 'CMD ["python", "web_entrypoint.py"]' in dockerfile
    assert compose["services"]["nginx"]["depends_on"]["web-api"]["condition"] == "service_healthy"


def test_archive_volume_is_documented_for_preparation_and_backup():
    """The retained web-asset archive volume remains documented centrally."""
    docs = (ROOT / "docs/项目文档.md").read_text(encoding="utf-8")
    assert "WEB_ASSET_ARCHIVE_HOST_PATH" in docs
    assert "web-assets" in docs
    assert "备份" in docs
