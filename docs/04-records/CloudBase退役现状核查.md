# CloudBase 退役现状核查（2026-09-28）

通过已登录的腾讯云 CloudBase 控制台（环境 `appletv-d5ge1bth794873f76`，个人版）
实地核查，非依据文档推断。**本文件不含任何卡密明文、签名或设备标识。**

## 一、数据库现状（PostgreSQL，schema `public`）

`licenses` 表结构（15 列）：
`code, plan, days, status, max_dev, devices(jsonb), ticket(jsonb), note,
created_at, sold_at, updated_at, activated_at, expire_at, redeemed_at, redeemed_to`

### 卡密存量

| 指标 | 值 |
|---|---|
| 总卡密 | **102**（`count(distinct code)` = 102，无重复） |
| 已激活 | **4**（`activated_at is not null`） |
| 当前状态 active | 102 |
| 已过期 | 0 |
| 套餐分布 | lifetime（月付卡另在本机签发批次中） |
| 创建时间跨度 | 2026-09-18 ~ 2026-09-19 |
| 每卡设备上限 | `max_dev = 2` |

### 其余业务表

| 表 | 行数 | 性质 |
|---|---|---|
| `activate_log` | 165 | 激活审计流水 |
| `hotupdate_pkg` | 2 | 热更包 |
| `hotupdate_log` | 2 | 热更记录 |
| `iptv_channel` | 77 | **直播频道（迁移必须带走）** |
| `iptv_line` | 127 | 直播线路 |
| `iptv_group` | 8 | 分组 |
| `iptv_config` | 1 | 配置 |
| `announcements` | 19 | 公告 |

以上表名与 `tools/selfhosted/import_cloudbase.py` 的 `BUSINESS_TABLES` 完全对应，
迁移脚本无需扩展即可承接。**数据量很小（总计约 500 行），适合一次性全量导出。**

## 二、线上服务实际状态

对 `https://appletv-d5ge1bth794873f76.service.tcloudbase.com` 逐端点探测（绕过本地代理）：

| 端点 | 状态 | 说明 |
|---|---|---|
| `POST /activate` | 200 | 入口存活，但**返回 `{"ret":426,"msg":"upgrade_required"}`** |
| `GET /admin` | 200 | 页面可访问 |
| `POST /admin` | 401 | 鉴权正常 |
| `GET /web/` | 200 | 静态托管可访问 |
| `GET /iptv/latest` | 200 | 返回真实直播 m3u8 |
| `POST /api/login` | — | `INVALID_PATH`（该路径在当前网关不存在） |

**关键判断**：`upgrade_required` 表明环境已因**套餐/配额**降级，激活功能实际已不可用；
控制台「运行异常」却显示无异常。两者结合，说明这不是代码故障，而是**计费层限制**。

## 三、自定义域名未接入（此前 403 的真正原因）

控制台「域名及路由」页显示：

- 域名管理：**暂无数据**（无自定义域名）
- 路由管理：**暂无数据**（无任何路由）

因此 `mytv-cloud.pengzhiyuan0724.chatgpt.site` 返回 403 的原因**不是** WAF 拦截，
而是**该域名根本没有指向这个 CloudBase 环境**。此前根据 403 响应体
（Cloudflare `Attention Required` 页面）推断为 WAF 规则，**该判断不成立，特此更正**。

该域名的 TLS 证书本身有效（Google Trust Services 签发，链到 GlobalSign 公共根，
有效期至 2026-12-14），可继续用作自建站点的正式域名。

## 四、结论：现在关停的影响面

1. **卡密数据仅 102 张、其中已激活 4 张**，迁移工作量很小。
2. **`activate` 已因 `upgrade_required` 实质不可用**——也就是说线上激活路径**当下已经断了**，
   不是「关停会打断」而是「本来就断着」。这降低了立即关停的破坏性。
3. **仍需先导出的数据**：`licenses`（含 ticket 签名，迁移后自建站必须能验签）、
   `iptv_channel/line/group`（77/127/8 直播数据）、`announcements`、`hotupdate_*`。
4. **未迁移就关停的后果**：4 名已激活用户无法续期/换设备；直播频道数据丢失需重新抓取。

## 五、待办（按顺序）

1. 用 `import_cloudbase.py` 的导出格式全量导出 8 张表（数据量小，可一次性完成）。
2. 在自建 PostgreSQL 执行 `apply_schema_migrations` 建表，再导入并核对行数。
3. **用本机已签发批次交叉验证**：`tools/out/` 下 21 个批次、530 张卡的签名
   已通过 `import_licenses.py --dry-run` 校验（本次新增该 dry-run 能力）。
4. 自建激活接口能识别存量 `ticket` 签名后，再切换客户端 `API_BASE_URL`。
5. 观察 1–2 周确认无异常，最后关停 CloudBase 环境。

## 六、本次新增的工具能力

`tools/selfhosted/import_licenses.py` 原先一运行即连库写入，没有只校验不写的中间态。
卡密迁移是单向门（导错库或签名不过后很难拆回），故新增 `--dry-run`：
复用既有 `_read_batch` 的签名校验与 cards/tickets 一致性检查，**完全不接触数据库**。

实测：清空 `OTV_LICENSE_IMPORT_*` 全部凭据后，`--dry-run` 仍成功
（`Validated 50 license tickets ... No database was contacted.`），
而真实导入按预期失败（`self-hosted PostgreSQL credentials are not configured`），
证明两条路径互不影响。契约测试见 `tools/tests/test_license_import_dry_run.py`（4 项）。
