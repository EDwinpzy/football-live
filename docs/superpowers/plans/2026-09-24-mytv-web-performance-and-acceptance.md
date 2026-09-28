# My TV Web 性能与全端验收实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化自建网页版点播体验，并以真实用户端、源站、服务器、截图和指标证据验收 TV APK、移动 APK、Web、后台及发布流程。

**Architecture:** Web 通过自建 HTTPS 拉取 manifest、metadata 和业务 API；首页缓存先显示，详情基础内容与点播解析并行，浏览器直连媒体优先，只有不适用/失败才受控 relay。构建时为 JS/CSS 生成内容哈希路径并设置精确缓存头。统一采集脱敏的页面/点播/播放阶段指标，最终验收不以编译、health 200 或 mock 结果替代真实端到端。

**Tech Stack:** tools/backend-src/www、Python API、Nginx、Chrome、Playwright 或仓库已有浏览器验收工具、Android emulator、pytest。

**Spec:** docs/superpowers/specs/2026-09-24-mytv-web-performance-and-acceptance-design.md

## Global Constraints

- HTML 使用 no-store；内容哈希 JS/CSS 使用长缓存 immutable。
- metadata API P95 ≤ 100ms；业务 API P95 ≤ 200ms；Resolver fast path P95 ≤ 1.5s。
- HTTP 5xx < 0.1%；对象下载支持 Range、ETag、Last-Modified 和正确版本缓存头。
- 媒体流量不强制经过自建服务器；报告 direct/relay 比例。
- API 与 relay 使用分开的 upstream timeout。
- 首页、片库、详情和播放性能阈值采用 Android/Resolver 规格；只记匿名化分析字段，不记姓名、卡密、Cookie、完整播放 URL、授权头或可反推用户的设备标识。
- 截图必须是实际目标端、目标方向/视口、数据已加载且改动可见的页面。
- 任一功能、版本、指标、截图或网络验收失败/未验证时，停止生产发布和最终切换。

## Review Focus

- 旧浏览器缓存继续使用上一版 JS：Task 1 通过哈希文件名与 HTML 引用一致性测试。
- 首页缓存/metadata 更新失败使离线页面变空：Task 2 注入超时、manifest 损坏和 5xx 测试。
- 跨域媒体直连失败后无 relay/重试操作：Task 2 覆盖直接播放、有限 relay、重试和错误恢复。
- 埋点意外保存播放 URL 或稳定设备身份：Task 3 对结构日志和指标响应做禁用字段断言。
- 只测编译/health 而漏掉功能或截图：Task 4/5 用逐项矩阵、实际截图清单和硬性发布门禁覆盖。

## Dependencies

- VOD Resolver 计划先提供 /vod/resolve/{mytv_id}?episode=1、preferred/backups/status/cache/version 合同。
- 静态资源 builder 与 VOD Resolver 的静态 handler 共用 dist/index.html、dist/assets/app.<sha256>.js、dist/assets/style.<sha256>.css、manifest.json 合同；VOD Task 5 先实现安全路由，本文 Task 1 再产出该目录并联调。
- Android 计划接入 metadata SQLite；Web 使用相同 manifest 与 metadata schema，不另造 ID 匹配规则。
- selfhosted retirement 计划先准备 staging 域名、公共证书和 health-gated deploy；正式 CloudBase 退役在本文全端验收之后。

---

### Task 0: 在改动前采集同条件性能基线

**Files:**
- Create: docs/04-records/2026-09-24-全链路优化基线.md
- Create: artifacts/acceptance/2026-09-24/baseline/

**Interfaces:**
- Consumes: 当前已安装 TV/移动构建、当前 Web/后台入口、稳定可测内容和现有网络。
- Produces: 可在最终报告中与新版本逐项复测的设备/视口/内容/网络基线、指标原始记录和真实截图。

- [ ] **Step 1: 在当前版本选择固定媒体样本并记录版本指纹**

固定可合法播放的电影/剧集、HLS/直链、1080p/低清、设备和当前自建 API 版本；记录 APK versionCode/hash、Web JS/CSS hash、metadata version、source index version、设备型号/分辨率/网络。不保存签名 URL、账户、Cookie 或激活卡密。

```python
record = BaselineRecord(
    product="tv",
    build_sha256=sha256_file(apk_path),
    device="emulator-5554",
    viewport="1920x1080",
    network_profile="stable_wifi",
    metadata_version=manifest["data_version"],
    sample_ref=selected_item.canonical_ref,
)
```

- [ ] **Step 2: 记录当前启动、列表、详情、解析、首帧和缓冲**

在当前 TV APK、移动 APK、Chrome desktop 与 mobile viewport 各记录 app/home first content、detail、resolve、prepare、first frame、rebuffer、direct/relay 和 5/30 分钟存活；另保存 TV 横屏主页/片库/详情/播放、移动竖屏片库/详情/播放、Web 首页/搜索或详情/播放的真实截图。

- [ ] **Step 3: 核实基线可复现再冻结**

重复一轮代表性样本；检查同一构建 hash、源引用、网络 profile 和视口后才冻结 baseline。来源 API 不可达、页面空白或截图未显示播放内容时记 NOT-VERIFIED，并保留错误类别；不得用 mock 数据补基线。

### Task 1: 内容哈希资源与可验证缓存合同

**Files:**
- Modify: tools/backend-src/www/index.html
- Modify: tools/backend-src/www/app.js
- Modify: tools/backend-src/www/style.css
- Modify: tools/build_backend_zip.py
- Create: tools/build_web_assets.py
- Test: tools/backend-src/tests/test_web_portrait_player_contract.py
- Create: tools/backend-src/tests/test_web_asset_cache_contract.py

**Interfaces:**
- Consumes: 前端源码与 release 版本。
- Produces: index HTML 引用唯一内容哈希 JS/CSS；HTML no-store；哈希资源 immutable；未哈希文件使用短 TTL。

- [ ] **Step 1: 写资源版本与缓存头合同测试**

```python
first = build_assets(source_dir, tmp_path / "dist")
second = build_assets(source_dir, tmp_path / "dist")
assert first.manifest == second.manifest
assert all((tmp_path / "dist" / path).is_file() for path in first.referenced_files)
```

测试重复构建字节/文件名稳定、改动 JS 后 hash 文件名改变、index 引用资源真实存在、旧 hash 资源可短期并存、HTML no-store、哈希 JS/CSS 为 public,max-age=31536000,immutable、非哈希 app.js/style.css 不超过 300 秒。

- [ ] **Step 2: 运行 Web 静态资源测试确认当前引用和缓存不满足合同**

Run: python -m pytest tools/backend-src/tests/test_web_asset_cache_contract.py tools/backend-src/tests/test_web_portrait_player_contract.py -q

Expected: 新合同测试因资源仍使用固定文件名或缓存时间不精确而 FAIL。

- [ ] **Step 3: 在可重复构建中产生内容哈希资源**

```python
digest = hashlib.sha256(source_bytes).hexdigest()[:16]
asset_name = f"app.{digest}.js"
write_dist_asset(asset_name, source_bytes)
rewrite_html_reference("/assets/" + asset_name)
```

由新 tools/build_web_assets.py 计算实际内容 SHA-256 前缀并重写 index 引用；同一输入构建的名字和字节固定，不同输入不会覆盖旧 URL。builder 输出独立 dist 目录，包含 index、哈希资源和清单；后端 proxy 静态 handler 由 VOD Resolver 计划的 Task 5 挂接，Nginx/proxy 根据文件类型和 hash 精确设置 cache header；普通 HTML 一律 no-store。修改 tools/build_backend_zip.py 使 APK 内置 Web 同样引用完整哈希产物，不修改 CloudBase function builder。

- [ ] **Step 4: 检查两套产物并提交**

Run: python -m pytest tools/backend-src/tests/test_web_asset_cache_contract.py tools/backend-src/tests/test_web_portrait_player_contract.py -q

Expected: PASS；构建出来的实际目录/zip 中 HTML 引用文件均存在，缓存头测试覆盖线上静态路由。提交消息：perf(web): version static assets by content hash。

### Task 2: 先显示页面、接入 Resolver 并保留直连重试

**Files:**
- Modify: tools/backend-src/www/app.js
- Modify: tools/backend-src/www/index.html
- Create: tools/backend-src/tests/test_web_vod_resolver_flow.py
- Modify: tools/backend-src/tests/test_web_portrait_player_contract.py

**Interfaces:**
- Consumes: local/persistent metadata data 和 VOD Resolver JSON 合同。
- Produces: 本地缓存先展示、detail 与 resolve 并行、preferred 起播、后续 backups 增量显示。

- [ ] **Step 1: 写浏览器流程合同测试**

```javascript
const [detail, resolved] = await Promise.all([
  catalog.detail(mytvId),
  api.resolve(mytvId, episode),
]);
assert.equal(player.prepareCalls[0].candidate.source_id, resolved.preferred.source_id);
```

使用假 API/播放器覆盖：缓存首页先可操作、metadata 刷新 5xx 时保留缓存；点击影片立即展示标题/封面/基础信息；resolve 与详情并行；preferred 到达就调用播放器；后续 backups 不重置时间；RESOLVING/FAST_PATH_FAILED/FALLBACK_SEARCHING/STALE_AVAILABLE/TEMPORARILY_UNAVAILABLE/MATCHED 各显示适当状态；无线路时可重试但不造 URL。

- [ ] **Step 2: 运行测试确认旧 detail 等待行为不符合合同**

Run: python -m pytest tools/backend-src/tests/test_web_vod_resolver_flow.py tools/backend-src/tests/test_web_portrait_player_contract.py -q

Expected: 当前页面若等待 /vod/detail?refresh=1 的全部来源，快起播测试 FAIL。

- [ ] **Step 3: 实现 Web 本地优先和 Resolver 消费**

```javascript
renderHome(await cache.readHome(metadataVersion));
refreshHomeInBackground();
const [detail, resolution] = await Promise.all([loadDetail(id), resolveLine(id, episode)]);
if (resolution.preferred) player.prepare(resolution.preferred);
```

先读 IndexedDB/localStorage 中带 metadata_version 的有效首页/详情缓存，再后台检查 manifest。缓存失效不能清空当前内容。点击后并行取详情与 Resolver；候选与播放 URL 区分，播放 URL 仅短暂留在播放器内存中。保留 HTTPS 直连优先；仅无法直连或播放失败时启用现有受控 relay；relay/API timeout 独立。错误状态提供显式重试、线路切换及再次解析。

- [ ] **Step 4: 运行 Web 点播与代理合同并提交**

Run: python -m pytest tools/backend-src/tests/test_web_vod_resolver_flow.py tools/backend-src/tests/test_web_portrait_player_contract.py tools/backend-src/tests/test_vod_api.py -q

Expected: PASS；普通媒体请求不进入 relay；成功、失败、重试、HLS/直链都保持可观测状态。提交消息：feat(web): resolve and play first trusted source。

### Task 3: 统一匿名化事件与性能采集

**Files:**
- Modify: tools/backend-src/www/app.js
- Create: tools/backend-src/tests/test_playback_telemetry_privacy.py
- Create: tools/backend-src/tests/test_playback_metrics_contract.py

**Interfaces:**
- Consumes: app lifecycle、content/resolve/player event。
- Produces: app_start、home_first_content、detail_open、resolve_start、resolve_first_source、play_click、player_prepare、first_frame、buffer_start/end、auto_switch、engine_fallback、play_5min、play_30min、fatal。

- [ ] **Step 1: 写事件字段与敏感值拒绝测试**

```javascript
const event = playbackEvent("first_frame", {
  session_id: shortLivedSessionId,
  source_id: candidate.source_id,
  elapsed_ms: firstFrameMs,
});
assert.equal(JSON.stringify(event).includes(candidate.url), false);
```

测试每条播放会话可计算 detail_ms、resolve_ms、prepare_ms、first_frame_ms、total_click_to_frame_ms 及 start_success_rate、first_frame_p50/p95、rebuffer_count/duration、source_switch_count、relay_ratio、VLC_fallback_ratio、30min_survival_rate。伪造带签名 URL、Cookie、授权头、卡密、账号和稳定设备 ID 的输入，断言不会出现在结构化日志、指标、持久缓存或报表。

- [ ] **Step 2: 实现前端/服务器一致事件名与聚合字段**

```javascript
const SAFE_FIELDS = new Set(["session_id", "content_bucket", "source_id", "state", "elapsed_ms"]);
sendEvent(Object.fromEntries(Object.entries(event).filter(([key]) => SAFE_FIELDS.has(key))));
```

事件只携带会话随机短期 ID、规范 mytv_id 的不可逆匿名桶、source_id、状态类别、缓存类别和耗时。单次事件不得写完整标题、网络 IP、媒体 URL 或账户标识；播放事件通过短生命周期关联 ID 汇总后丢弃。后台 metrics 同时导出请求数、p50/p95/p99、match/cache hit、source timeout/error/circuit、metadata version、Postgres connections、CPU/RAM/disk/network 和 Nginx 499/502/504。

- [ ] **Step 3: 运行隐私与字段合同并提交**

Run: python -m pytest tools/backend-src/tests/test_playback_telemetry_privacy.py tools/backend-src/tests/test_playback_metrics_contract.py -q

Expected: PASS；用户级事件不可被日志恢复成实际身份或播放凭据。提交消息：feat(metrics): add anonymous cross-platform playback timing。

### Task 4: 准备数据/网络媒体样本和全端功能验收记录

**Files:**
- Create: tools/qa/media_matrix.json
- Create: tools/qa/run_acceptance.py
- Create: docs/04-records/2026-09-24-全链路性能验收.md

**Interfaces:**
- Consumes: 已启用合法来源、测试设备、Web 浏览器和 selfhosted staging。
- Produces: 可追溯逐项矩阵、原始结果、截图目录和明确的 PASS/FAIL/NOT-VERIFIED 状态。

- [ ] **Step 1: 建立真实媒体样本清单**

```json
{
  "id": "sample-movie-01",
  "kind": "movie",
  "stream_type": "hls",
  "resolution": 1080,
  "authorized_test": true
}
```

至少登记 10 部电影、10 部剧集、5 个 1080p、5 个低清旧源、5 个 HLS、5 个直链。每项记录脱敏内容引用、来源、类型、分辨率、episode、授权/可测试状态，不把账号凭据或签名 URL 写入仓库。并列出稳定 Wi-Fi、高延迟、10Mbps/5Mbps 限速、1% 丢包、3s/10s 断网的测试方法与实际设备/代理。

- [ ] **Step 2: 实现测试记录器和跨端表格**

每条记录包含产品/构建版本、设备/浏览器/视口、网络、内容类别、次数、首帧、缓冲、自动换线、存活、错误日志与截图路径。功能列表逐项记：首页、片库、筛选、搜索、详情、收藏、历史/进度、episode/线路、直连/relay、自动换线、VLC 回退、续播、IPTV、电视源、公告、激活/license、热更新/版本检查、DLNA/投屏、手势、TV 遥控焦点/返回恢复、Web 搜索/详情/播放/重试、后台配置/发布/回滚/对象上传。

- [ ] **Step 3: 定义发布门禁及负例自测**

```python
required = [row for row in ledger.rows if row.required]
blocked = [row for row in required if row.status != "PASS"]
if blocked:
    return GateResult(allowed=False, blockers=blocked)
return GateResult(allowed=True, blockers=[])
```

报告生成器遇到功能失败、版本缺项、性能指标失败、截图缺失、未测的必测网络/媒体类别、VPS 出口缺证或 CloudBase 当前请求非零时返回非零状态，并拒绝生成“全部通过”摘要。标记 N/A 必须带产品能力说明；不得把未验证写成 N/A。

- [ ] **Step 4: 运行清单工具自测并提交**

Run: python tools/qa/run_acceptance.py --self-test

Expected: self-test 使用本地 fixture 验证通过/失败/未验证三种状态与隐私过滤，不报告真实产品验收通过。提交消息：test(qa): add traceable cross-platform acceptance ledger。

### Task 5: 执行 staging 全矩阵、指标对比和真实截图

**Files:**
- Modify: docs/04-records/2026-09-24-全链路性能验收.md
- Create: artifacts/acceptance/2026-09-24/web/
- Create: artifacts/acceptance/2026-09-24/android/
- Create: artifacts/acceptance/2026-09-24/backend/

**Interfaces:**
- Consumes: Web/Resolver/Android/selfhosted 的已完成构建与测试输出。
- Produces: 同条件前后对比、全版本结果、截图、VPS 和发布链路证据。

- [ ] **Step 1: 先运行服务端与资源检查**

Run: python -m pytest tools/backend-src/tests -q
Run: python -m pytest tools/tests -q
Run: 检查部署 Nginx/config tests 以及 release archive 的资源 hash/cache headers。

Expected: 全部当前生产/自建合同通过；旧 CloudBase bridge 测试应已转为 migration-only，不可被 release 打包或生产路由调用。

- [ ] **Step 2: 对全部实际 Android 工程运行三类构建测试**

Run: python tools/sync_shared.py --list
Run: 每个被列出工程分别执行 .\gradlew.bat testReleaseUnitTest、.\gradlew.bat connectedAndroidTest 和 .\gradlew.bat assembleRelease。

Expected: 每个当前存在的变体都生成可安装 APK 并完成连接设备测试；TV/移动截图显示实际加载内容。旧 APK 不安装、不测、不做迁移验收。

- [ ] **Step 3: 对 staging Web、后台及服务执行端到端矩阵**

至少在 Chrome desktop 1440×900 和 mobile viewport 390×844 对当前 Web 与后台做真实浏览器回归：

- **Web**：首页、足球列表/筛选/焦点比赛、片库分类/筛选/加载更多、搜索、详情/剧集、真实媒体播放、直连失败后的受控 relay/重试/换线、本地收藏/历史/进度刷新恢复、IPTV 分组/频道/切台，以及未授权拦截和隔离测试卡激活/续费/状态刷新。
- **后台**：登录失败与成功、会话刷新/权限、总览、激活日志筛选、卡密查询/状态/解绑/售出/备注/补货/签发/CSV 下载；卡密“生成并入库”须验证导入脚本专用最小权限、签名校验、重复批次幂等，以及实际插入数与页面报告一致；热更与 APK 分片上传/版本清单/哈希/下载、公告发布与读取、电视源导入草稿/频道分组及 Logo 编辑/探测/发布/版本/回滚/模式切换、影视源发现/草稿/启停/排序/单源测试/全量测速/显式发布，并验证失败发布保留旧版本。
- 所有有写入的流程仅操作隔离 staging 数据，执行后逐项读回；对象下载另验 GET/HEAD/Range、ETag 与内容哈希。Web 与后台的每个页面和关键完成态都要有对应视口截图。不得在生产调用破坏性重置接口，也不得把 mock、源码合同测试或只有 health 200 算作浏览器通过。
- TV 1920×1080 emulator 截主页/片库/详情/播放/焦点返回；移动 1080×2400 emulator 截片库/详情/播放/旋转/DLNA/手势。运行网络受控样本与 5 分钟/30 分钟每类播放；代表性组合再跑 2 小时。失败页、空数据或方向错误的截图不算证据。

#### 门禁护栏（不可关闭）

`tools/qa/run_acceptance.py` 对下列规则做**代码级**强制，不接受命令行参数或矩阵文件把要求降级：

1. **Canonical 浏览器证据要求不可关闭**：全部必测 Web/后台行的 `screenshot_required` 必须为 true；涉及隔离 staging 写入的行（admin-06/08/09/10/11/13/14 与 admin-import-*）的 `write_readback_required` 必须为 true。写成 optional 或关掉要求会直接 fail fast，并指明具体行号。
2. **必测播放行不可缺省**：120 条必测播放行（20 样本 × 3 平台 × 5/30 分钟）不能标记为 optional；其 `evidence_ref` 必须指向产物根下真实存在、且与该行/版本/视口绑定的证据清单。
3. **单色截图不算截图**：纯色或只有 1–2 种颜色的 PNG 会被判为 blank 并拒绝作为「页面已加载」证据。判空基于 32×32 采样网格的颜色多样性，真实 UI 截图必然超过阈值。
4. **两小时播放拒绝未来时间窗**：代表性 120 分钟运行的 `measurement_started_at` / `measurement_ended_at` 必须都不晚于当前时间，且窗口确实达到 120 分钟。
5. **后台读回清单不能用普通截图冒充**：`write_readback_required` 的行必须提供结构化 JSON 读回清单，逐项记录 `written_value` 与 `read_back_value` 并绑定 staging 环境引用；截图只能作为附加证据。
6. **部署 PASS 必须绑定可验证清单**：`isolated_staging`、`vps_egress`、`database_backup_restore`、`cloudbase_runtime_requests`、`object_storage_contract` 五个门槛声明 PASS 时，必须提供可解析的 JSON 证据清单，且 `manifest_entries` 覆盖该门槛要求的全部条目（CloudBase 的「请求数为 0」同样需要抓包/日志/统计佐证）。

- [ ] **Step 4: 复测性能门槛并生成真实前后对比**

```python
comparison = compare_runs(
    baseline=load_run("baseline"),
    after=load_run("after"),
    keys=("device", "content_ref", "network_profile", "viewport"),
)
write_report(comparison, include_raw_sample_counts=True)
```

对相同设备、内容、来源、网络条件记录 baseline 与 after：metadata/API/Resolver P95、首页/片库/详情、点击至首帧 P50/P95、5xx、rebuffer、direct/relay、自动切源、30 分钟存活和严重卡顿。阈值严格按本文 Global Constraints 与配套 Android/Resolver 规格。样本不足、源 API 无法访问或生产域名尚缺时标 NOT-VERIFIED，不把模拟数据计入真实样本。

- [ ] **Step 5: 检查 CloudBase 零请求、最终 Diff 和用户证据**

搜索当前构建包、运行配置、VPS 日志与网络请求；仅测试夹具、一次性迁移工具和已标记退役的历史记录可出现 CloudBase 字样。当前支持版本运行时请求必须为 0。复核截图真实、内容已加载且文件路径可打开；检查文档无明文密钥；所有测试/指标/截图/出口证据全 PASS 才允许调用 selfhosted retirement 计划的生产退役和发布步骤。

## 总体验收范围

| 功能 | TV APK | 移动 APK | Web | 服务/后台 |
|---|---|---|---|---|
| 首页、片库、筛选、搜索、详情 | 是 | 是 | 是 | metadata/API 合同 |
| 收藏、历史、播放进度/续播 | 是 | 是 | 按现有能力逐项验证 | 用户数据不丢失 |
| 线路/集数、首线、缓存、重试 | 是 | 是 | 是 | Resolver 全状态合同 |
| 直连、受控 relay、自动换线 | 是 | 是 | 是 | direct/relay 比例 |
| Media3/VLC 错误分类与回退 | 是 | 是 | N/A | 错误状态可观测 |
| IPTV、电视源、公告、license、热更新 | 是 | 是 | 按当前能力逐项验证 | activation/update/object |
| 遥控焦点及返回恢复 | 是 | N/A | N/A | N/A |
| DLNA、旋转、手势 | N/A | 是 | N/A | N/A |
| 后台配置/发布/回滚/对象上传 | N/A | N/A | 管理入口 | 是 |

历史 CloudBase 迁移计划和 metadata center 计划的已完成条目只作证据，不重复执行；旧 APK 不属于矩阵。任何目标失败、未完成或未验证，整体结果都不是“所有版本所有功能通过”。
