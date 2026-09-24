# My TV 网页性能、指标与全端验收设计

日期：2026-09-24  
状态：待用户规格审核  
依据：桌面全链路优化方案及 tools/backend-src/www、自建 Nginx/API 和两套 Android 产品设计。

## 1. 网页行为与静态资源

- 网页和 API 使用经验证的自建 HTTPS 主入口；HTML 使用 no-store，版本化或内容哈希 JS/CSS 使用长缓存 immutable。
- 首页优先显示本地持久缓存，再后台刷新；点卡后先显示标题、封面和基础详情，同时启动详情及 source resolve。
- 保留 HTTPS 视频 URL 浏览器直连优先；无法直连或播放失败时才使用受控 relay。正常播放的 relay ratio 应尽量低。
- 当前版本不包含 CloudBase relay、点播兜底和对象镜像。
- 静态资源版本或内容哈希由可重复构建过程生成；发布后检查 HTML 引用的文件存在且缓存头正确。

## 2. 统一端到端指标

当前 TV、移动和 Web 至少记录：app_start、home_first_content、detail_open、resolve_start、resolve_first_source、play_click、player_prepare、first_frame、buffer_start、buffer_end、auto_switch、engine_fallback、play_5min、play_30min、fatal。

每次播放形成 detail_ms、resolve_ms、prepare_ms、first_frame_ms、total_click_to_frame_ms；播放质量汇总 start_success_rate、first_frame_p50/p95、rebuffer_count/duration、source_switch_count、relay_ratio、VLC_fallback_ratio、30min_survival_rate。

服务端记录请求数、p50/p95/p99、source match/cache 命中率、source timeout/error/circuit open、metadata version、Postgres connections、CPU/RAM/disk I/O/network 与 Nginx 499/502/504。

只保存分析所需的匿名化标识与类别，不记录姓名、卡密、Cookie、完整播放 URL、授权头或可反推用户的设备标识。

## 3. Web/API 性能门槛

- metadata API P95 ≤ 100ms；业务 API P95 ≤ 200ms；Resolver fast path P95 ≤ 1.5s。
- HTTP 5xx < 0.1%；对象下载支持 Range、ETag、Last-Modified 和正确版本缓存头。
- 媒体流量不强制经过自建服务器；报告 direct/relay 比例以验证控制面与数据面分离。
- API 与 relay 分开设置 upstream timeout，避免源站延迟拖住普通业务 API。
- 公网 health 200 不能代替 VPS 源 API 出口验收。

## 4. 当前版本全端功能矩阵

测试交付物为 TV APK、移动 APK、Web；按项目产品清单覆盖：

- 首页、片库、筛选、搜索、详情、收藏、历史与播放进度；
- 点播线路/集数、快起播、直连与 relay 回退、自动换线、VLC 解码回退和续播；
- IPTV/电视源浏览与播放、公告、激活/license 校验、热更新与版本检查；
- 移动端 DLNA/投屏和播放器手势；
- TV 遥控焦点、方向键、返回及退出播放后的焦点恢复；
- Web 搜索、详情、片源、浏览器播放与重试；
- 当前支持的管理后台配置、发布、回滚与对象上传流程。

真实媒体集至少覆盖 10 部电影、10 部剧集、5 个 1080p、5 个低清旧源、5 个 HLS 和 5 个直链；可控测试源覆盖稳定 Wi-Fi、高延迟、10Mbps/5Mbps 限速、1% 丢包及 3s/10s 断网。每类至少覆盖 5 分钟与 30 分钟；2 小时长播采用代表性组合，并标明不适用条件。

## 5. 测试证据与发布门槛

- 后端及契约测试、Nginx/部署配置测试、Web 流程和资源缓存检查全绿。
- TV、移动两工程分别完成 unit tests、connected instrumentation tests 与 assembleRelease。
- Web、TV、移动功能清单逐项记录结果；版本号、设备/视口、网络、样本量、首帧/缓冲和错误日志可追溯。
- 截图覆盖 TV 横屏主页/片库/详情/播放，移动竖屏片库/详情/播放，Web 首页/搜索或详情/播放。页面已加载完成且改动效果可见。
- CloudBase 搜索结果分为当前代码/部署、测试和历史文档；当前代码/部署无可执行依赖，历史条目标记退役。当前支持版本运行时请求中 CloudBase 为零。
- 有任何功能、版本、目标指标、截图或网络验收未通过时，停止生产发布和最终切换。编译通过不能替代端到端验收。

## 6. 前后对比报告

实施前采集首屏、resolve、首帧、buffer、30 分钟存活率和 UI jank 基线；实施后以相同设备、内容和网络条件复测。报告分别展示 TV、移动、Web、样本量、通过项、失败项及外部阻塞项。没有实测数据的指标必须标为未验证。
