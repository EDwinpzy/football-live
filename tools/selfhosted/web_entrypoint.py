"""Restore immutable bundles before making the Web API healthy."""
from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__:
    from .web_asset_archive import prepare_web_assets
else:  # Executed as /app/web_entrypoint.py inside the image.
    from web_asset_archive import prepare_web_assets


def main() -> None:
    web_root = Path(os.environ.get("OTV_WEB_ROOT", "/app/www"))
    archive_root = Path(os.environ.get("OTV_WEB_ASSET_ARCHIVE", "/var/lib/mytv/web-assets"))
    prepare_web_assets(web_root, archive_root)
    port = os.environ.get("PORT", "8090")
    os.execvp(sys.executable, [sys.executable, "proxy.py", port])


if __name__ == "__main__":
    main()
