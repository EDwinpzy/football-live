<a id="中文"></a>
[中文](#中文) | [English](#english)

# 做直播 / Football Live

## 中文说明

### 项目定位

“做直播”（工程品牌 `ZuoLive`，仓库名 `football-live`）是一款面向足球赛事直播的 Android 应用，只提供 Android TV 与 Android 手机/平板两个客户端。用户可查看足球赛程与比赛状态，选择单场比赛的直播信号源，并进入播放器观看直播。

### 核心能力

- 足球赛程与比赛状态展示。
- 单场比赛的直播信号源选择。
- HLS 直播播放。
- 播放故障恢复：换线、重试、首帧恢复和播放健康检查。
- 本机中继：客户端启动内嵌 Python 后端，通过本机 HTTP 服务辅助赛程、队徽和直播流/分片请求。
- 应用内更新：检查版本清单、校验更新包并进入安装流程。
- TV 遥控焦点导航与手机/平板触摸导航。

### 产品边界

- 仅交付 Android TV 与 Android 手机/平板客户端。
- Windows、iOS 和独立网页客户端均已移除，不再提供构建入口或产品验收面。
- 服务端保留的 `www/`、`/web/` 仅用于现有客户端的更新清单、健康检查、静态资源兼容和升级链路，不属于第三个客户端。
- 已移除的影视、点播、IPTV、会员和后台业务不应重新开放。

### 工程结构

| 路径 | 用途 |
|---|---|
| `android/` | Android TV 客户端、内嵌后端与资源 |
| `internal/mobile/` | Android 手机/平板客户端 |
| `tools/backend-src/` | 客户端内嵌 Python 后端、足球数据源、播放中继及契约测试 |
| `tools/` | 后端打包、APK 更新发布、验收和运维检查脚本 |
| `deploy/selfhosted/` | 自托管更新服务与部署配置 |
| `docs/项目文档.md` | 唯一的详细项目文档 |

### 环境要求

- JDK 17 或更高版本。
- Android SDK：当前工程使用 `compileSdk 35`、`minSdk 30`、`targetSdk 34`。
- 两个 Android 工程均自带 Gradle Wrapper 8.14.3，无需单独安装 Gradle。
- Python 3.11 或 3.12。
- 可选：使用 Python 虚拟环境安装工具依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r tools\requirements.txt
```

macOS/Linux 下将 `.\gradlew.bat` 替换为 `./gradlew`，并使用对应的 Python 虚拟环境激活命令。

### 开发与测试

以下命令均从仓库根目录开始执行。

后端测试：

```powershell
python -m unittest discover -s tools/backend-src/tests -p "test_*.py"
```

工具、部署与文档契约测试：

```powershell
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

修改 `tools/backend-src/` 后，如需刷新两端共用的内嵌后端包：

```powershell
python tools/build_backend_zip.py
```

### 文档边界

- `README.md` 是项目入口，只承载产品定位、能力边界、环境要求、常用命令和文档导航。
- `docs/项目文档.md` 是唯一详细文档，承载架构、实现约束、验收、发布、运维和排障说明。
- 不新增 `README.en.md` 或其他项目说明文档。本机调试记录仅放在不入库的 `.learnings/`。

### 发布边界

发布必须由用户在当前请求中明确授权。未获得当前授权时，不执行 Release 构建、部署、应用内更新包上传、提交或推送。具体发布顺序和检查项以 `docs/项目文档.md` 及仓库根目录 `AGENTS.md` 为准。

### 真实直播验收

真实直播结论必须来自使用当前源码构建的客户端和真实上游直播源，记录实际播放、信号源、时间、进程状态、崩溃日志、缓冲与恢复事件。模拟器交互检查、单元测试、测试视频、旧 APK 或首页截图都不能替代真实直播验收；没有完成证据时，应明确标记为“未完成”，不能宣称通过。

---

<a id="english"></a>

## English

### Overview

**Football Live** (engineering brand `ZuoLive`, repository `football-live`) is an Android app for football match streaming. It ships only two clients: Android TV and Android phones/tablets. Users can browse football fixtures and match status, choose an available live source for a match, and open the player to watch the stream.

### Capabilities

- Football fixtures and match-status display.
- Live-source selection for individual matches.
- HLS live playback.
- Playback recovery, including source switching, retries, first-frame recovery, and playback health checks.
- Local relay: each client starts an embedded Python backend that serves local HTTP requests for fixtures, team artwork, and live stream/segment relay support.
- In-app updates: version-manifest checks, update-package validation, and the installation flow.
- Remote-control focus navigation on TV and touch navigation on phones/tablets.

### Product scope

- Only the Android TV and Android phone/tablet clients are delivered.
- The Windows, iOS, and standalone web clients have been removed and no longer have build entry points or product acceptance surfaces.
- The server-side `www/` and `/web/` paths are retained only for the existing clients' update manifests, health checks, static-resource compatibility, and upgrade path. They are not a third client.
- Removed movie-on-demand, IPTV, membership, and admin business surfaces must not be reopened.

### Repository layout

| Path | Purpose |
|---|---|
| `android/` | Android TV client, embedded backend, and resources |
| `internal/mobile/` | Android phone/tablet client |
| `tools/backend-src/` | Embedded Python backend, football data sources, streaming relay, and contract tests |
| `tools/` | Backend packaging, APK update publishing, acceptance, and operations checks |
| `deploy/selfhosted/` | Self-hosted update service and deployment configuration |
| `docs/项目文档.md` | The single detailed project document |

### Requirements

- JDK 17 or newer.
- Android SDK: the current projects use `compileSdk 35`, `minSdk 30`, and `targetSdk 34`.
- Both Android projects include Gradle Wrapper 8.14.3; a separate Gradle installation is not required.
- Python 3.11 or 3.12.
- Optional: install tooling dependencies in a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r tools\requirements.txt
```

On macOS/Linux, use `./gradlew` instead of `.\gradlew.bat` and the corresponding virtual-environment activation command.

### Development and tests

Run the following commands from the repository root.

Backend tests:

```powershell
python -m unittest discover -s tools/backend-src/tests -p "test_*.py"
```

Tooling, deployment, and documentation contract tests:

```powershell
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

After changing `tools/backend-src/`, refresh the embedded backend archive shared by both clients when needed:

```powershell
python tools/build_backend_zip.py
```

### Documentation boundary

- `README.md` is the project entry point and covers only product positioning, scope, requirements, common commands, and documentation navigation.
- `docs/项目文档.md` is the single detailed document for architecture, implementation constraints, acceptance, release, operations, and troubleshooting.
- Do not add `README.en.md` or other project explanation documents. Local debugging notes belong only in the uncommitted `.learnings/` directory.

### Release boundary

A release requires explicit authorization in the user's current request. Without that authorization, do not run release builds, deploy, upload in-app update packages, commit, or push. Follow `docs/项目文档.md` and the repository-level `AGENTS.md` for the release order and required checks.

### Live acceptance

A real-live conclusion must be based on a client built from the current source and a real upstream live source. Record the actual playback, source, time, process state, crash logs, buffering, and recovery events. Emulator interaction checks, unit tests, test videos, old APKs, and home-screen screenshots cannot replace real live acceptance. If the evidence is incomplete, mark the result as incomplete rather than claiming a pass.
