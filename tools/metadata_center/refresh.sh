#!/bin/sh
# Daily refresh: re-harvest the Douban snapshot (best effort) and republish the
# metadata database.  A failed harvest must never stop us from republishing the
# last good snapshot, so the harvest step is allowed to fail.
set -u

SNAPSHOT="${OTV_METADATA_SNAPSHOT:-/srv/metadata/snapshots/douban_snapshot.json}"
SOURCE="${OTV_METADATA_SOURCE_SNAPSHOT:-/app/tools/backend-src/douban_snapshot.json}"

echo "[metadata-refresh] $(date -u +%Y-%m-%dT%H:%M:%SZ) harvest start"
# Harvest failures are expected when Douban rate-limits the VPS.  The previous
# snapshot stays in place and the rebuild below still publishes it.
python /app/tools/build_douban_snapshot.py || true

if [ -f "$SOURCE" ]; then
    mkdir -p "$(dirname "$SNAPSHOT")"
    cp -f "$SOURCE" "$SNAPSHOT"
    echo "[metadata-refresh] snapshot <- $SOURCE ($(wc -c < "$SNAPSHOT") bytes)"
else
    echo "[metadata-refresh] no source snapshot at $SOURCE; using $SNAPSHOT"
fi

python -m metadata_center.cli rebuild
STATUS=$?
echo "[metadata-refresh] rebuild exit=$STATUS"
exit "$STATUS"
