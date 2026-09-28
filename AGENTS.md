# AGENTS.md — My TV 仓库约定

## 并行委派

- 有两个或以上独立、边界清楚且可安全并行的子任务时，默认分派子 Agent 并行处理，以缩短耗时；每个 Agent 必须拥有明确且互不重叠的文件或模块范围。
- 汇总并检查所有 Agent 的改动后，再进行集成验证。
- 最终数据切换、生产发布、版本分配及其他共享状态操作保持串行。此项目偏好覆盖技能默认的“计划由单 Agent 逐项执行”，除非更高优先级或会话指令禁止委派。

## 每次改动后的默认收尾（用户长期授权，无需再逐项确认）

用户于 2026-09-17 明确要求：**以后更新之后自动构建 APK、自动发布、自动更新文档、自动上传、自动推送**。
因此改动完成并通过验证后，直接按下面顺序执行到底，最后汇报结果与证据；不要再停下来征求发布确认。

> **每次对话收尾前检查一次：没有别的对话在改就直接发布（用户 2026-09-18 要求，本仓库所有对话统一遵守）**：
> 每个对话做完自己的改动、写最终回复之前，用 `codex_app` 的 `list_threads` 看本仓库
> （`D:\MyProjects\My TV`）还有没有其它 `status=active` 的对话：
> - **没有** → 直接按下面 1~6 步跑完整流水线（打包 / 构建 APK / 发布上传 / 文档 / 推送），
>   不用再问用户确认；
> - **还有** → 本次就**不要打包、不要构建 APK、不要上传发布**，只做第 1、5、6 步
>   （测试、文档、提交推送），把发布留给后面收尾的那个对话——它收尾时会再检查一次，
>   那时没有别的对话在改了，就由它统一发布。
>
> 发布前 `git status` 要干净：别人未提交的改动不要打包进去。目的：避免热更版本号抢占、
> 半成品上线、APK 内嵌 zip 与构建产物不一致。**不要用定时任务来做这件事**——就是每次对话收尾时检查一次。

1. 测试：后端改动跑 `tools/backend-src/tests`（至少 `test_douban_catalog.py`）；Kotlin 侧以 `assembleRelease` 编译通过兜底。
2. 部署网页版 + 重建产物：`python tools/release_all.py --deploy`。
3. 构建 APK：`android` 与 `internal/mobile` 各跑一次 `.\gradlew.bat assembleRelease`（自动归档到 `apk/TV版/`、`apk/移动版/`）。
4. 上传并发布热更：`node 后台管理系统/scripts/ops/hu-upload.mjs --site "https://<正式域名>/admin" --name "<名称>" --notes "<备注>"`
   （站点最高版本 +1、立即发布）。**`--site` 或 `SITE_URL` 必须显式传入**：
   CloudBase 已退役，脚本不再内置默认站点，缺站点会直接 exit 2 失败，
   避免标准发布流程把热更静默发回已下线的云端。
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

- 详细流程见 `docs/项目文档.md`（第二部分：开发、发布与运维手册）；变更记录写入 `docs/04-records/`（时间倒序，附实测证据）。
- 本机调试经验与历史坑位记录在 `.learnings/`（含 `MEMORY.md`、`ERRORS.md`、`LEARNINGS.md`），
  只在本机保留、不入库，**改动前先读**。实机截图证据在 `artifacts/`（如 `artifacts/2026-09-ui-evidence/`）。
- 视觉改动的截图验证必须覆盖实际目标页面、目标方向/视口，并确认关键内容已加载、改动效果可见。仅打开页面、使用错误方向截图、或截图里只有报错/空数据都不算完成视觉验证；无法复现时明确记为未完成，不得声称截图验证通过。
- 私钥、卡密、服务密钥、Cookie、长期令牌不得进入 Git、云端部署包或文档。
