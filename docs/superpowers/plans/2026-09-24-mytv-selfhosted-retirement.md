# My TV 自建服务器与 CloudBase 退役实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让当前支持的 TV APK、移动 APK、Web、管理后台和发布流程只使用自建服务器，并在所有端到端验收通过后退役当前 CloudBase 生产入口。

**Architecture:** 保留已完成的 PostgreSQL、对象存储、metadata center 和 Compose 基础；清理 CloudBase 生产配置与发布副作用；用经公共 CA 验证的 HTTPS 域名作为正式入口。源 API 出网、域名、证书和服务器恢复能力分别取证，未通过时不切流量也不宣称完成。

**Tech Stack:** Ubuntu 24.04、Docker Compose、PostgreSQL、Nginx、Python、Gradle、PowerShell、SSH/SCP。

**Spec:** docs/superpowers/specs/2026-09-24-mytv-selfhosted-retirement-design.md

## Global Constraints

- 当前受支持的生产路径包括 TV APK、移动 APK、Web、后台和发布流程，且均不依赖 CloudBase。
- 用户已明确已发布旧 APK 不纳入本轮兼容或强制升级范围。
- 公网 health 200 不能代替 VPS 源 API 出口验收。
- IP 与迁移 CA 只作为测试入口；生产验收必须使用正式域名和公共 CA 证书。
- 私钥、卡密、Cookie 和服务密钥不得写入 Git、镜像、普通日志或文档。
- CloudBase 不作为默认回滚目标；回滚使用已验证的 Git 提交、自建容器镜像、异机备份和对象校验清单。

## Review Focus

- 打包后仍包含可执行 CloudBase 地址或默认回退：由 Task 1 的构建产物扫描与发布器假执行测试覆盖。
- Release APK 仍指向 IP/迁移 CA 或错误域名：由 Task 2 的双工程 release 配置测试覆盖。
- VPS 能返回 health，却无法访问启用的片源 API：由 Task 3 的 VPS 本机出口报告覆盖。
- 部署中途失败留下半新半旧容器或静态文件：由 Task 4 的原子部署、健康检查及回滚测试覆盖。
- 在自建服务可恢复前关闭 CloudBase 入口：由 Task 5 的切换状态门禁与备份恢复演练覆盖。

## Plan Set Execution Order

1. 先采集当前基线并确认自建数据备份可恢复。
2. 本计划与 Resolver 服务端计划可以并行实现；本计划只负责服务/域名/发布，不改 Resolver 业务代码。
3. TV/移动与 Web 计划在 Resolver API 合同稳定后实现。
4. 最终服务器部署、功能矩阵、截图、CloudBase 生产入口退役和发布按序执行；最终切换不得并行。

---

### Task 1: 清除当前版本发布链中的 CloudBase 生产路径

**Files:**
- Modify: tools/release_all.py
- Delete: tools/tests/test_release_all_cloudbase_update.py
- Delete: tools/build_web_function.py
- Delete: tools/build_admin_function.py
- Delete: tools/pack_bridge_function.py
- Delete: tools/scf_main.py
- Delete: tools/fn_env.py
- Delete: tools/cloudrun-admin/Dockerfile
- Delete: tools/cloudrun-admin/build_context.py
- Modify: tools/admin_server.py
- Modify: tools/apk_update.py
- Delete: tools/db_import.py
- Create: tools/selfhosted/import_licenses.py
- Create: tools/tests/test_selfhosted_license_import.py
- Delete: tools/tests/test_cloudbase_bridge_packaging.py
- Delete: tools/tests/test_cloudbase_admin_bridge.py
- Delete: tools/tests/test_cloudbase_iptv_bridge_contract.py
- Delete: cloudfunctions/
- Modify: deploy/selfhosted/compose.yaml
- Modify: deploy/selfhosted/.env.example
- Modify: deploy/selfhosted/README.md
- Test: tools/tests/test_selfhosted_admin_config.py
- Create: tools/tests/test_current_release_has_no_cloudbase.py

**Interfaces:**
- Consumes: 后端和 Web 本地产物、已配置的自建部署目标。
- Produces: release_all.py 的部署动作只调用自建部署入口；本地打包不要求 CloudBase CLI。
- Resolver 对 proxy.py 中的 CloudBase 点播兜底负责；本任务不修改该文件。

- [ ] **Step 1: 写失败的发布链合同测试**

```python
def test_release_deploy_never_invokes_cloudbase_cli(fake_runner, release_root):
    run_release(release_root, deploy=True, runner=fake_runner)
    assert not any("tcb" in str(call) or "cloudbase" in str(call).lower()
                   for call in fake_runner.calls)
```

断言同时覆盖 deploy 参数、包构建清单和最终命令；测试不得连接生产服务器。另加一条构建包检查，允许迁移导入工具和标记为退役的历史文档存在，但当前 release 清单、Compose 环境和 APK 配置不得包含 CloudBase API、relay、对象镜像或管理入口。

- [ ] **Step 2: 运行新增测试并确认它因现行 CloudBase 发布命令失败**

Run: python -m pytest tools/tests/test_current_release_has_no_cloudbase.py -q

Expected: FAIL，测试报告当前发布器仍构造 CloudBase CLI 命令或把 CloudBase 变量加入运行环境。

- [ ] **Step 3: 改写发布流程并移除旧合同**

修改 tools/release_all.py，使本地构建和自建部署不调用 CloudBase 函数/桥接构建器，也不探测 CloudBase CLI。用 test_current_release_has_no_cloudbase.py 覆盖新发布合同并删除旧 test_release_all_cloudbase_update.py。删除只验证旧 bridge/SCF 行为的测试、不再调用的 CloudBase 部署器和 cloudfunctions/ 下可部署函数源码。CloudBase 卡密批次导入替换为 tools/selfhosted/import_licenses.py，保留 license_gen.py 的批次格式与校验、改为写入自建数据库；同步更新 tools/admin_server.py 与 tools/apk_update.py 的过期 CloudBase 对象/管理说明。Web 源码由 Web 计划的新静态资源构建步骤产出，不再写入 cloudfunctions/。Compose 与示例环境移除 CloudBase relay、点播兜底、对象镜像和管理入口变量。保留 tools/selfhosted/import_cloudbase.py、database_reconcile.py 和必要 SQL 作为显式的一次性迁移/审计工具，不把它们纳入普通打包/部署/启动路径。

- [ ] **Step 4: 运行发布与配置合同测试**

Run: python -m pytest tools/tests/test_current_release_has_no_cloudbase.py tools/tests/test_selfhosted_admin_config.py tools/tests/test_selfhosted_nginx_config.py -q

Expected: PASS；fake runner 的命令仅包含已批准的自建部署目标，不读写 CloudBase 环境变量。

- [ ] **Step 5: 检查发布清单并提交本任务**

Run: rg -n -i "cloudbase|tcloudbase|tcb " tools/release_all.py deploy/selfhosted

Expected: 无当前 release/Compose 可执行依赖；迁移工具和标记为退役的记录应位于本命令扫描范围之外。提交消息：refactor(release): remove cloudbase production path。

### Task 2: 为所有当前 Release APK 固定正式 HTTPS 入口

**Files:**
- Modify: android/app/build.gradle
- Modify: internal/mobile/app/build.gradle
- Modify: android/app/src/main/java/com/qiubo/optimaltv/hotupdate/HotUpdateManager.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/repo/IptvRepository.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/hotupdate/HotUpdateManager.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/IptvRepository.kt
- Test: tools/tests/test_release_signing_config.py
- Create: tools/tests/test_release_api_origin_contract.py

**Interfaces:**
- Consumes: Release 构建传入的 apiBaseUrl 属性。
- Produces: 所有实际存在的 Release APK 均写入同一个经验收的正式 HTTPS origin；Debug 测试可以显式使用测试入口。

- [ ] **Step 1: 添加两套 Gradle 工程的失败配置测试**

```python
def test_release_variants_require_explicit_https_origin(gradle_projects):
    for project in gradle_projects:
        result = assemble_release(project, api_base_url=None)
        assert result.failed
        assert "apiBaseUrl" in result.output
```

再测试 HTTP、含路径的 URL、迁移 CA 测试地址和 TV/移动 origin 不一致时 Release 任务失败；扫描全部实际 APK 源代码，断言 activation、license、hotupdate 与 IPTV 请求均由 BuildConfig.API_BASE_URL/自建配置提供，没有硬编码 CloudBase origin。测试工程列表取 tools/sync_shared.py --list 实际发现的目录，不把不存在的版本目录伪装为已验证。

- [ ] **Step 2: 确认配置测试在当前默认 IP/迁移 CA 设置下失败**

Run: python -m pytest tools/tests/test_release_api_origin_contract.py tools/tests/test_release_signing_config.py -q

Expected: FAIL，输出指出正式 Release 尚未由显式公共 HTTPS origin 驱动。

- [ ] **Step 3: 在两个 APK 工程中实现同一 Release origin 校验**

Release 任务只接受显式 apiBaseUrl=https://正式域名，拒绝 IP、非 HTTPS、路径/查询/片段和测试 CA origin。TV 与移动工程共用同一校验规则和失败信息；HotUpdateManager、license 与 IptvRepository 只从统一 BuildConfig endpoint 取路由，删除过期 CloudBase 注释和硬编码域名。Debug 仍可由测试者显式选择自建测试 origin。正式域名未取得前，只运行负例配置测试，不生成宣称可发布的 Release APK。

```groovy
def uri = new URI(apiBaseUrl)
if (releaseTask && (!isPublicHttpsDomain(uri) || uri.rawPath || uri.query || uri.fragment)) {
    throw new GradleException("Release requires one verified public HTTPS origin")
}
```

- [ ] **Step 4: 运行 Gradle 合同测试并提交**

Run: python -m pytest tools/tests/test_release_api_origin_contract.py tools/tests/test_release_signing_config.py -q

Expected: PASS，全部当前 Release 工程拒绝无效入口并接受同一个 HTTPS 域名。提交消息：build(android): require verified self-hosted release origin。

### Task 3: 从 VPS 验收出口、域名、TLS、对象和恢复能力

**Files:**
- Modify: tools/selfhosted/health.py
- Modify: deploy/selfhosted/nginx.conf
- Modify: deploy/selfhosted/README.md
- Create: tools/selfhosted/network_acceptance.py
- Create: tools/tests/test_selfhosted_network_acceptance.py

**Interfaces:**
- Consumes: VPS 本机的启用来源配置、正式域名、证书状态和对象清单。
- Produces: 带时间、目标、DNS/TCP/TLS/HTTP 状态、重定向及耗时的验收 JSON/Markdown；不改变线上配置。

- [ ] **Step 1: 为探测器编写无网络的失败合同测试**

测试用本地假 DNS/HTTP/TLS 适配器覆盖解析失败、连接拒绝、证书域名不符、超时、重定向、Range 206、ETag 和对象 SHA-256 不符。另测来源频率限制：同一来源串行且使用配置间隔，不并发轰击来源。

- [ ] **Step 2: 运行本地合同并确认新探测器尚未实现**

Run: python -m pytest tools/tests/test_selfhosted_network_acceptance.py -q

Expected: FAIL，原因是验收入口或探测结果字段不存在。

- [ ] **Step 3: 实现只读 VPS 网络验收和安全报告**

探测目标由当前后台启用的 tools/backend-src/vod_sources.default.json 派生，并覆盖每个启用源的 DNS、TCP、TLS、列表/搜索、详情、播放地址解析；对代表性合法样本抽测视频 CDN、HLS manifest 和首个分片。记录 IPv4/IPv6、重定向、各阶段耗时、状态和错误类别。不得绕过登录、访问控制或反爬；报告与日志不得包含完整签名 URL、Cookie 或授权头。新增正式域名 DNS/公共证书/自动续期检查、容器重启恢复、PostgreSQL 备份恢复、磁盘/内存、对象数量/大小/hash 抽检和 Range/ETag 检查；性能报告同时记录 Postgres connections、CPU/RAM/disk I/O/network 和 Nginx 499/502/504。

```python
report = {
    "origin_checks": probe_public_origin(domain),
    "source_checks": probe_enabled_sources_from_vps(active_sources),
    "object_checks": verify_range_etag_and_hash(object_manifest),
    "host_metrics": collect_postgres_nginx_host_metrics(),
}
```

- [ ] **Step 4: 运行本地合同和部署配置回归**

Run: python -m pytest tools/tests/test_selfhosted_network_acceptance.py tools/tests/test_selfhosted_health.py tools/tests/test_selfhosted_nginx_config.py tools/tests/test_selfhosted_database_reconcile.py -q

Expected: PASS；本地测试不伪装为 VPS 实测。只有通过经 SSH 登录的目标 VPS 执行同一只读报告，才把出口和容量状态记为 PASS。

- [ ] **Step 5: 保存基线证据并提交**

将目标机实测 JSON、脱敏的摘要和恢复演练结果保存到 artifacts/acceptance/2026-09-24/selfhosted/，只提交不含凭证的 README 与报告模板。缺正式域名、公共证书或 VPS shell 时记录 BLOCKED 和缺失项，不以公网 health 200 替代。提交消息：feat(selfhosted): add production network acceptance report。

### Task 4: 实现健康门控、自建原子部署与自建回滚

**Files:**
- Modify: tools/release_all.py
- Create: tools/selfhosted/deploy_release.py
- Modify: deploy/selfhosted/compose.yaml
- Modify: deploy/selfhosted/Dockerfile.web
- Modify: deploy/selfhosted/README.md
- Create: tools/tests/test_selfhosted_deploy_transaction.py

**Interfaces:**
- Consumes: release_all.py 的已验证本地产物、Task 3 的网络/容量状态及部署目标。
- Produces: staging 与 production 的版本化自建部署、健康检查结果、可回滚的镜像/对象清单。

- [ ] **Step 1: 用假 SSH/Docker runner 写部署事务测试**

覆盖 build/upload 失败、容器启动失败、health 失败、对象校验失败、完整成功和 rollback。验证临时版本目录只有全部 hash/health 校验通过后才原子切换为 current；任何失败均保留上一版并输出非零状态。

- [ ] **Step 2: 运行测试并确认当前部署器缺少事务保证**

Run: python -m pytest tools/tests/test_selfhosted_deploy_transaction.py -q

Expected: 新测试因部署状态记录、健康门控或回滚行为缺失而 FAIL。

- [ ] **Step 3: 新建独立的安全部署器并复用切换状态门禁**

新建 tools/selfhosted/deploy_release.py；tools/selfhosted/cutover.py 只负责数据切换/验收状态，不能充当部署器。部署器打包时调用 Web 计划新增的 tools/build_web_assets.py，再构建 backend zip 与容器镜像；Dockerfile.web 只复制该 builder 生成的 dist，不复制 CloudBase/SCF 函数包。凭证仅从 SSH agent、用户 SSH config 或环境变量读取，不写入命令日志、Git 或报告。应用镜像、Web 哈希资源、metadata manifest 与对象先上传暂存区，执行 Compose 配置检查、服务 health、激活/热更新/IPTV/Web/metadata/VOD/Range 合同和 SHA-256 检查后切换。失败时恢复上一版自建镜像与对象指针，生成含阶段及错误类别的脱敏报告。

```python
stage = upload_versioned_bundle(bundle)
validate_compose(stage)
if not run_health_and_contracts(stage) or not verify_sha256(stage):
    restore_previous_release()
    raise DeploymentRejected(stage.redacted_failure_report())
promote_atomically(stage)
```

- [ ] **Step 4: 运行本地事务、运维与后端合同**

Run: python -m pytest tools/tests/test_selfhosted_deploy_transaction.py tools/backend-src/tests/test_selfhosted_api_contract.py -q

Expected: PASS；假 runner 全程确认未调用 CloudBase CLI、API 或对象 URL。

- [ ] **Step 5: 对 staging 做一次故障注入与一次成功部署**

使用真实 staging VPS 执行探测；随后注入无效健康状态确认自动回滚，再部署有效构建并确认所有入口/对象 hash 一致。没有目标机访问权时保持 BLOCKED，不把本地 fake runner 结果写成服务器通过。提交消息：feat(selfhosted): gate deploys on health and rollback。

### Task 5: 全端通过后退役 CloudBase 生产入口并归档证据

**Files:**
- Modify: tools/selfhosted/cutover.py
- Modify: tools/tests/test_selfhosted_cutover.py
- Modify: docs/项目文档.md
- Modify: docs/04-records/版本史.md
- Modify: docs/04-records/测试记录.md
- Create: docs/04-records/2026-09-24-自建服务器与CloudBase退役验收.md

**Interfaces:**
- Consumes: Resolver、Android、Web 计划的通过记录，以及 Task 3/4 的真实 VPS 报告。
- Produces: 可审计的 CloudBase 退役记录和只使用自建环境的当前运维说明。

- [ ] **Step 1: 执行退役前检查清单**

确认受支持构建版本、端到端矩阵、截图、真实媒体播放、正式域名/TLS、来源出口、对象 hash、数据库异机恢复、VPS 容量和回滚全部 PASS；检查运行时请求记录 CloudBase 为零。任何失败或未验证项均停止。

```python
required = acceptance_report.required_checks
if any(check.status != "PASS" for check in required):
    raise CutoverBlocked("production retirement requires every required check to pass")
```

- [ ] **Step 2: 退役当前 CloudBase 生产入口**

冻结 CloudBase 生产写入，导出并校验保留所需的数据/对象清单，然后关闭当前版本依赖的函数触发器、托管入口、relay、对象镜像和发布权限。保留项目及数据作为只读归档；本计划不删除 CloudBase 项目、历史数据或旧 APK，也不要求旧 APK继续可用。

- [ ] **Step 3: 进行切换后 smoke test 与零请求复核**

重新安装每种当前 Release APK、清空缓存启动并逐项调用自建激活、热更新、IPTV、metadata、点播解析和对象下载。Web/后台重新登录并执行只读与受控写操作。检查日志、网络抓包或服务访问计数，确认当前支持版本 CloudBase 请求为零。

- [ ] **Step 4: 更新运维记录、复核截图及最终 Diff**

当前手册仅记录自建域名、恢复、发布、回滚和对象流程；旧兼容代理日期标为已退役。保存 TV、移动、Web/后台和 VPS 报告的实际证据链接；复核 git diff 无密钥、临时部署包或 debug 配置。提交消息：docs(release): record cloudbase retirement and self-hosted cutover。

## 未解决的外部门槛

- 需取得并验证正式域名 DNS 控制权和公共 CA 证书/自动续期。
- 需提供可用且只用于运维的 VPS shell/SSH 访问方式；仓库盘点尚未确认正式 SSH alias。
- 需从 VPS 本机通过启用源 API 的出口探测；不能从开发电脑代测。
- 以上条件不满足时可完成代码、单测和本地验收，但不得报告“所有功能通过”、切换生产入口或退役 CloudBase。
