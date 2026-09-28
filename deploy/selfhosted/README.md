# My TV self-hosted stack

This directory contains the self-hosted My TV stack. Only Nginx publishes host ports: provider NAT maps external HTTPS `35208` to internal `8443`, and external health port `25182` to internal `8080`. Do not change security groups or NAT mappings. The health port exposes only `/health`; application traffic uses HTTPS on port `35208`.

The file API allows only the `hotupdate` bucket. Its HTTPS object route is `/v1/storages/object/{bucket}/{key}` and supports `GET`, `HEAD`, and single byte ranges. Upload and deletion require a valid service JWT. Admin pages and business APIs are exposed only through Nginx over the configured HTTPS NAT port. The Admin UI is available at `/` and `/admin/`; `/admin` redirects to `/admin/`, and both entries reach the same static assets and API handlers.

The self-hosted admin runs the existing IPTV source rebuild daily at 06:30 Asia/Shanghai. Scheduled rebuilds, manual publishes, rollbacks, and mode changes share a database lease with a five-minute expiry and 30-second renewal. If the lease RPC is unavailable, publication fails closed; a crashed process releases the lease automatically after expiry.

## Prepare the VPS

1. Install Docker Engine and the Docker Compose plugin from the Ubuntu 24.04 package repository.
2. Check out the approved repository version under `/srv/mytv`; Compose builds the web and admin images from this checkout.
3. Create `/srv/mytv/objects`, `/srv/mytv/secrets`, and `/srv/mytv/secrets/license`. Copy `.env.example` to `.env`; generate separate random values for the Postgres administrator, PostgREST `authenticator` role, and JWT signing key. Create `auth-users.json` with salted PBKDF2 password hashes and a `jwt-signing-key` file containing the same JWT key used by PostgREST and the private file API.
4. Create root-only `admin-credentials.json`, `admin-session-secret`, and `admin-api-credentials.json` files. The admin API account must use the My TV service subject `2094843486709620737`; store only its salted hash in `auth-users.json` and keep its matching username/password in `admin-api-credentials.json`. Create `/srv/mytv/secrets/license-importer-db-password` for the restricted license-import role as described below. The original license-signing key, if available, belongs at `/srv/mytv/secrets/license/private_key.pem`; do not generate a replacement because existing apps trust the original public key.
5. Restrict `.env` and all secret files to root (`chmod 600`); make the object directory writable by the storage service.
6. Create the Web asset archive directory and make it writable by the `web-api` container. The container runs as root, so the host directory must exist and must not be a read-only bind mount, a path whose SELinux label blocks writes, or a regular file:

   ```sh
   mkdir -p /srv/mytv/web-assets
   chown root:root /srv/mytv/web-assets
   chmod 755 /srv/mytv/web-assets
   ```

   Set `WEB_ASSET_ARCHIVE_HOST_PATH` in `.env` when the archive should not live at the default `./data/web-assets`. Compose mounts that host path at `/var/lib/mytv/web-assets` and passes the same location to the entrypoint as `OTV_WEB_ASSET_ARCHIVE`; if the two ever drift, the "persistent" archive silently becomes a container-local directory that dies with the container. When the volume cannot be written, `web-api` aborts before its health port opens and `docker compose logs web-api` reports `Web asset archive directory is not writable: <path>`.
7. In the provider NAT panel, forward an external port to internal TCP port 8080. Do not add or change a security group.
8. From the repository root, run `python tools/release_all.py` to build the Web assets and the embedded backend package. Then start the stack with `docker compose -f deploy/selfhosted/compose.yaml up -d --build` and check `docker compose -f deploy/selfhosted/compose.yaml ps` plus `curl http://127.0.0.1:8080/health`. Before PostgREST starts, the one-shot `schema-migrations` service applies each missing SQL version to the existing PostgreSQL volume and records its filename and timestamp in `public.schema_migrations`.
9. Confirm the Web stack came up with a healthy archive. `GET /api/health` on `web-api` reports `"assets":{"ok":true,"build":"<id>","retained":[...]}`, where `build` is the running build and `retained` lists the generations kept on disk. `ok:false` means the running tree and the archive disagree.

On this deployment host, `python tools/release_all.py --deploy` performs the same local build and then invokes the configured self-hosted Compose deployment entry. The Admin container does not receive the PostgreSQL bootstrap credentials. It mounts only the separate password file for `license_importer`, which can insert issued licenses and read only their `code` column for duplicate detection.

### Prepare the license-import password

Generate a strong password directly into the root-only file mounted by Admin. Keep this file outside the repository and do not add its contents to `.env`:

```sh
sudo sh -c 'umask 077; openssl rand -base64 36 > /srv/mytv/secrets/license-importer-db-password'
```

The `schema-migrations` service reads this file without printing its contents, creates or repairs `license_importer`, and reconciles its grants before PostgREST starts. It records the role policy version and runs a rollback-only SQL smoke check that verifies insert, duplicate import, code-only reads, rejected detail reads, and rejected updates. A missing/empty password file or a failed migration keeps PostgREST and dependent API services stopped. On each Compose rollout, the role password is refreshed from this file, so rotate the file and restart the stack together.

The HTTP health endpoint intentionally serves only `/health`; never send credentials or App traffic to it. The HTTPS certificate is issued for the VPS IP and its migration CA is packaged with both APKs. If the original license-signing key is not installed, license replenishment stays unavailable while existing licenses continue to work.

## Publish an APK from the VPS

The optional `apk-publisher` Compose profile sends upload requests to the private storage service and writes public manifest URLs using `OTV_PUBLIC_BASE_URL`. Copy a signed APK into `/srv/mytv/deploy/selfhosted/releases/`, then run from `/srv/mytv`:

```sh
docker compose -f deploy/selfhosted/compose.yaml --profile ops run --rm apk-publisher \
  --channel tv --apk /releases/MyTV-TV版.apk \
  --version-code 110 --version-name 1.1 --min-supported 100 --force \
  --cert <APK-signing-certificate-SHA-256> \
  --title "后台迁移" --note "后台服务已迁移到新服务器"
```

Use `--channel mobile` for the mobile package. The self-hosted storage service serves APK and hot-update objects; publish each release through this stack so installed clients use the configured self-hosted origin.

## Stop and preserve data

Use `docker compose down` to stop containers. Do not pass `--volumes`; `postgres_data` contains the database. Object files persist under `OBJECTS_HOST_PATH`.

The Web asset archive persists under `WEB_ASSET_ARCHIVE_HOST_PATH` (default `./data/web-assets`) and holds the current and previous Web builds: the content-hashed JS/CSS, the football poster pool, the logo and the web manifest. It exists so that a browser which loaded `index.html` just before a container replacement can still fetch the exact assets that page references, instead of silently mixing in the new release's artwork.

It is a cache, not system of record: every build regenerates the current generation, so a lost archive costs one extra fetch for any page mid-load and no data. Back it up only to speed up rollbacks or preserve a known-good generation across a host migration:

```sh
# back up (safe while the stack runs; each generation is written to a temp dir and renamed)
tar -czf web-assets-$(date +%F).tar.gz -C "$(dirname "${WEB_ASSET_ARCHIVE_HOST_PATH:-./data/web-assets}")" "$(basename "${WEB_ASSET_ARCHIVE_HOST_PATH:-./data/web-assets}")"

# restore into a stopped stack
docker compose down
rm -rf "${WEB_ASSET_ARCHIVE_HOST_PATH:-./data/web-assets}"
tar -xzf web-assets-2026-09-28.tar.gz -C "$(dirname "${WEB_ASSET_ARCHIVE_HOST_PATH:-./data/web-assets}")"
docker compose up -d
```

If a generation in the archive is damaged, the next start discards it, logs the drop and keeps the current build, so a corrupt archive degrades to "one fewer cached generation" rather than an outage. A damaged *current* build is a different matter and stops the container on purpose: that is an image build problem, not a cache problem.

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
