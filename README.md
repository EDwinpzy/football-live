# 球迹直播 · QiuJi Live

> 面向 Android TV 与 Android 手机/平板的足球直播客户端，内置 Python 本机后端，支持 HLS 直播播放、线路切换、播放恢复与应用内更新校验。

**品牌：** 球迹直播（英文：QiuJi Live）
**工程仓库：** `football-live`
**客户端：** Android TV、Android 手机/平板

[简体中文](#zh-cn) | [English](#en)

| 项目状态 | 说明 |
|---|---|
| 交付客户端 | Android TV、Android 手机/平板 |
| 播放能力 | HLS 播放、本机中继、线路切换、首帧恢复、播放健康检查 |
| 后端形态 | Python 后端随客户端 APK 内嵌运行 |
| 更新链路 | 版本清单检查、APK 分片与整包 SHA-256 校验、签名证书校验、系统安装 |
| 产品边界 | 不提供 Windows、iOS、独立网页或影视点播客户端 |
| 发布策略 | 仅在用户当前请求中明确授权后执行 |

---

<a id="zh-cn"></a>

## 简体中文

### 项目简介

**球迹直播**（英文品牌 **QiuJi Live**）是一个只交付 Android TV 与 Android 手机/平板两个客户端的足球直播应用。用户可以查看足球赛程和比赛状态，选择单场比赛的直播信号源，并在播放器中观看 HLS 直播。

产品显示名已经统一为“球迹直播”。工程仓库名仍为 **`football-live`**；在 APK 文件名等不适合空格的场景中统一使用 **`QiuJiLive`**。

### 功能

- 查看足球赛程、比赛状态和球队信息。
- 为单场比赛选择可用直播信号源。
- 播放 HLS 直播，并支持 TS 分片经本机后端中继。
- 处理换线、重试、首帧恢复和长播放健康检查。
- TV 端支持遥控器方向键、OK 和返回键焦点导航。
- 手机/平板端支持触摸导航，并覆盖竖屏与横屏页面。
- 客户端启动内嵌 Python 后端，通过本机 HTTP 服务处理赛程、队徽、直播流和分片请求。
- 应用内检查更新清单，校验 APK 分片、整包 SHA-256 和签名证书，再进入 Android 系统安装流程。
- 自托管服务提供健康检查、更新清单、对象存储和静态资源兼容入口。

### 客户端

| 客户端 | 工程目录 | applicationId | 主要交互 |
|---|---|---|---|
| Android TV | `android/` | `com.qiubo.optimaltv.public` | 遥控焦点导航、播放器与电视大屏布局 |
| Android 手机/平板 | `internal/mobile/` | `com.qiubo.optimaltv.mobile.public` | 触摸导航、竖屏与横屏布局 |

两端使用相同的显示名“球迹直播”，但 applicationId 不同。为避免在同一设备桌面和启动器中混淆两个客户端，请按目标设备安装对应版本。

### 系统架构

```text
Android TV / Android 手机·平板
              │
              │ 本机 HTTP
              ▼
    内嵌 Python 后端
      ├─ 足球赛程与比赛数据
      ├─ 队徽与兼容静态资源
      └─ HLS / TS 分片本机中继
              │
              ▼
        上游真实直播源

自托管服务 ── 健康检查、更新清单、对象存储与升级兼容入口
```

核心约束：

- 原生客户端负责界面、遥控或触摸交互、播放器状态和系统安装流程。
- Python 后端随 APK 内嵌，不要求用户单独安装或启动服务。
- HLS 播放链路同时包含 Media3 与故障恢复策略；真实直播验收不能只验证首页或静态视频。
- 更新包必须通过 HTTPS 获取，并完成分片/整包摘要和签名证书校验。

### 目录结构

| 路径 | 用途 |
|---|---|
| `android/` | Android TV 客户端、内嵌 Python 后端与资源 |
| `internal/mobile/` | Android 手机/平板客户端 |
| `tools/backend-src/` | 内嵌 Python 后端、足球数据源、HLS 中继及契约测试 |
| `tools/qa/` | 真实直播采集、延迟对照和验收评估工具 |
| `tools/` | 后端打包、APK 更新发布、资源和运维检查脚本 |
| `deploy/selfhosted/` | 自托管健康检查、更新服务与部署配置 |
| `apk/` | 本地构建归档目录；生成物不应替代源码验证 |
| `docs/项目文档.md` | 唯一详细项目文档 |
| `.learnings/` | 本机调试经验，不纳入 Git |
| `artifacts/` | 本机截图、日志和验收证据，不纳入 Git |

### 产品范围

- 只交付 Android TV 与 Android 手机/平板客户端。
- Windows、iOS 和独立网页客户端已移除，不再提供构建入口或产品验收面。
- 服务端保留的 `www/`、`/web/` 仅服务于现有客户端的更新清单、健康检查、静态资源兼容和升级链路，不是第三个客户端。
- 影视、点播、IPTV、会员和后台管理产品面已移除，不应重新开放。
- 仓库整理和发布不得重新引入已下线的客户端、业务模块或旧影视运行时。

### 快速开始

环境要求：

- JDK 17 或更高版本。
- Android SDK：`compileSdk 35`、`minSdk 30`、`targetSdk 34`。
- 两个 Android 工程均自带 Gradle Wrapper `8.14.3`，不需要单独安装 Gradle。
- Python `3.11` 或 `3.12`。
- Windows PowerShell 为本文示例环境；macOS/Linux 将 `.\gradlew.bat` 替换为 `./gradlew`，并使用对应的虚拟环境激活命令。

从仓库根目录准备 Python 工具环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r tools\requirements.txt
```

修改 `tools/backend-src/` 后，重新生成两端共用的内嵌后端包：

```powershell
python tools/build_backend_zip.py
```

分别构建两个客户端的调试包：

```powershell
Push-Location .\android
.\gradlew.bat assembleDebug
Pop-Location

Push-Location .\internal\mobile
.\gradlew.bat assembleDebug
Pop-Location
```

### 构建与测试

从仓库根目录执行后端、工具和契约测试：

```powershell
python -m unittest discover -s tools/backend-src/tests -p "test_*.py"
python -m unittest discover -s tools/tests -p "test_*.py"
```

Android TV 单元测试与调试构建：

```powershell
Push-Location .\android
.\gradlew.bat testDebugUnitTest
.\gradlew.bat assembleDebug
Pop-Location
```

Android 手机/平板单元测试与调试构建：

```powershell
Push-Location .\internal\mobile
.\gradlew.bat testDebugUnitTest
.\gradlew.bat assembleDebug
Pop-Location
```

Release 构建有正式入口门槛：必须传入已验收的 HTTPS API origin。

```powershell
Push-Location .\android
.\gradlew.bat assembleRelease -PapiBaseUrl=https://<已验收的正式域名>
Pop-Location

Push-Location .\internal\mobile
.\gradlew.bat assembleRelease -PapiBaseUrl=https://<已验收的正式域名>
Pop-Location
```

也可以通过环境变量 `OTV_RELEASE_API_BASE_URL` 提供同一入口。自建 VPS 的 IP 迁移入口不是默认授权路径；只有明确接受该入口时，才按构建门槛同时传入配套的 `-PallowIpReleaseOrigin=true`。不得用裸 `assembleRelease` 绕过或省略入口校验。

修改后端后，必须在构建 APK 前运行 `python tools/build_backend_zip.py`，确保两端 APK 内嵌的 `python-backend.zip` 与源码一致。

### 发布

发布、部署、应用内更新包上传、提交和推送都必须由用户在当前请求中明确授权。未获得当前授权时，只做本地测试和构建检查。

获得授权后，按以下顺序串行执行：

1. 检查共享工作区和其他活动任务，确认未混入他人未提交改动。
2. 运行后端、工具、TV 和移动版测试，并完成两端 Release 编译。
3. 使用 `python tools/release_all.py --deploy` 构建并部署自托管服务。该步骤会重建后端包、执行数据库迁移、更新 Compose、检查并重载 nginx。
4. 部署完成后再重建 TV 和移动版 APK，确保 APK 内嵌后端与部署产物一致。
5. 核对实际 APK、内嵌 `python-backend.zip`、公开更新清单和对象存储回读摘要。
6. 如需发布应用内更新，使用现有发布脚本：

```powershell
python tools/publish_apk_update.py `
  --channel tv `
  --apk "apk\TV版\QiuJiLive-TV.apk" `
  --version-code <version-code> `
  --version-name <version-name> `
  --cert <signing-certificate-sha256> `
  --note "<update-note>"
```

移动版将 `--channel tv` 改为 `--channel mobile`，并传入 `apk\移动版\QiuJiLive-Mobile.apk`。发布前必须确认 `versionCode` 严格递增、APK 签名证书匹配、分片和整包 SHA-256 可回读验证。

详细发布门槛、共享工作区规则和回滚检查见 [docs/项目文档.md](docs/项目文档.md) 与根目录 [AGENTS.md](AGENTS.md)。不要自行触发生产发布或另行分配版本号。

### 真实直播验收边界

真实直播结论必须基于**当前源码重新构建并安装的客户端**和**真实上游直播源**。首页截图、单元测试、调试构建、模拟器页面交互、测试视频或旧 APK 都不能替代真实直播验收。

当前验收口径：

- 至少覆盖 3 场不同比赛，不得用同一场比赛多开代替。
- TV 与手机/平板两个客户端都要覆盖，并使用实际可用线路。
- 每场连续播放至少 10 分钟；若比赛结束，应切换到另一场真实比赛继续累计。
- 播放期间不得出现进程退出、FATAL/AndroidRuntime 崩溃、持续黑屏或冻结、ANR，以及用户可见的反复缓冲。
- 相对同一上游原始直播信号，客户端额外延迟不超过 5 秒，且不得随播放时间累积。
- 记录比赛 ID、线路 ID、开始/结束时间、周期截图、进程存活、`logcat -b crash`、播放健康日志、缓冲和恢复事件。

无法取得真实可播比赛、上游身份或完整日志证据时，应明确标记为“未完成”，不能把工具自测、静态页面或历史截图写成验收通过。

### 文档

- [README.md](README.md)：项目入口，包含定位、能力、客户端、架构、常用命令、发布和验收边界。
- [docs/项目文档.md](docs/项目文档.md)：唯一详细文档，包含架构、实现约束、发布顺序、运维、排障和历史验收记录。
- `.learnings/`：本机调试经验，不纳入版本控制。
- `artifacts/`：本机截图、日志和验收证据，不纳入版本控制。

不新增 `README.en.md` 或其他项目说明文档；英文内容与中文内容都维护在本文件的不同章节中。

### License

当前仓库没有 `LICENSE` 文件，因此未声明任何开源许可证，也不对复制、分发、商用或再许可作默认授权。使用前请向仓库所有者确认授权范围。

---

<a id="en"></a>

## English

### Overview

**QiuJi Live** (Chinese brand: **球迹直播**) is a football live-streaming application with only two delivered clients: Android TV and Android phones/tablets. Users can browse football fixtures and match status, choose an available live source for a match, and watch HLS streams in the player.

The product display name is unified as “球迹直播”. The engineering repository remains **`football-live`**. **`QiuJiLive`** is used where spaces are unsuitable, such as APK filenames.

### Features

- Browse football fixtures, match status, and team information.
- Select an available live source for an individual match.
- Play HLS live streams, including TS-segment relay through the local backend.
- Handle source switching, retries, first-frame recovery, and long-playback health checks.
- Support D-pad, OK, and Back focus navigation on Android TV.
- Support touch navigation in portrait and landscape layouts on phones/tablets.
- Start an embedded Python backend that serves fixtures, team artwork, live streams, and segments over local HTTP.
- Check update manifests and validate APK segments, whole-package SHA-256, and signing certificates before invoking the Android installer.
- Use a self-hosted service for health checks, update manifests, object storage, and static-resource compatibility.

### Clients

| Client | Project directory | applicationId | Primary interaction |
|---|---|---|---|
| Android TV | `android/` | `com.qiubo.optimaltv.public` | Remote-control focus navigation and a TV-sized layout |
| Android phone/tablet | `internal/mobile/` | `com.qiubo.optimaltv.mobile.public` | Touch navigation with portrait and landscape layouts |

Both clients display the same “球迹直播” label but use different applicationIds. Install the client appropriate for each target device to avoid confusion between the two apps in a launcher.

### Architecture

```text
Android TV / Android phone·tablet
              │
              │ local HTTP
              ▼
      Embedded Python backend
      ├─ football fixtures and match data
      ├─ team artwork and compatibility assets
      └─ HLS / TS segment local relay
              │
              ▼
          Real upstream live source

Self-hosted service ── health checks, update manifests, object storage, and upgrade compatibility
```

Key constraints:

- Native clients own the UI, remote/touch interaction, player state, and Android installation flow.
- The Python backend is embedded in each APK; users do not need to install or start it separately.
- The HLS path includes Media3 playback and recovery policies. Home-screen checks or static test videos do not prove live-stream acceptance.
- Update packages must be fetched over HTTPS and must pass segment/package digest and signing-certificate validation.

### Repository layout

| Path | Purpose |
|---|---|
| `android/` | Android TV client, embedded Python backend, and resources |
| `internal/mobile/` | Android phone/tablet client |
| `tools/backend-src/` | Embedded Python backend, football data sources, HLS relay, and contract tests |
| `tools/qa/` | Real-live capture, delay comparison, and acceptance evaluation tools |
| `tools/` | Backend packaging, APK update publishing, asset generation, and operations checks |
| `deploy/selfhosted/` | Self-hosted health-check, update, and deployment configuration |
| `apk/` | Local build archive; generated files do not replace source validation |
| `docs/项目文档.md` | The single detailed project document |
| `.learnings/` | Local debugging notes, not tracked by Git |
| `artifacts/` | Local screenshots, logs, and acceptance evidence, not tracked by Git |

### Product scope

- Only the Android TV and Android phone/tablet clients are delivered.
- The Windows, iOS, and standalone web clients have been removed and no longer have build entry points or product acceptance surfaces.
- The retained `www/` and `/web/` paths serve only existing clients' update manifests, health checks, static-resource compatibility, and upgrade path. They are not a third client.
- Movie-on-demand, VOD, IPTV, membership, and admin product surfaces have been removed and must not be reopened.
- Repository cleanup and releases must not reintroduce retired clients, business modules, or the old film runtime.

### Quick start

Requirements:

- JDK 17 or newer.
- Android SDK: `compileSdk 35`, `minSdk 30`, and `targetSdk 34`.
- Both Android projects include Gradle Wrapper `8.14.3`; a separate Gradle installation is not required.
- Python `3.11` or `3.12`.
- Commands below use Windows PowerShell. On macOS/Linux, replace `.\gradlew.bat` with `./gradlew` and use the matching virtual-environment activation command.

From the repository root, prepare the Python tooling environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r tools\requirements.txt
```

After changing `tools/backend-src/`, rebuild the embedded backend archive shared by both clients:

```powershell
python tools/build_backend_zip.py
```

Build both debug clients separately:

```powershell
Push-Location .\android
.\gradlew.bat assembleDebug
Pop-Location

Push-Location .\internal\mobile
.\gradlew.bat assembleDebug
Pop-Location
```

### Build and test

Run backend, tooling, and contract tests from the repository root:

```powershell
python -m unittest discover -s tools/backend-src/tests -p "test_*.py"
python -m unittest discover -s tools/tests -p "test_*.py"
```

Android TV unit tests and debug build:

```powershell
Push-Location .\android
.\gradlew.bat testDebugUnitTest
.\gradlew.bat assembleDebug
Pop-Location
```

Android phone/tablet unit tests and debug build:

```powershell
Push-Location .\internal\mobile
.\gradlew.bat testDebugUnitTest
.\gradlew.bat assembleDebug
Pop-Location
```

Release builds require an explicitly approved HTTPS API origin:

```powershell
Push-Location .\android
.\gradlew.bat assembleRelease -PapiBaseUrl=https://<approved-production-origin>
Pop-Location

Push-Location .\internal\mobile
.\gradlew.bat assembleRelease -PapiBaseUrl=https://<approved-production-origin>
Pop-Location
```

The same origin can be supplied through `OTV_RELEASE_API_BASE_URL`. The self-hosted VPS IP migration origin is not the default release path; only when that migration entry point is explicitly accepted should the matching `-PallowIpReleaseOrigin=true` option be supplied. Do not bypass or omit the origin gate with a bare `assembleRelease`.

After backend changes, run `python tools/build_backend_zip.py` before building APKs so the embedded `python-backend.zip` in both clients matches the source.

### Release

A release, deployment, in-app update upload, commit, or push requires explicit authorization in the user's current request. Without that authorization, limit work to local tests and build checks.

When authorized, execute the following serially:

1. Check the shared worktree and other active tasks; do not include unrelated uncommitted changes.
2. Run backend, tooling, TV, and mobile tests, and complete both Release compilations.
3. Run `python tools/release_all.py --deploy` to build and deploy the self-hosted service. This rebuilds the backend package, applies database migrations, updates Compose, validates/recreates nginx, and reloads the proxy.
4. Rebuild both APKs after deployment so the embedded backend matches the deployed artifacts.
5. Verify the APKs, embedded `python-backend.zip`, public update manifests, and object-storage read-back digests.
6. To publish an in-app update, use the existing publisher:

```powershell
python tools/publish_apk_update.py `
  --channel tv `
  --apk "apk\TV版\QiuJiLive-TV.apk" `
  --version-code <version-code> `
  --version-name <version-name> `
  --cert <signing-certificate-sha256> `
  --note "<update-note>"
```

For mobile, use `--channel mobile` and `apk\移动版\QiuJiLive-Mobile.apk`. Before publishing, confirm a strictly increasing `versionCode`, a matching signing certificate, and read-back-verifiable segment and whole-package SHA-256 values.

See [docs/项目文档.md](docs/项目文档.md) and [AGENTS.md](AGENTS.md) for detailed release gates, shared-worktree rules, and rollback checks. Do not start a production release or allocate a version independently.

### Real-live acceptance boundary

A real-live conclusion must use a client rebuilt from the current source and installed on the target, plus a real upstream live source. Home-screen screenshots, unit tests, debug builds, emulator page interaction, test videos, and old APKs do not replace real-live acceptance.

Current acceptance criteria:

- Cover at least three different matches; multiple instances of one match do not count.
- Cover both Android TV and phone/tablet clients using actual available sources.
- Observe each match continuously for at least 10 minutes. If a match ends, switch to another real match to continue accumulating evidence.
- During playback, there must be no process exit, FATAL/AndroidRuntime crash, persistent black screen or freeze, ANR, or visible repeated rebuffering.
- Relative to the same upstream live signal, additional client delay must not exceed 5 seconds and must not accumulate over time.
- Record match IDs, source IDs, start/end times, periodic screenshots, process liveness, `logcat -b crash`, playback-health logs, buffering events, and recovery events.

If real playable matches, upstream identity, or complete logs are unavailable, mark the result as **incomplete**. Do not describe tool self-tests, static pages, or historical screenshots as a passed live acceptance.

### Documentation

- [README.md](README.md): project entry point covering positioning, capabilities, clients, architecture, common commands, release, and acceptance boundaries.
- [docs/项目文档.md](docs/项目文档.md): the single detailed document for architecture, implementation constraints, release order, operations, troubleshooting, and historical acceptance records.
- `.learnings/`: local debugging notes, not tracked by Git.
- `artifacts/`: local screenshots, logs, and acceptance evidence, not tracked by Git.

Do not add `README.en.md` or another project explanation document. Both Chinese and English content are maintained in separate sections of this file.

### License

This repository has no `LICENSE` file, so no open-source license is declared and no default permission to copy, redistribute, commercialize, or sublicense is granted. Confirm the authorization scope with the repository owner before use.
