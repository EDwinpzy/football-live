# AGENTS.md — My TV 仓库约定

## 每次改动后的默认收尾（用户长期授权，无需再逐项确认）

用户于 2026-09-17 明确要求：**以后更新之后自动构建 APK、自动发布、自动更新文档、自动上传、自动推送**。
因此改动完成并通过验证后，直接按下面顺序执行到底，最后汇报结果与证据；不要再停下来征求发布确认。

> **发布前先检查，没有别的对话在改就直接发（用户 2026-09-18 要求，本仓库所有对话统一遵守）**：
> 同一工作区经常同时有多个对话在改这个仓库，所以**每次要走发布流水线时先检查一次**——
> 用 `codex_app` 的 `list_threads` 看本仓库（`D:\MyProjects\My TV`）还有没有其它 `active` 对话：
> - **没有**（本对话即将收尾、其它都已结束）→ 直接按下面 1~6 步跑完整流水线发布，
>   不用再等谁、不用再问用户确认；
> - **还有** → 本次只做第 1、5、6 步（测试、文档、提交推送），把第 2~4 步
>   （部署网页版 / 重打两个 APK / 上传发布热更）留给**最后一个收尾的对话自动执行**。
>
> 统一发布前先 `git status` 确认工作区干净：别人未提交的改动要么等其提交，要么不要打包进去。
> 目的：避免热更版本号抢占、半成品上线、APK 内嵌 zip 与构建产物不一致。

1. 测试：后端改动跑 `tools/backend-src/tests`（至少 `test_douban_catalog.py`）；Kotlin 侧以 `assembleRelease` 编译通过兜底。
2. 部署网页版 + 重建产物：`python tools/release_all.py --deploy`。
3. 构建 APK：`android` 与 `internal/mobile` 各跑一次 `.\gradlew.bat assembleRelease`（自动归档到 `apk/TV版/`、`apk/移动版/`）。
4. 上传并发布热更：`node 后台管理系统/scripts/ops/hu-upload.mjs --name "<名称>" --notes "<备注>"`（默认站点最高版本 +1、立即发布）。
5. 更新文档：`docs/04-records/版本史.md` 追加变更摘要，`docs/04-records/测试记录.md` 追加实测证据。
6. 提交并推送：`git add -A` → commit → `git push origin main`。

### 顺序与口径

- `tools/release_all.py` 会先刷新豆瓣快照（超过 24h 才抓），会改变后端 zip 内容，**必须先跑它再重打 APK**，
  否则 APK 内嵌 zip 与构建产物不一致。
- 热更包即 `android/app/src/main/assets/backend/python-backend.zip`；版本号取站点当前最高 +1。
- 并发期间只提交代码与文档是安全的；但**热更版本号与 APK 产物以最后一次统一构建为准**，
  中途任务不要再单独上传热更或另发 APK 版本。
- 发布后自检：线上 `/web/app.js` 与 `tools/backend-src/www/app.js` 的 SHA-256 一致；线上热更包 sha256 与本地 zip 一致；
  两个 APK 内嵌 zip 的 `douban_catalog.py`、`proxy.py`、`douban_snapshot.json`、`www/app.js` 与 `tools/backend-src` 逐一一致。
- 仍要先问的情况：不可恢复的破坏性操作（历史重写、删数据、下线在跑的东西），以及需要用户做产品取舍的改动。

## 文档

- 详细流程见 `docs/02-guides/开发与发布手册.md`；变更记录写入 `docs/04-records/`（时间倒序，附实测证据）。
- 私钥、卡密、服务密钥、Cookie、长期令牌不得进入 Git、云端部署包或文档。
