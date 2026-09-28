# My TV（OptimalTV 工程）

一款面向电视大屏、手机和平板的原生影视应用，外加一个功能对齐的网页版。
Kotlin + Jetpack Compose + Media3 构建，tvOS 深色风格；Python 后端内嵌进 app（Chaquopy），
设备端自给自足。商业化 = 卡密付费。

## 文档只有两份

| 文档 | 内容 |
|---|---|
| 本 README | 项目介绍、能力清单、技术栈、快速开始 |
| **[项目文档](docs/项目文档.md)** | 唯一详细文档：架构 · 部署 · 后台 · 构建发布 · 测试 · 运维排障 |

## 产品展示

### TV 大屏频道浏览

![TV 大屏频道浏览](shots/tv_v_01_browse.png)

### 手机与平板影视详情

![移动端影视详情](shots/mobile-own-emulator-detail-landscape.png)

### 影视搜索与内容发现

![移动端影视搜索](shots/mobile-own-emulator-search-love.png)

## 能力清单

- 影视点播：首页 / 全部 / 详情选集 / 搜索 / 收藏 / 历史 / 片库多维筛选
- 卫视直播：精编频道表 + 多源备份无感切换
- IPTV 电视：400+ 频道分组选台 + 足球直播广场
- 播放三级回退：Media3 → 中继 HLS → libVLC，自动换线 + 长跑稳定性治理
- DLNA 投屏（仅移动版）
- 卡密付费：一码 2 台设备，24 小时网络宽限
- 热更新：改后端与网页资源不发 APK
- 网页版：云端影视 + IPTV，与 APK 同构
- 后台管理：卡密 / 热更 / 公告 / 电视源 / 影视源
- OtvNav 遥控器焦点引擎

## 技术栈

| 层 | 选型 |
|---|---|
| 客户端 | Kotlin · Jetpack Compose（TV 大屏 + 触屏）· Media3 · Room · DataStore · Chaquopy |
| 内嵌后端 | Python 3（`proxy.py` / 抓取 / VOD 解析 / 队标）运行于 `127.0.0.1:8090` |
| 网页版 | 原生 HTML/CSS/JS，与 APK 逐行对照复刻；hls.js + PWA |
| 服务端 | Docker Compose · PostgreSQL + PostgREST · Nginx（自建 VPS） |
| 发布 | Gradle `assembleRelease` · 内容寻址 zip 热更 · APK 更新清单 |

## 快速开始

| 我想… | 怎么做 |
|---|---|
| 看文档 | [项目文档](docs/项目文档.md) 是唯一详细文档，从 §0 速览开始读 |
| 本地构建后端与网页 | `python tools/release_all.py`（只在本机生成部署包与热更包，不触碰公网） |
| 部署 / 推送 | `python tools/release_all.py --deploy` → 重打两个 APK → `node 后台管理系统/scripts/ops/hu-upload.mjs --site "https://<站点>"` 上传并发布热更 → `git push origin main` |
| 发新 APK | `android/` 或 `internal/mobile/` 下跑 `.\gradlew.bat assembleRelease`（Release 必须显式传 `-PapiBaseUrl=`，见项目文档 §11.4）；产物自动归档到 `apk/` |
| 跑测试 | `python -m unittest discover -s tools/backend-src/tests -p 'test_*.py'`（后端）＋ `python -m unittest discover -s tools/tests -p 'test_*.py'`（发布契约）＋ `node tools/backend-src/tests/web_resolver_harness.mjs`（网页换线） |
| 签发卡密 | `python tools/license_gen.py gen --plan <套餐> --count <数量>` → `python tools/selfhosted/import_licenses.py` 入库（支持 `--dry-run` 预检） |
| 验证改动 | 模拟器实测（截图 + logcat tag `OTV`）；网页版改动**必须浏览器实测** |

完整的发布口径、版本号规则、后台操作与排障速查都在 [项目文档](docs/项目文档.md)。

## 目录结构

```
My TV/
├── android/                  # TV 版工程（Kotlin + Compose TV）
│   └── app/src/main/
│       ├── java/com/qiubo/optimaltv/
│       │   ├── data/         # 数据层：repo（直播/点播/IPTV）、source、db(Room)、prefs；UrlGuard 双层 SSRF 防护
│       │   ├── playback/     # 播放层：引擎调度、解密中继、PlaybackHealthLog 长跑体检
│       │   ├── license/      # 卡密激活/本地验签/离线授权
│       │   ├── hotupdate/    # HotUpdateManager 热更下载与坏包防护
│       │   ├── ui/           # 界面：home/all/detail/search/live/tv/player/paywall…（OtvNav 在 components/Focus.kt）
│       │   └── App.kt / MainActivity.kt / EmbeddedBackend.kt
│       ├── assets/           # python-backend.zip（内嵌后端=热更对象）、iptv_curated.m3u、logos/、拼音词典
│       ├── python/           # Chaquopy Python 源码
│       └── cpp/pinyinime/    # AOSP 谷歌拼音解码器
├── internal/mobile/          # 移动版工程（触屏；data/playback/license 与 TV 字节级一致）
├── deploy/selfhosted/        # 自建部署（compose.yaml / Dockerfile.* / nginx.conf）
├── tools/                    # 后端源码、后台源码（admin_server.py/admin.html）、构建与发布脚本、SQL 迁移、测试
├── docs/项目文档.md           # 唯一详细文档
├── 后台管理系统/             # 上一代自建站点形态（已退役，独立仓库，仅供迁移对照）
├── shots/                    # README 展示截图
└── apk/                      # 构建产物（TV版/ 移动版/）
```

## 三条铁律

1. **改动默认同步所有版本**（TV + 移动 + 网页版，共享层跑 `python tools/sync_shared.py`，
   改 `proxy.py` 需双构建后重部署）——除非明确说只改某端。
2. **私钥与凭据不出本机**（`tools/keys/`、`codes.csv` 不进 git、不进云、不贴会话）。
3. **两工程不要并行构建**（构建目录虽已分治，历史教训仍在）。

## 文档索引

- [项目文档](docs/项目文档.md)
  - §0 项目速览 · §1 产品概览 · §2 总体架构 · §3 端侧功能模块 · §4 双工程共享层
  - §5 网页版 · §6 后台管理系统 · §7 自建 VPS 基础设施 · §8 数据库与迁移体系
  - §9 工具脚本速查 · §10 工作区约定 · §11 构建与版本号规则 · §12 发布流水线
  - §13 测试与验收体系 · §14 运维排障手册 · §15 已知限制与风险 · §16 合规与安全边界
- [AGENTS.md](AGENTS.md) — 本仓库的协作与发布约定（每次改动后的收尾流水线）
- [deploy/selfhosted/README.md](deploy/selfhosted/README.md) — 服务器连接方式、端口映射与密钥布局

## 当前版本

TV / 移动 APK **V1.2**（versionCode 120）· 热更 **v25**（均已发布）。

> **APK 版本号规则**（2026-09-22 重置）：版本清零后从 **V1.0** 重新计起，每版 +0.1；
> `versionCode = 主版本×100 + 次版本×10`（V1.0=100、V1.1=110、V1.2=120、V2.0=200），
> 必须严格递增——客户端只比较 `versionCode`。详见项目文档 §11.3。

## 内容来源与合规

影视信息来自豆瓣目录，播放线路来自项目内已配置的影视源，均属无授权聚合/采集类站点，
项目未取得其内容授权，仅供个人/家庭自用。详见项目文档 §16.1。