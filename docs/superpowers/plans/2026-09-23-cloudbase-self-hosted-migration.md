# CloudBase Self-Hosted Migration Implementation Plan

> **状态：已被 2026-09-24 的自建服务器与 CloudBase 退役计划取代。** 仅把这里已完成的 VPS/Compose/健康检查步骤作为历史证据参考；不要执行本计划的旧 APK 兼容代理、旧 CloudBase 管理桥或 2026-10-09 兼容期要求。当前实施入口见 docs/superpowers/plans/2026-09-24-mytv-selfhosted-retirement.md。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 My TV 的 CloudBase 数据库、对象存储、管理/API、激活、热更新、电视源和网页迁到现有 Ubuntu VPS，并保留旧 App 的 CloudBase 地址作为过渡代理。

**Architecture:** 使用轻量 Docker Compose 服务：PostgreSQL + PostgREST + 本地认证签发服务 + 业务函数/API + HTTPS 反向代理；静态对象保存到宿主机持久化目录并提供 Range 下载。先并行迁移和验收，再切换新客户端；CloudBase 在兼容期只转发旧客户端请求。

**Tech Stack:** Ubuntu Server 24.04.1 x86_64、Docker Compose、PostgreSQL（与 CloudBase 主版本匹配）、PostgREST、Python 3.12、现有 Node.js HTTP 函数、Nginx、PowerShell/OpenSSH 运维。

**Spec:** `docs/superpowers/specs/2026-09-23-cloudbase-self-hosted-migration-design.md`

## Global Constraints

- 第一阶段不扩容；目标主机约 4 GB RAM、29 GB 系统盘，约 27 GB 可用。
- 不配置安全组；公网入口全部使用服务商 NAT 端口转发。
- 数据库和对象存储迁移后仅新服务器接受写入，不做双主写入。
- 已安装 App 的 CloudBase 端点在兼容期必须继续可用。
- 仍在使用的 CloudBase `/admin/*` 入口在切换后只转发到新服务器，禁止继续写 CloudBase。
- 私钥、服务账号密码、会话密钥不得进入 Git、镜像、日志或文档。
- 迁移前后核对表行数、序列状态、对象数、对象大小与 SHA-256。
- PostgreSQL RPC 的管理权限、`auth.uid()` 身份语义和客户端协议必须保持或明确替代。

## Review Focus

- 卡密激活、续期与设备绑定并发时只允许一个有效状态；给激活 RPC 加并发契约测试。
- 公开热更新下载支持 Range 和断点续传；给文件服务加 Range/越界路径测试。
- 管理会话过期、无效 JWT 或未授权调用不得执行写 RPC；给认证兼容层加拒绝测试。
- 迁移中新旧端点同时可访问时只能有一个写主；给迁移状态脚本加门禁测试。
- NAT 域名/端口变化不得让旧版 App 指向错误入口；给客户端端点配置和旧代理加契约测试。

---

### Task 1: 新服务器 Compose 基础环境与健康检查

**Files:**
- Create: `deploy/selfhosted/compose.yaml`
- Create: `deploy/selfhosted/nginx.conf`
- Create: `deploy/selfhosted/.env.example`
- Create: `deploy/selfhosted/README.md`
- Create: `tools/selfhosted/health.py`
- Test: `tools/tests/test_selfhosted_health.py`

**Interfaces:**
- Health endpoint: `GET /health` returns HTTP 200 and `{"status":"ok"}` only after required local dependencies are reachable.
- Compose services expose PostgreSQL only on the internal Compose network; Nginx is the only public web entry.

- [ ] **Step 1: Add failing health and configuration tests**

  Test `/health` response, required environment-variable validation, PostgreSQL not published to host ports, and persistent mount declarations.

- [ ] **Step 2: Run the focused tests and confirm failure**

  Run: `python -m unittest discover -s tools/tests -p "test_selfhosted_health.py" -v`  
  Expected: FAIL because the health module and Compose files do not exist yet.

- [ ] **Step 3: Add the minimal Compose and health implementation**

  Use a private bridge network, named persistent database volume, bind-mounted object directory, restart policies, bounded log rotation, and a reverse proxy health route. Keep secrets in a host-only `.env` file with mode `0600`; `.env.example` contains names and safe sample values only.

- [x] **Step 4: Validate the Compose configuration and health route**

  Run: `docker compose -f deploy/selfhosted/compose.yaml config --quiet` and `python -m unittest discover -s tools/tests -p "test_selfhosted_health.py" -v`.  
  Expected: both succeed; `docker compose ps` shows healthy services after launch; restarting containers preserves mounted data.

- [x] **Step 5: Install the baseline on the VPS**

  Use the SSH external port already supplied by the NAT panel. Check available memory/disk, install Docker Engine and Compose plugin from Ubuntu's supported repository, copy the Compose files, create host-only secrets, then start only the health-check stack. Do not change security groups or CloudBase traffic.

### Task 2: PostgreSQL schema, RPC, and service authentication compatibility

**Files:**
- Create: `tools/selfhosted/auth_service.py`
- Create: `tools/selfhosted/db_bootstrap.sql`
- Create: `tools/selfhosted/import_cloudbase.py`
- Modify: `tools/*_migration.sql` only where a CloudBase-specific auth/storage dependency requires a compatible replacement
- Test: `tools/tests/test_selfhosted_auth.py`
- Test: `tools/tests/test_selfhosted_db_contract.py`

**Interfaces:**
- `POST /auth/v1/signin` accepts the existing service-login JSON and returns a signed bearer token with stable subject and expiry fields.
- PostgREST RPC remains under `/v1/rdb/rest/rpc/{name}` for existing service adapters.
- `auth.uid()` reads the authenticated request subject; management RPCs accept only the configured service identity.
- Import command supports schema-only, data-only, and full restore modes and prints counts, never row contents or credentials.

- [ ] **Step 1: Add failing tests for login, JWT claims, authorization, and migration counts**

  Cover valid/invalid password, token expiry, tampered token, service subject allowlist, all existing migration files applying in order, row counts and sequence maxima.

- [ ] **Step 2: Run focused tests and confirm failure**

  Run: `python -m unittest discover -s tools/tests -p "test_selfhosted_auth.py" -v` and `python -m unittest discover -s tools/tests -p "test_selfhosted_db_contract.py" -v`.  
  Expected: FAIL because the self-hosted auth and import modules do not exist.

- [ ] **Step 3: Implement the auth compatibility layer and DB bootstrap**

  Use Python standard-library HMAC-SHA256 JWT signing and a salted PBKDF2 password hash. Create least-privilege `anon` and `authenticated` roles, the text-returning `auth.uid()` compatibility function, and separate service identities. Read signing keys only from a protected host secret file/environment variable.

- [ ] **Step 4: Implement repeatable CloudBase export/import and verify it on an empty local DB**

  Query the current PostgreSQL major version and use the same major image on the VPS. Export schema/data using the available CloudBase SQL tooling; apply migrations in filename order; restore data and sequences without printing ticket/device rows. Produce a machine-readable count report.

- [ ] **Step 5: Run contract and data-integrity tests**

  Run: `python -m unittest discover -s tools/tests -p "test_selfhosted_auth.py" -v`, `python -m unittest discover -s tools/tests -p "test_selfhosted_db_contract.py" -v`, and `python -m unittest discover -s tools/backend-src/tests -p "test_activate_v2_contract.py" -v` plus `python -m unittest discover -s tools/backend-src/tests -p "test_admin_reset_contract.py" -v`.  
  Expected: all pass; denied identities cannot invoke management RPCs, while authorized service calls retain existing response shapes.

### Task 3: Local object storage and CloudBase storage API replacement

**Files:**
- Create: `tools/selfhosted/storage_service.py`
- Create: `tools/selfhosted/storage_manifest.py`
- Create: `tools/tests/test_selfhosted_storage.py`
- Modify: `tools/admin_server.py`
- Modify: `tools/apk_update.py`
- Modify: `cloudfunctions/hotupdate/index.js`

**Interfaces:**
- Public object URL: `/v1/storages/object/{bucket}/{object_key}` supports `GET`, `HEAD`, and byte-range requests.
- Authenticated upload/delete API accepts only configured buckets and normalized object keys.
- Manifest generation preserves current fields and uses the new public object URL for new App builds.

- [ ] **Step 1: Add failing tests for range, content hash, auth, and path traversal**

  Cover full download, `Range: bytes=0-1023`, unsatisfiable range, `HEAD`, anonymous upload rejection, authenticated upload, `../` and encoded traversal rejection, and SHA-256 manifest output.

- [ ] **Step 2: Run the focused tests and confirm failure**

  Run: `python -m unittest discover -s tools/tests -p "test_selfhosted_storage.py" -v`  
  Expected: FAIL because the file service does not exist.

- [ ] **Step 3: Implement local durable object handling**

  Store bucket objects below one configured data root; canonicalize paths before opening files; stream responses without loading whole APKs into memory; implement single-range byte serving and ETag/length headers.

- [ ] **Step 4: Point admin uploads and manifests at the configurable storage base**

  Replace CloudBase host allowlists with explicit allowed self-host origin configuration; keep CloudBase as a separate compatibility origin only in bridge code. Preserve APK upload validation, chunk commit verification, hotupdate version monotonicity, and IPTV object naming.

- [ ] **Step 5: Migrate and verify the existing 14 objects**

  Download each object into a protected staging directory, upload/copy it to the new object root, compare object key, byte length, and SHA-256, then emit a private reconciliation report.

### Task 4: Move business APIs and admin/web services onto the new stack

**Files:**
- Modify: `tools/admin_server.py`
- Modify: `tools/db_import.py`
- Modify: `cloudfunctions/activate/index.js`
- Modify: `cloudfunctions/hotupdate/index.js`
- Modify: `cloudfunctions/iptv/index.py`
- Modify: `cloudfunctions/iptv-rebuild/index.py`
- Modify: `tools/scf_main.py`
- Modify: `tools/build_admin_function.py`
- Modify: `tools/backend-src/proxy.py`
- Create: `tools/selfhosted/scheduler.py`
- Test: `tools/backend-src/tests/test_selfhosted_api_contract.py`
- Test: `tools/tests/test_cloudbase_admin_bridge.py`

**Interfaces:**
- Preserve `/activate`, `/hotupdate/check`, `/hotupdate/report`, `/hotupdate/announce`, `/hotupdate/vod-sources/check`, `/iptv/latest`, `/admin/`, `/web/`, and existing `/api/*` response contracts.
- After the final data cutover, the existing CloudBase `/admin/*` route proxies the admin page and `/api/*` calls to the VPS, preserving session cookies; it must not execute CloudBase database logic.
- The scheduler exposes a single-run lock and cannot overlap manual IPTV publish operations.

- [ ] **Step 1: Add API contract tests using existing CloudBase functions as fixtures**

  Record sanitized request/response fixtures for activation v2, hotupdate check/report/announcement/VOD config, IPTV latest, admin RPC, and APK manifest. Exclude secrets, license codes, and device IDs.

- [ ] **Step 2: Run the new contract suite and establish expected behavior**

  Run: `python -m unittest discover -s tools/backend-src/tests -p "test_selfhosted_api_contract.py" -v`.  
  Expected: tests first fail against missing self-host route adapters.

- [ ] **Step 3: Rewire business functions to local PostgREST and storage configuration**

  Read all service hosts from environment. Keep payload validation, response codes, caches, blackout switch, object URL generation, and CORS behavior. Add an explicit production configuration check that rejects empty/default credentials and CloudBase URLs in the new stack.

- [ ] **Step 4: Move scheduled IPTV rebuild to a single-run local scheduler**

  Preserve source selection and publish protocol; use a database advisory lock around rebuild/publish so a scheduled run cannot overwrite a concurrent admin publish.

- [ ] **Step 5: Run focused contracts and backend regression suite**

  Run: `python -m unittest discover -s tools/backend-src/tests -p "test_selfhosted_*.py" -v` then `python -m unittest discover -s tools/backend-src/tests -v`.  
  Expected: all pass; test output contains no secrets or production row data.

### Task 5: New client endpoints and old CloudBase compatibility bridge

**Files:**
- Modify: `android/app/build.gradle`
- Modify: `internal/mobile/app/build.gradle`
- Modify: `internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/IptvRepository.kt`
- Modify: `tools/backend-src/proxy.py`
- Modify: `tools/admin_server.py`
- Modify: `cloudfunctions/activate/index.js`
- Modify: `cloudfunctions/hotupdate/index.js`
- Modify: `cloudfunctions/iptv/index.py`
- Test: `tools/backend-src/tests/test_endpoint_migration_contract.py`

**Interfaces:**
- New APKs receive the new HTTPS origin through the Gradle property `apiBaseUrl`, passed as `-PapiBaseUrl=$env:OTV_API_BASE_URL`; local dev builds retain the current default until cutover.
- Old CloudBase functions forward only the documented public routes to the new server and do not read/write CloudBase business tables or buckets after cutover.
- Large legacy-client downloads remain mirrored in CloudBase object storage during the compatibility window. Do not stream APK or hot-update binaries through CloudBase functions.

- [ ] **Step 1: Add failing tests for build endpoint injection and bridge forwarding**

  Verify both APK builds accept one configured origin for activation, hotupdate, and IPTV; verify CloudBase bridge preserves method, path, query, selected headers, body, status, and response bytes, while rejecting unlisted paths.
  Verify the legacy `/admin/*` proxy maps to the VPS admin root, preserves login cookies, caps requests/responses to CloudBase-sized payloads, and rejects traversal and unrelated service routes.

- [ ] **Step 2: Implement configurable client endpoint injection**

  Read Gradle property `apiBaseUrl`; keep current production URL as default for existing release builds. Pass the configured origin to license, hotupdate, APK manifest, and IPTV URL construction without altering unrelated playback sources.

- [ ] **Step 3: Implement narrow CloudBase reverse-proxy handlers**

  Proxy activation, hot-update checks/reports, and IPTV reads to the VPS. Keep APK manifests and large download parts in CloudBase object storage for old clients; mirror the exact APK/hot-update objects there. Set the upstream origin from protected function environment and reject loops back to CloudBase.
  Also make the existing `otv-admin` CloudBase function a fixed HTTPS proxy for only the admin page and `/api/*` routes. Verify the bundled migration CA, preserve session cookies, and never execute the old CloudBase database code after promotion.

- [ ] **Step 4: Validate endpoint and proxy contracts**

  Run: `python -m unittest discover -s tools/backend-src/tests -p "test_endpoint_migration_contract.py" -v` and `python -m unittest discover -s tools/backend-src/tests -p "test_activate_v2_contract.py" -v`.  
  Expected: both new and legacy routes return equivalent protocol bodies; disallowed routes return 404.

### Task 6: Staged data cutover, client release, and CloudBase retirement evidence

**Files:**
- Create: `tools/selfhosted/cutover.py`
- Modify: `docs/02-guides/开发与发布手册.md`
- Modify: `docs/04-records/版本史.md`
- Modify: `docs/04-records/测试记录.md`
- Test: `tools/tests/test_selfhosted_cutover.py`

**Interfaces:**
- Cutover state is one of `cloudbase_primary`, `maintenance`, `selfhosted_primary`, or `rollback`; invalid transitions are rejected.
- A cutover report includes table/object counts, object SHA-256 results, service health checks, and rollback checkpoint identifiers without secrets.

- [ ] **Step 1: Add failing tests for allowed cutover and rollback transitions**

  Cover CloudBase primary → maintenance → self-hosted primary, rollback before writes, refusal to silently switch after new-side writes, and report redaction.

- [ ] **Step 2: Implement repeatable migration and reconciliation commands**

  `tools/selfhosted/cutover.py` must support `snapshot`, `sync`, `verify`, `promote`, and `rollback-check` commands. `promote` requires all verification gates and a maintenance marker; it never deletes source data.

- [ ] **Step 3: Complete staging acceptance before touching production writes**

  Require HTTPS reachability on the service-provider NAT mapping; test admin login, activation/renewal/restore, hotupdate range download/report, IPTV publication/rollback, VOD source publishing, APK manifests, and web pages. Build and install debug TV/mobile packages using the new origin.

- [ ] **Step 4: Perform the production cutover**

  Capture a final CloudBase snapshot and object manifest; pause all CloudBase admin writes; import the final delta; verify counts/hashes; deploy CloudBase `activate`, `hotupdate`, `iptv`, and `otv-admin` compatibility forwarding; promote the VPS as sole writer; confirm new logs and no CloudBase business writes. Keep CloudBase APK/hot-update object mirrors until the environment cutoff.

- [ ] **Step 5: Build and validate release APKs and public endpoints**

  Run both `assembleRelease` builds with `apiBaseUrl=https://103.236.97.137:35208`; inspect merged endpoint constants, verify the package signer and embedded backend assets, publish manifests on the VPS, mirror the APK parts/manifests to CloudBase for legacy clients, and test activation, hotupdate, IPTV, and APK update flow.

- [ ] **Step 6: Maintain compatibility and retire only with evidence**

  Keep legacy proxy routes while they receive old-client traffic. Add a documented observation window and request-count threshold agreed with the user; archive final backups before disabling the bridge. Update deployment and test records with non-secret evidence.

## Execution Decisions and Current Evidence

- The user authorized the agent to decide migration details. Use `https://103.236.97.137:35208` (provider NAT to internal TCP 8443) and health port 25182 (to internal 8080); do not alter security groups or NAT.
- Ruling: include the active CloudBase `otv-admin` route in the compatibility bridge — why: leaving its current implementation after promotion would permit a second writer against CloudBase, while proxying only admin routes preserves the existing browser entry and keeps the VPS as the sole writer — cost if wrong: admin requests through CloudBase fail until the legacy proxy code is restored.
- CloudBase currently reports service cutoff on 2026-10-09. Complete legacy-client upgrades before then; the VPS certificate is valid through 2028-12-26.
- Ruling: keep APK and hot-update files mirrored in CloudBase object storage until cutoff. Tencent Cloud documents a 6 MB synchronous function response ceiling; APK parts and hot-update archives exceed it, so function-based binary streaming would fail. Cost if wrong: duplicate object storage during the transition window.
- Latest CloudBase export has one more `activate_log` row than the staging VPS. Perform and verify a final snapshot sync immediately before promoting the VPS.
- Keep a date-stamped local backup outside Git before promotion. No off-host backup destination was configured during staging.

## Execution Method

Native implementation in the current task; no delegated workers. Each task is delivered and verified before the next dependent task. Production database promotion and public routing are separate final steps after staging checks.
