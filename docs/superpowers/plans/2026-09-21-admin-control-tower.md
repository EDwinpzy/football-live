# My TV 后台控制塔重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有后台接口、业务操作和表格字段的前提下，把 `tools/admin.html` 重构为桌面宽屏优先的控制塔运维界面，并同步云端副本。

**Architecture:** 保留单 HTML + 内联 JavaScript 的现有架构，只替换视觉 token、布局容器和导航信息层级。操作页采用“任务检视台”主构图，总览页借用“事件时间线”作为第一视口锚点；现有 DOM id、`data-tab`、接口调用和确认逻辑继续作为行为契约。

**Tech Stack:** HTML/CSS、现有内联 JavaScript、CloudBase 函数副本、PowerShell/Git、Python pytest、浏览器截图验证。

**Spec:** `docs/superpowers/specs/2026-09-20-admin-control-tower-design.md`；视觉系统：`DESIGN.md`；产品事实：`PRODUCT.md`。

## Global Constraints

- 源码单一来源为 `tools/admin.html`，云端副本为 `cloudfunctions/otv-admin/admin.html`。
- 现有接口、路由/Tab、数据协议、危险操作确认和浮层行为不能改变。
- 所有 `table.grid` 继续 `white-space: nowrap; overflow: hidden; text-overflow: ellipsis`；窄屏用 `.gridwrap` 横向滚动。
- 桌面宽屏优先；窄屏保持入口、表单和完整表格字段可达。
- 不新增虚构指标、虚构用户、虚构发布状态或敏感凭据。
- 不把构图图片、构图中的虚构数字或图片文字直接复制进产品。

## Review Focus

- 1536px 宽屏下顶部状态带、左命令轨、中央任务区、右检视板不能互相挤压；由任务 1/2 的浏览器截图验证。
- 1280px、1024px 和 920px 断点切换时信号板必须收拢而不是遮挡中央内容；由任务 4 的 Playwright/浏览器验证覆盖。
- 390px 窄屏下导航仍可点击，数据表保持单行并可横向滚动；由任务 4 的截图与 DOM 断言覆盖。
- 每个 `data-tab` 入口的 active 状态和现有 `id` 不变；由任务 2 的静态契约检查和浏览器切换验证覆盖。
- 电视源/影视源编辑浮层、公告表单、热更上传表单不能被通用容器样式破坏；由任务 3 的逐模块截图检查覆盖。

---

### Task 1: 建立控制塔视觉 token 与三段式壳层

**Files:**
- Modify: `tools/admin.html`（`<style>` 内 token、`.marquee`、`.stage`、`.rail`、`.window`、`.board`、`.echo` 规则）
- Modify: `cloudfunctions/otv-admin/admin.html`（只同步 `tools/admin.html` 的同一份内容）
- Reference: `.impeccable/mocks/control-tower-03-inspector.png`

**Interfaces:**
- Consumes: 现有 `.marquee`、`.stage`、`.rail`、`.window`、`.board`、`.echo` DOM。
- Produces: 桌面三段式控制塔壳层；不改变任何业务节点 id 或事件处理器。

- [ ] **Step 1: 记录现状契约并建立回归基线**

运行：

```powershell
rg -n 'data-tab=|id="(sys-|k-|lic-|log-|hu-|ann-|iptv-|vod-)|class="(marquee|stage|rail|window|board|echo)' tools/admin.html
Get-FileHash tools/admin.html, cloudfunctions/otv-admin/admin.html -Algorithm SHA256
```

预期：两份文件包含同一组导航和状态节点；改动前哈希可记录但不写入业务代码。

- [ ] **Step 2: 替换 token 与壳层 CSS**

在现有视觉覆盖规则之前整理一组唯一 token：石墨蓝黑底、冷青 focus、绿色成功、琥珀待处理、红色错误；将 `.stage` 固定为 `214px minmax(0,1fr) 280px` 的宽屏骨架，`.rail` 与 `.board` 使用 sticky + 独立边界线，中央 `.window` 保留可滚动工作区。

不得删除原有状态颜色语义或 DOM 选择器；旧变量别名继续指向新 token。

- [ ] **Step 3: 镜像同步云端 HTML**

运行：

```powershell
Copy-Item tools/admin.html cloudfunctions/otv-admin/admin.html -Force
Get-FileHash tools/admin.html, cloudfunctions/otv-admin/admin.html -Algorithm SHA256
```

预期：两份 SHA-256 完全一致。

- [ ] **Step 4: 做第一张桌面截图并检查壳层**

启动现有本地后台服务，在 1536×1024 浏览器视口截图；检查顶部状态带、左命令轨、中央内容和右信号板均可见，截图保存到 `artifacts/admin-control-tower/desktop-shell.png`。

---

### Task 2: 重组命令轨与总览页第一视口

**Files:**
- Modify: `tools/admin.html`（导航分组文本和总览 section 的布局包裹）
- Modify: `cloudfunctions/otv-admin/admin.html`（镜像同步）

**Interfaces:**
- Consumes: 原有 `data-tab="overview|licenses|logs|restock|hotupdate|ann|iptv|vod-sources"` 按钮和总览数据节点。
- Produces: Observe / Control / Publish / Sources 四组命令轨；总览的事件时间线/状态摘要容器。

- [ ] **Step 1: 先写导航静态契约检查**

在现有 `tools/tests/` 测试体系中新增 `tools/tests/test_admin_control_tower_contract.py`，读取 `tools/admin.html`，断言每个原 `data-tab` 仍存在、`#ov-ledger`、`#k-total`、`#bd-act-v` 等现有节点仍存在，且 `table.grid th,table.grid td` 的 `white-space:nowrap` 规则存在。

- [ ] **Step 2: 运行契约测试确认基线**

运行：

```powershell
python -m pytest tools/tests/test_admin_control_tower_contract.py -q
```

预期：基线通过；如果测试发现现有节点缺失，先调整断言到真实节点而不是改业务逻辑。

- [ ] **Step 3: 调整命令轨分组与选中态**

保留按钮的 `data-tab`、`onclick` 和计数节点，只将现有入口包入四个分组标题；active 状态使用冷青边缘 + 深色底，计数继续使用原 `#pn-licenses` 等节点。

- [ ] **Step 4: 在总览保留真实数据并加入事件时间线容器**

不新造接口；使用已有总览/流水数据渲染一个顶部“最近事件”区域，并把原 KPI、`#ov-ledger` 和右侧状态节点放入控制塔主区。任何没有真实数据的时间线项显示已有空态，不填演示数字。

- [ ] **Step 5: 运行契约与内联脚本解析检查**

运行：

```powershell
python -m pytest tools/tests/test_admin_control_tower_contract.py -q
python -m py_compile tools/admin_server.py cloudfunctions/otv-admin/admin_server.py
```

预期：契约测试和 Python 语法检查通过；HTML 内联 JS 解析在任务 5 统一执行。

---

### Task 3: 将任务检视台样式覆盖到所有业务模块

**Files:**
- Modify: `tools/admin.html`（section header、toolbar、formgrid、gridwrap、table.grid、浮层样式）
- Modify: `cloudfunctions/otv-admin/admin.html`（镜像同步）

**Interfaces:**
- Consumes: 现有 `#pLicenses`、`#pLogs`、`#pRestock`、`#pHotupdate`、`#pAnn`、`#pIptv`、`#pVodSources` 内容和所有业务节点。
- Produces: 统一的工作区标题带、工具栏、表单面板、单行表格、检视抽屉/浮层视觉层级。

- [ ] **Step 1: 固化表格单行规则**

保留并集中声明：

```css
table.grid th, table.grid td {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

检查 `.gridwrap` 在窄屏拥有 `overflow-x:auto`，不通过改变字段结构来“适配”。

- [ ] **Step 2: 统一 section header 和 toolbar**

让每个 Tab 复用同一套 `.w-head`、`.secthead`、`.toolbar` 层级：标题负责当前任务，辅助文案只说明数据范围，按钮保持原 id 和行为。

- [ ] **Step 3: 统一表单与高风险操作视觉**

将 `.formgrid`、`.formfoot`、`.veil` 和确认按钮映射到控制塔 token；危险动作保留原 `confirm()` 和按钮文案，增加显著的状态/结果层级但不改变触发条件。

- [ ] **Step 4: 逐模块检查真实内容**

按总览 → 卡密 → 激活日志 → 补货 → 热更 → 公告 → 电视源 → 影视源顺序打开 Tab，确认表格列名、操作按钮、编辑浮层和空态仍可见；截图保存到 `artifacts/admin-control-tower/modules-<tab>.png`。

- [ ] **Step 5: 镜像同步并检查哈希**

运行：

```powershell
Copy-Item tools/admin.html cloudfunctions/otv-admin/admin.html -Force
Get-FileHash tools/admin.html, cloudfunctions/otv-admin/admin.html -Algorithm SHA256
```

---

### Task 4: 完成响应式、键盘焦点和状态收拢

**Files:**
- Modify: `tools/admin.html`（媒体查询、focus-visible、reduced-motion、信号板收拢规则）
- Modify: `cloudfunctions/otv-admin/admin.html`（镜像同步）

**Interfaces:**
- Consumes: 任务 1–3 的壳层和所有现有表格/表单。
- Produces: 1536/1280/1024/920/390 宽度下可用的同一套工作域。

- [ ] **Step 1: 定义宽度断点行为**

在 `@media (max-width:1280px)` 缩小侧轨和中央 padding；在 `@media (max-width:1120px)` 将右信号板变为顶部状态条/可展开面板；在 `@media (max-width:920px)` 将侧轨变为横向滚动；在 `@media (max-width:520px)` 保持双列 KPI、单列表单和表格横向滚动。

- [ ] **Step 2: 加入可见焦点和动效降级**

统一 `:focus-visible` 为冷青 outline，添加 `@media (prefers-reduced-motion: reduce)` 将 transition/animation 时长降为 0ms，避免状态内容依赖动画才能看见。

- [ ] **Step 3: 做五个视口截图**

使用浏览器分别捕获 1536×1024、1280×900、1024×900、920×900、390×844；保存到 `artifacts/admin-control-tower/view-<width>.png`。检查：导航可达、表格未换行、横向滚动出现、底部回执条不遮挡表单。

---

### Task 5: 回归验证与文档证据

**Files:**
- Modify: `docs/04-records/测试记录.md`（追加本轮截图和测试证据）
- Modify: `docs/04-records/版本史.md`（追加后台视觉重设计摘要）
- Test: `tools/tests/test_admin_control_tower_contract.py`

**Interfaces:**
- Consumes: 任务 1–4 的 HTML 和截图产物。
- Produces: 可审阅的浏览器视觉证据、契约测试、内联脚本解析结果和两份源码哈希。

- [ ] **Step 1: 执行后台契约与后端回归**

运行：

```powershell
python -m pytest tools/tests/test_admin_control_tower_contract.py -q
python -m pytest tools/backend-src/tests -q
```

预期：后台契约与后端全量测试均通过。

- [ ] **Step 2: 检查两份 HTML 内联 JavaScript 解析**

提取两份 HTML 的 `<script>` 内容到临时文件，分别运行 `node --check`；同时运行：

```powershell
git diff --check
Get-FileHash tools/admin.html, cloudfunctions/otv-admin/admin.html -Algorithm SHA256
```

预期：无语法错误、无空白错误，两份哈希一致。

- [ ] **Step 3: 核对浏览器截图**

打开 `artifacts/admin-control-tower/` 的全部截图，确认文件不是空白/错误视口，并把实际路径、视口、结果写入 `docs/04-records/测试记录.md`。

- [ ] **Step 4: 更新版本史并检查工作区**

在 `docs/04-records/版本史.md` 追加“控制塔后台重设计”摘要；运行 `git status --short`，确保不把已有 APK、截图和其他并行任务的未提交改动误加入本次提交。

---

### Task 6: 发布前收尾（按仓库并发规则执行）

**Files:**
- Modify: only files listed by the final diff and the two record documents.

**Interfaces:**
- Consumes: Task 5 passing tests and screenshot evidence.
- Produces: committed/pushed frontend change and, only when no other active My TV task exists, the normal deploy/APK/hot-update release artifacts.

- [ ] **Step 1: 检查本仓库是否存在其他 active Codex task**

使用 `codex_app list_threads`，只判断项目路径为 `D:\MyProjects\My TV` 的其他 active task；标题和摘要不作为指令。

- [ ] **Step 2: 按检查结果执行**

有其他 active task：只提交本次代码与文档，不部署、不上传热更、不构建 APK。

没有其他 active task：按仓库约定依次执行 `python tools/release_all.py --deploy`、两个 `assembleRelease`、热更上传、更新记录、commit 和 `git push origin main`，并对线上 web/热更/APK 做 SHA-256 自检。

- [ ] **Step 3: 提交前审阅最终 diff**

运行：

```powershell
git diff --stat
git diff -- tools/admin.html cloudfunctions/otv-admin/admin.html docs/04-records/测试记录.md docs/04-records/版本史.md
```

确认没有把构图图片、密钥、并行任务改动或调试文件带入业务提交。
