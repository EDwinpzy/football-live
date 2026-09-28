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

> **2026-09-28 复核更正**：本节及第五节的「待办」已全部完成，数据迁移早已落地。
> 详见文末「七、迁移完成复核」。下面的原始判断保留作为过程记录，**不代表当前状态**。

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

## 七、迁移完成复核（2026-09-28，实测）

直接 SSH 登录自建 VPS 核查，**推翻**了本文档此前「数据尚未迁移、线上是旧版栈」的判断。
那个判断源于探测 URL 用错（详见下），并非真实故障。

### 7.1 服务健康，无需重装

10 个容器全部 up 4 天+，`postgres` / `web-api` / `health` 均 healthy。
内部 8443 与外部 35208 逐端点探测一致。三个「404」全是**探测方式错误**，不是故障：

| 探测 | 404 原因 | 正确探测 | 结果 |
|---|---|---|---|
| `/v1/storages/object/hotupdate/` | `storage_service.py:37` 拒绝空 key | `…/hotupdate/backend-hu23.zip` | **200，40,763,541 字节** |
| `/admin` | 后台在根路径，代码从未实现 `/admin` 前缀 | `/` | **200，175,863 字节** |
| `/hotupdate` | 裸路径无 handler，需子路径 | `/hotupdate/manifest` | **200** |

`/hotupdate/check`、`/hotupdate/announce`、`/iptv/status`、`/metadata/health` 均 200。
对象目录 `/srv/mytv/objects/hotupdate/` 有 160MB 数据（4 个后端包、APK 更新、直播、metadata）。

### 7.2 数据早已迁完，且线上比 CloudBase 更新

自建 PostgreSQL 精确 `count(*)` 对比 CloudBase 导出（`artifacts/cloudbase-export-20260928/`）：

| 表 | CloudBase | 自建线上 | 说明 |
|---|---|---|---|
| `licenses` | 102 | **102** | 一致 |
| `iptv_channel` | 77 | **77** | 一致 |
| `iptv_line` | 127 | **127** | 一致 |
| `iptv_group` | 8 | **8** | 一致 |
| `iptv_config` | 1 | **1** | 一致 |
| `announcements` | 19 | **19** | 一致 |
| `hotupdate_pkg` | 2 | **4** | 线上多 2 |
| `activate_log` | 165 | **200** | 线上多 35（真实激活流量） |
| `hotupdate_log` | 2 | **15** | 线上多 13 |

**已激活卡数 7 > CloudBase 的 4**，说明迁移后自建站仍在正常受理激活。
迁移完成于 2026-09-24 前后，本文第五节 1~4 步均已执行完毕。

### 7.3 签名体系一致，存量票全部验签通过

这是「存量用户能否续期」的唯一决定性验证：

1. **私钥一致**：`/srv/mytv/secrets/license/private_key.pem` 与本机
   `tools/keys/private_key.pem` 的 PEM body SHA-256 均为
   `bb9886e99981b20d9ee583fb5cc02866` —— 同一把 P-256 密钥，APK 内置公钥必然验得过。
2. **全量验签**：在 VPS 上用该私钥导出公钥，对库中 102 张卡的 `ticket`
   按 `tools/license_gen.py` 的 canonical 串（`v|code|plan|days|issued|nonce`）
   逐张 ECDSA-SHA256 验签，结果
   `{"total":102,"verified_ok":102,"failed":0}`。
3. 102 张卡 `ticket` 均非空，`status` 全为 `active`，`expire_at` 无过期。

### 7.4 剩余唯一待办

**关停 CloudBase 环境**。数据、签名、服务三项前置条件已全部满足且有余量
（自建站多出的 35 次激活证明它已成为实际主站）。此操作不可恢复，执行前需用户确认。

## 六、本次新增的工具能力

`tools/selfhosted/import_licenses.py` 原先一运行即连库写入，没有只校验不写的中间态。
卡密迁移是单向门（导错库或签名不过后很难拆回），故新增 `--dry-run`：
复用既有 `_read_batch` 的签名校验与 cards/tickets 一致性检查，**完全不接触数据库**。

实测：清空 `OTV_LICENSE_IMPORT_*` 全部凭据后，`--dry-run` 仍成功
（`Validated 50 license tickets ... No database was contacted.`），
而真实导入按预期失败（`self-hosted PostgreSQL credentials are not configured`），
证明两条路径互不影响。契约测试见 `tools/tests/test_license_import_dry_run.py`（4 项）。
