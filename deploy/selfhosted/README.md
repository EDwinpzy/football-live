# My TV self-hosted stack

This directory contains the self-hosted My TV stack. Only Nginx publishes host ports: provider NAT maps external HTTPS `35208` to internal `8443`, and external health port `25182` to internal `8080`. Do not change security groups or NAT mappings. The health port exposes only `/health`; application traffic uses HTTPS on port `35208`.

The file API allows only the `hotupdate` bucket. Its HTTPS object route is `/v1/storages/object/{bucket}/{key}` and supports `GET`, `HEAD`, and single byte ranges. Upload and deletion require a valid service JWT. Admin pages and business APIs are exposed only through Nginx over the configured HTTPS NAT port.

The CloudBase migration scope is restricted to My TV's 13 `public` business tables and object keys under `apk-update/`, `backend-hu*.zip`, and `iptv/` in the `hotupdate` bucket. Other CloudBase schemas, buckets, and object keys are excluded.

The self-hosted admin runs the existing IPTV source rebuild daily at 06:30 Asia/Shanghai. Scheduled rebuilds, manual publishes, rollbacks, and mode changes share a database lease with a five-minute expiry and 30-second renewal. If the lease RPC is unavailable, publication fails closed; a crashed process releases the lease automatically after expiry.

## Prepare the VPS

1. Install Docker Engine and the Docker Compose plugin from the Ubuntu 24.04 package repository.
2. Copy this directory and `tools/selfhosted/health.py` to the same relative paths on the VPS.
3. Create `/srv/mytv/objects`, `/srv/mytv/secrets`, and `/srv/mytv/secrets/license`. Copy `.env.example` to `.env`; generate separate random values for the Postgres administrator, PostgREST `authenticator` role, and JWT signing key. Create `auth-users.json` with salted PBKDF2 password hashes and a `jwt-signing-key` file containing the same JWT key used by PostgREST and the private file API.
4. Create root-only `admin-credentials.json`, `admin-session-secret`, and `admin-api-credentials.json` files. The admin API account must use the My TV service subject `2094843486709620737`; store only its salted hash in `auth-users.json` and keep its matching username/password in `admin-api-credentials.json`. The original license-signing key, if available, belongs at `/srv/mytv/secrets/license/private_key.pem`; do not generate a replacement because existing apps trust the original public key.
5. Restrict `.env` and all secret files to root (`chmod 600`); make the object directory writable by the storage service.
6. In the provider NAT panel, forward an external port to internal TCP port 8080. Do not add or change a security group.
7. Start the staging stack with `docker compose up -d --build` and check `docker compose ps` plus `curl http://127.0.0.1:8080/health`.

The HTTP health endpoint intentionally serves only `/health`; never send credentials or App traffic to it. The HTTPS certificate is issued for the VPS IP and its migration CA is packaged with both APKs and the CloudBase bridge functions. If the original license-signing key is not installed, license replenishment stays unavailable while migrated existing licenses continue to work.

## Publish an APK from the VPS

The optional `apk-publisher` Compose profile sends upload requests to the private storage service and writes public manifest URLs using `OTV_PUBLIC_BASE_URL`. Copy a signed APK into `/srv/mytv/deploy/selfhosted/releases/`, then run from `/srv/mytv`:

```sh
docker compose -f deploy/selfhosted/compose.yaml --profile ops run --rm apk-publisher \
  --channel tv --apk /releases/MyTV-TV版.apk \
  --version-code 110 --version-name 1.1 --min-supported 100 --force \
  --cert <APK-signing-certificate-SHA-256> \
  --title "后台迁移" --note "后台服务已迁移到新服务器"
```

Use `--channel mobile` for the mobile package. During the CloudBase compatibility period, publish the same release to CloudBase storage as well so already-installed clients can download it from their existing update endpoint. The CloudBase bridge sends API requests to this VPS; it does not carry large APK or hot-update binaries through a function response.

CloudBase service cutoff is **2026-10-09**. Keep the legacy `activate`, `hotupdate`, and `iptv` bridges plus APK/hot-update object mirrors active until then. Publish the migration APK with forced update and minimum supported version `100`; confirm that existing clients can download and install it before the cutoff. After the verified data cutover, the VPS is the sole writer. This migration covers My TV resources only.

## Stop and preserve data

Use `docker compose down` to stop containers. Do not pass `--volumes`; `postgres_data` contains the database. Object files persist under `OBJECTS_HOST_PATH`.

## Metadata centre

`metadata-api` answers read-only catalogue queries on internal port `8094`; Nginx publishes it under
`/metadata/` on the HTTPS NAT port. Published artefacts sit next to the other objects:

- `/v1/storages/object/hotupdate/metadata/manifest.json` - latest version, sizes and SHA-256 values
- `/v1/storages/object/hotupdate/metadata/metadata-v<version>.sqlite.gz` - full catalogue, gzip, deterministic
- `/v1/storages/object/hotupdate/metadata/delta-<from>-<to>.sql.gz` - SQL delta between two versions

Endpoints: `/metadata/health`, `/metadata/manifest.json`, `/metadata/search?q=&limit=`, `/metadata/<mytv_id>`,
`/metadata/<mytv_id>/credits`, `/metadata/<mytv_id>/similar`. `mytv_id` is MyTV-owned and permanent; the original
Douban reference stays available as `douban_id` and `legacy_ref`, so favourites, history and playback bindings
keep working.

Host directories: `/srv/mytv/metadata/snapshots` holds the source snapshot, `/srv/mytv/metadata/db` holds
`metadata-latest.sqlite` (what the API reads), and `/srv/mytv/metadata/build` holds staged builds.

```sh
cd /srv/mytv
# rebuild, publish and self-check
docker compose -f deploy/selfhosted/compose.yaml --profile ops run --rm metadata-builder rebuild
# inspect the current database and manifest
docker compose -f deploy/selfhosted/compose.yaml --profile ops run --rm metadata-builder stats
# re-verify the published hashes without rebuilding
docker compose -f deploy/selfhosted/compose.yaml --profile ops run --rm metadata-builder verify
```

`mytv-metadata-refresh.service` and its timer run harvest, rebuild and publish daily at 05:30 Asia/Shanghai
(21:30 UTC on this host). The harvest step is best effort: if Douban rate-limits the VPS the previous snapshot
stays in place and the rebuild still runs. A rebuild whose catalogue content is unchanged publishes nothing,
so an idle day does not create an empty version. Browsers need the migration CA trusted before they can open
the HTTPS metadata URLs directly.
