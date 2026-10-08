"""Package an unsigned Xcode device product for personal local re-signing."""

import argparse
import pathlib
import plistlib
import zipfile


def package(app: pathlib.Path, output: pathlib.Path) -> None:
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("Expected an Xcode .app directory")
    info = plistlib.loads((app / "Info.plist").read_bytes())
    platforms = info.get("CFBundleSupportedPlatforms", [])
    if "iPhoneOS" not in platforms:
        raise ValueError("Refusing to package a simulator product as an iPhone IPA")
    executable = app / info["CFBundleExecutable"]
    if not executable.is_file():
        raise ValueError("App executable is missing")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(app.rglob("*")):
            if not file.is_file():
                continue
            entry = zipfile.ZipInfo.from_file(file, pathlib.PurePosixPath("Payload", app.name, *file.relative_to(app).parts).as_posix())
            entry.compress_type = zipfile.ZIP_DEFLATED
            # Preserve the executable bit even when this helper is checked on Windows.
            mode = 0o755 if file == executable else 0o644
            entry.external_attr = (0o100000 | mode) << 16
            archive.writestr(entry, file.read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    package(args.app, args.output)
