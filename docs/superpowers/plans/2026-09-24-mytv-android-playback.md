# My TV TV/移动端播放与目录性能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已批准的 metadata SQLite 与 Source Resolver 接入所有当前可构建 Android 版本，减少打开详情和起播等待，并加强错误恢复、长播稳定性且保住本机用户数据。

**Architecture:** 将版本化 metadata SQLite 作为独立只读目录库，Room 继续保存收藏/历史/进度/last-good 引用；先在暂存副本校验 manifest、hash、schema 和 integrity，再原子替换。TV 与移动复用 data/repository/engine 代码，保留各自 UI、TV 焦点与移动 DLNA/手势差异。

**Tech Stack:** Kotlin、Compose、Room、Android SQLite、OkHttp、Media3、libVLC、Coroutines、Gradle、Android emulator。

**Spec:** docs/superpowers/specs/2026-09-24-mytv-android-playback-design.md

## Global Constraints

- 已发布旧 APK 不纳入本轮兼容；当前受支持版本不调用 CloudBase。
- metadata manifest 与 SQLite 必须通过版本、大小、SHA-256、schema 检查与 PRAGMA integrity_check 后才原子替换。
- 收藏、历史、播放进度、设置和 license 数据不能因目录更新、Room 迁移或损坏回退丢失。
- 最近成功线路只存 source_id/content_id/episode 与成功时间，不持久化签名播放 URL。
- 首帧 watchdog 以真实首帧事件判定；3–5 秒准备 backup、5–7 秒执行可恢复换线、15 秒才是最终硬超时。
- BUFFERING 依据缓冲增量/字节或分片进展和 20–30 秒换线冷却，不形成切线循环。
- 首页可操作 ≤ 1.0s、片库首屏 ≤ 1.2s、详情基础信息 ≤ 300ms、首帧 P50 ≤ 2.0s 且 P95 ≤ 4.0s。
- 30 分钟异常中断率 < 0.5%、重缓冲占比 < 1%、自动换线恢复目标 1–3s、UI 严重卡顿帧 < 1%。

## Review Focus

- Room 从 v1 升级时 destructive migration 清空用户数据：Task 2 保留数据库快照并逐表断言行数与值。
- metadata 文件下载损坏时覆盖现有可用库：Task 1 的 hash/schema/atomic-replace 失败注入覆盖。
- Resolver 的 mytv_id 与旧 douban:<id> 键错配收藏/历史：Task 1 明确保留原存储键并验证解析映射。
- READY 被误认为已首帧或 decoder 错误错误地切换来源：Task 4 用 Media3 listener fake 覆盖事件分类。
- TV 焦点或移动 DLNA/手势因共享代码同步丢失：Task 5 两类 instrumentation 和双工程同步检查覆盖。

## Dependencies

- 使用 VOD Resolver 计划提供的 GET /vod/resolve/{mytv_id}?episode=1 和已批准的状态/候选字段。
- 使用 metadata center 已完成的 schema 3 manifest、完整包与增量包；不得重做 metadata builder/VPS 容器/metadata 基础 API。
- 最终 Release origin 来自 selfhosted retirement 计划；正式域名缺失时只能跑 Debug/staging 验证。

---

### Task 1: 安装和查询本地 metadata SQLite

**Files:**
- Create: android/app/src/main/java/com/qiubo/optimaltv/data/catalog/MetadataCatalogStore.kt
- Create: android/app/src/test/java/com/qiubo/optimaltv/data/catalog/MetadataCatalogStoreTest.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/source/VodApiSource.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/model/Models.kt
- Mirror: internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/
- Mirror: any other existing Android project returned by python tools/sync_shared.py --list

**Interfaces:**
- Consumes: manifest URL、gzip/full SQLite 或 delta、当前可用 catalog_seed.json。
- Produces: suspend refreshIfNeeded(): CatalogRefreshResult、query/search/detail by mytv_id、legacy douban_id mapping、schema/data version.

- [ ] **Step 1: 写 metadata 包与目录查询失败测试**

```kotlin
val oldBytes = store.currentDatabaseBytes()
val result = store.install(invalidHashPackage)
assertIs<CatalogRefreshResult.Rejected>(result)
assertContentEquals(oldBytes, store.currentDatabaseBytes())
```

覆盖首次完整下载、同版本不重拉、更新下载中断、hash 不符、schema 版本不支持、integrity_check 失败、delta from/to 版本错、delta SQL 失败和并发刷新。失败时断言旧文件字节与当前 UI 目录均保持可读；测试房间数据 fixture 中至少有一条 favorite/history/progress 样本。

- [ ] **Step 2: 运行测试确认当前客户端没有 metadata SQLite Store**

Run: 在 TV 工程执行 .\gradlew.bat testDebugUnitTest --tests '*MetadataCatalogStoreTest'

Expected: FAIL，目标类/manifest 安装流程尚不存在。

- [ ] **Step 3: 实现独立 catalog 文件的原子安装和读取**

```kotlin
val staged = downloadToStaging(manifest)
require(sha256(staged) == manifest.sha256)
require(readSchemaVersion(staged) == 3 && integrityCheck(staged) == "ok")
atomicReplace(staged, currentCatalogFile)
```

通过 OkHttp 下载 metadata-center manifest 指定的 gzip 全量包或适用增量；先写到 catalog-staging.sqlite，校验 manifest size/SHA-256、data_version、schema=3 和 PRAGMA integrity_check，再 fsync 并原子替换当前文件。delta 只在旧库副本上以事务应用，from_version 不匹配即重新取全量包。目录更新放 IO/后台 dispatcher；不可用或未完成时继续显示 APK 内 catalog_seed.json。用 SQLite FTS 检测运行时能力；不可用时查询降级到 schema 定义的 LIKE/前缀检索，不破坏已下载库。

- [ ] **Step 4: 将 metadata 查询接入 VodRepository 且保留旧键**

从本地目录返回基础详情、首页/分类/筛选与搜索结果；新增 VodItem.mytvId 字段用于 Source Resolver。当前 favorite/history/progress 的既有 id 字段继续保存原值，不在本轮批量重键；通过 metadata external_id 显式联结 mytv_id 与 douban_id。所有 SQLite 读写和拼音/FTS 查询运行在后台 dispatcher。

- [ ] **Step 5: 运行目录测试并同步已有 Android 工程**

Run: .\gradlew.bat testDebugUnitTest --tests '*MetadataCatalogStoreTest'
Run: 在仓库根目录执行 python tools/sync_shared.py --source internal/mobile --target android
Run: 在仓库根目录执行 python tools/sync_shared.py

Expected: 单测 PASS；随后同步检查报告所有实际存在的工程共享路径一致。提交消息：feat(android): install and query local metadata catalog。

### Task 2: 为 last-good 引用增加非破坏性 Room 迁移

**Files:**
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/db/AppDb.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt
- Create: android/app/src/test/java/com/qiubo/optimaltv/data/db/AppDbMigrationTest.kt
- Mirror: other discovered Android projects' data/db and data/repo

**Interfaces:**
- Consumes: source_id、content_id、episode_index、successful_at。
- Produces: 按规范 mytv_id/episode 读取和更新 last-good source reference；不存签名 URL。

- [ ] **Step 1: 写 v1-to-v2 Room migration 测试**

```kotlin
val before = dao.exportUserRows()
database.openHelper.writableDatabase.version = 1
database.runMigrations(MIGRATION_1_2)
assertEquals(before, dao.exportUserRows())
assertTrue(database.lastGoodDao().all().isEmpty())
```

使用 Room in-memory/on-disk fixture 建立原 v1 favorite、history、progress 表各两行，升级后断言主键、id、时间、进度和值逐项不变；新增 last_good 表为空且可写。再断言 favorite/history/progress DAO 仍返回旧键。

- [ ] **Step 2: 运行测试确认 destructive migration 会清数据**

Run: .\gradlew.bat testDebugUnitTest --tests '*AppDbMigrationTest'

Expected: FAIL，当前 AppDb 使用 fallbackToDestructiveMigration 而没有从 v1 到新 schema 的显式迁移。

- [ ] **Step 3: 添加显式 Room schema migration**

```kotlin
val MIGRATION_1_2 = object : Migration(1, 2) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("""
            CREATE TABLE IF NOT EXISTS last_good_source (
                mytv_id INTEGER NOT NULL,
                episode_index INTEGER NOT NULL,
                source_id TEXT NOT NULL,
                content_id TEXT NOT NULL,
                successful_at INTEGER NOT NULL,
                PRIMARY KEY(mytv_id, episode_index)
            )
        """.trimIndent())
    }
}
```

增加 LastGoodSourceEntity/DAO 和 AppDb migration；移除会删除生产用户数据的 destructive fallback。存储 source_id、content_id、episode_index、successful_at 和规范影片引用；不存 URL、Cookie 或临时认证信息。对不认识的未来 schema 只进入可恢复错误界面，不自动删库。

- [ ] **Step 4: 运行 Room migration 与已存在 DAO 回归**

Run: .\gradlew.bat testDebugUnitTest --tests '*AppDbMigrationTest'

Expected: PASS；每个既有表的全部 fixture 行和字段保持不变。按共享清单同步到所有发现的版本工程并提交：fix(android): preserve local data in Room migration。

### Task 3: 串接 Resolver 状态、详情并行加载与快速起播

**Files:**
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/source/VodApiSource.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/data/repo/VodRepository.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/detail/DetailViewModel.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt
- Create: android/app/src/test/java/com/qiubo/optimaltv/data/repo/VodResolveFlowTest.kt
- Mirror: every matching shared file in discovered Android project directories

**Interfaces:**
- Consumes: ResolveResponse from Resolver API; local metadata detail; Room last-good reference.
- Produces: ResolveState flow with the six server states and immediately usable preferred/backups.

- [ ] **Step 1: 写详情不等待源和播放不等待所有源的测试**

```kotlin
val detail = repository.observeDetail(itemId).first()
assertTrue(detail.metadataReady)
assertFalse(detail.resolveCompleted)
assertEquals(expectedFirstLine, detail.preferred)
```

断言本地基础详情先呈现；线路请求后台启动；preferred 到达后播放器立即 prepare；backups 后到只更新候选、不重置当前播放位置；没有 preferred 时第一个可信候选到达即 prepare；超时/空 episodes 保持可重试状态而不是最终 UNAVAILABLE。

- [ ] **Step 2: 运行 resolver flow 测试确认旧 refresh 顺序失败**

Run: .\gradlew.bat testDebugUnitTest --tests '*VodResolveFlowTest'

Expected: 至少“基础详情先展示”和“首个可信线路先 prepare”失败。

- [ ] **Step 3: 通过 mytvId 使用稳定 Resolver 合同**

```kotlin
combine(localCatalog.detail(mytvId), vodApi.resolve(mytvId, episode)) { metadata, resolved ->
    DetailState(metadata = metadata, resolveState = resolved)
}.flowOn(Dispatchers.IO)
```

VodApiSource 增加 resolve(mytvId, episode)，解析 preferred/backups/cache/version/status。将 RESOLVING 映射为初始匹配提示、FAST_PATH_FAILED 映射为快路径失败提示、FALLBACK_SEARCHING 映射为补查进度、STALE_AVAILABLE 显示仍可播放的旧线路、TEMPORARILY_UNAVAILABLE 显示可重试提示、MATCHED 显示已就绪。DetailViewModel 从本地 metadata 展示详情，同时异步解析；移除打开详情必定 refresh 所有源的等待。last-good 命中先播放该 source/content/episode 引用，同时后台刷新。超时、短暂空列表和过期旧线路均保留独立状态，不映射为最终 UNAVAILABLE。episode 的 UI 选择以 episode_index 明确传入。

- [ ] **Step 4: 运行 repository、详情和播放状态单测并同步**

Run: .\gradlew.bat testDebugUnitTest --tests '*VodResolveFlowTest' --tests '*EpisodeRefreshPolicyTest' --tests '*VodRefreshRoutesTest'
Run: python tools/sync_shared.py

Expected: PASS；所有发现工程共享数据层哈希一致。提交消息：feat(android): start playback from first resolved line。

### Task 4: 用真实首帧与错误分类修复播放恢复

**Files:**
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/player/PlaybackPolicy.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/playback/PlaybackRecoveryPolicy.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/playback/Engines.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/player/PlaybackPolicy.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/playback/PlaybackRecoveryPolicy.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/playback/Engines.kt
- Create: android/app/src/main/java/com/qiubo/optimaltv/data/analytics/PlaybackTelemetry.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/App.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/OtvLog.kt
- Create: android/app/src/test/java/com/qiubo/optimaltv/ui/player/FirstFrameRecoveryPolicyTest.kt
- Create: android/app/src/test/java/com/qiubo/optimaltv/ui/player/LongPlaybackHealthPolicyTest.kt
- Create: android/app/src/test/java/com/qiubo/optimaltv/data/analytics/PlaybackTelemetryContractTest.kt

**Interfaces:**
- Consumes: Media3 rendered-first-frame, playback state/error category, buffered-duration/bytes/segment progress and candidate stream.
- Produces: staged backup preparation, line switch, optional engine switch, cooled-down recovery action.

- [ ] **Step 1: 写首帧、缓冲进展、错误类别和冷却策略测试**

```kotlin
assertEquals(RecoveryAction.PREPARE_BACKUP, policy.tick(elapsedMs = 4_000, firstFrame = false))
assertEquals(RecoveryAction.SWITCH_SOURCE, policy.tick(elapsedMs = 6_000, networkFailure = true))
assertEquals(RecoveryAction.RETRYABLE_TIMEOUT, policy.tick(elapsedMs = 15_000))
```

覆盖 READY 但尚无 rendered frame、首帧在 3/5 秒边界到达、5–7 秒网络错误换线、15 秒最终硬超时、decoder/音轨错误优先切 libVLC、HTTP/manifest 错误优先换线路、无 buffer/byte/segment 增长、progress 持续增长、20 秒/30 秒换线冷却及防重复换线。再为 app_start、home_first_content、detail_open、resolve_start、resolve_first_source、play_click、player_prepare、first_frame、buffer_start/end、auto_switch、engine_fallback、play_5min、play_30min 和 fatal 的匿名化字段写单测，断言日志中没有完整播放 URL、Cookie、授权头、卡密或稳定设备标识。

- [ ] **Step 2: 运行策略测试确认 READY watchdog 有缺口**

Run: .\gradlew.bat testDebugUnitTest --tests '*FirstFrameRecoveryPolicyTest' --tests '*LongPlaybackHealthPolicyTest'

Expected: FAIL，现有策略不能区分 READY 与真实首帧或不能组合播放进展判定。

- [ ] **Step 3: 实现阶段恢复与并行监控**

```kotlin
when (policy.decide(snapshot)) {
    RecoveryAction.PREPARE_BACKUP -> prepareNextCandidate()
    RecoveryAction.SWITCH_SOURCE -> switchCandidatePreservingPosition()
    RecoveryAction.SWITCH_ENGINE -> switchToVlc()
    RecoveryAction.RETRYABLE_TIMEOUT -> showRetry()
    RecoveryAction.NONE -> Unit
}
```

在 Media3 renderer 的真实首帧回调后停止 watchdog。3–5 秒准备 backup；5–7 秒只对可恢复网络类错误换线；15 秒硬超时显示明确重试。decoder、容器、音轨错误按当前不支持音轨规则优先切换 libVLC。每 5–10 秒采集 position、buffered duration、playing/state、网络字节和 manifest/segment 最近进展；以信号组合确认卡死。metadata 刷新、测速与日志使用独立调度资源，不拿全局播放器锁，不在持续前进时切线。PlaybackTelemetry 按已批准事件名发匿名化生命周期与播放事件；仅保留短期随机关联 ID、规范内容匿名桶、source_id、状态类别和耗时。

- [ ] **Step 4: 运行播放策略及两工程 Release 单测**

Run: 在 TV 与每个发现的移动/公测工程分别执行 .\gradlew.bat testReleaseUnitTest

Expected: PASS；移动端 DLNA 控制器和播放器手势状态没有被删除或绕过。提交消息：fix(android): recover playback from measured first frame。

### Task 5: 稳定列表/焦点并执行所有实际 Android 变体的设备验收

**Files:**
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/home/HomeScreen.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/all/AllScreen.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/detail/DetailScreen.kt
- Modify: android/app/src/main/java/com/qiubo/optimaltv/ui/search/SearchScreen.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/home/HomeScreen.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/all/AllScreen.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/detail/DetailScreen.kt
- Modify: internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/search/SearchScreen.kt
- Create: android/app/src/androidTest/java/com/qiubo/optimaltv/ui/AndroidPlaybackAcceptanceTest.kt
- Create: internal/mobile/app/src/androidTest/java/com/qiubo/optimaltv/mobileui/MobilePlaybackAcceptanceTest.kt
- Create: docs/04-records/2026-09-24-Android播放与目录验收.md

**Interfaces:**
- Consumes: 所有前述目录、播放和恢复改动。
- Produces: 每种存在的 Android 版本的 unit/instrumentation/release build、长播报告和实际目标屏幕截图。

- [ ] **Step 1: 固定 UI 回归自动化**

```kotlin
LazyColumn {
    items(items = movies, key = { movie -> movie.id }) { movie ->
        MovieCard(movie = movie)
    }
}
```

增加稳定业务 key 检查；只修复仍使用 index key 的动态片库/推荐列表。异步图片和板块更新不替换 section 容器 key。保持 Coil 内存/磁盘缓存及现有封面预取，限制可见窗口与并发；图片到达不改变卡片尺寸。测试 TV D-pad 连续 100 次、异步内容到达和 200 卡片滚动；移动端快滑、旋转、反复进出详情、seek、音量/亮度手势、DLNA 播放/断开/续播。

- [ ] **Step 2: 在每个存在的 Android 工程运行 JVM 和设备测试**

先运行 python tools/sync_shared.py --list 取得实际工程目录。每个工程从其目录运行：

```powershell
.\gradlew.bat testReleaseUnitTest
.\gradlew.bat connectedAndroidTest
.\gradlew.bat assembleRelease
```

测试目标至少包含 TV 横屏 1920×1080 与移动竖屏 1080×2400 emulator；先由 adb devices 确认设备在线。任一现存工程无 APK、测试失败或安装失败均为未通过。

- [ ] **Step 3: 跑媒体/网络矩阵并保存截图与性能原始值**

真实媒体至少包含 10 部电影、10 部剧集、5 个 1080p、5 个低清旧源、5 个 HLS、5 个直链。网络覆盖稳定 Wi-Fi、高延迟、10Mbps/5Mbps 限速、1% 丢包、3s/10s 短断网。每类至少做 5 分钟与 30 分钟；2 小时长播用代表性组合并注明不适用条件。记录样本/设备/版本/网络/首帧/缓冲/自动换线/存活/错误日志。截图须显示已加载的真实首页、片库、详情和播放画面；移动另截旋转/DLNA/手势状态，TV 截焦点/退出恢复状态。

- [ ] **Step 4: 对比指标、复核共享文件和最终 Diff**

使用同一设备/内容/网络与基线比较首页、片库、详情、首帧 P50/P95、30 分钟生存、重缓冲、换线和严重卡顿。样本不足或没有真实媒体/网络条件的一项明确记为未验证，不能推断通过。截图及脱敏日志放 artifacts/acceptance/2026-09-24/android/，报告写入文档路径。运行 python tools/sync_shared.py，提交消息：test(android): record cross-version playback acceptance。
