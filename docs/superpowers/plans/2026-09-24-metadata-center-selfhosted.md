# MyTV 影视元数据中心落地方案（自建 VPS）

**目标：** 把「影视元数据中心」落到已上线的自建 VPS 上，让元数据不再依赖请求时的第三方抓取，
并给设备端留出稳定的 `mytv_id` + 版本化数据包接口。

**运行位置：** `103.236.97.137`（服务商 NAT，HTTPS `35208` → 内部 `8443`）。数据库、构建、发布全部在 VPS 内完成；
公开产物复用现有对象存储（`/srv/mytv/objects/hotupdate/metadata/`），匿名只读、支持 Range。

**来源文档：** 用户提供 `C:\Users\63054\Desktop\MyTV_影视元数据中心完整方案.md`；本文档里的取舍以本仓库现状为准。

## 与本仓库现状的关系

- 现有客户端在请求时抓豆瓣（`tools/backend-src/douban_catalog.py`），失败时回落到内置 `douban_snapshot.json`
  （565 条索引 / 160 条详情）；设备端另有按 `douban_id` 建键的本地搜索库（`tools/backend-src/media_index.py`）。
- 本轮**不替换**线上接口，也不改 `/vod/*`、`/web/*` 的现有行为；新增能力全部走新路径。
- 设备端仍以 `douban:<id>` 作为兼容引用；新库同时保存 `external_id(provider='douban')` 与永久 `mytv_id`，
  为后续「收藏/历史/播放引用换 ID」留出一次性迁移的映射基础。

## 交付范围（本轮）

1. `tools/metadata_center/`：统一数据模型、`metadata.sqlite` schema、拼音与多音字、匹配/合并、
   构建器、发布器（全量 + 增量 + manifest）、校验器、只读查询服务、CLI。
2. VPS 部署：`metadata-api`（只读查询，新容器）+ `metadata-builder` / `metadata-refresh`
   （ops profile 一次性任务）+ Nginx 新增 `/metadata/` 位置。不动现有容器行为、不改安全组、不改 NAT。
3. 发布物：`/v1/storages/object/hotupdate/metadata/manifest.json`、`metadata-v<版本>.sqlite(.gz)`、
   `delta-<from>-<to>.sql.gz`。版本号＝`YYYYMMDDnn`。
4. 测试与验证证据：单元测试 + VPS 上真实构建/发布/下载/Hash 比对 + 现有接口回归。

## 关键决策

- **ID 规则**：`mytv_id` 从 10,000,001 起单调分配，分配结果写进 `external_id`；重建时先读旧库沿用旧分配，
  因此同一部影片的 ID 跨版本稳定，增量包才有意义。已有 `douban:<id>` 字符串继续有效。
- **拼音**：沿用 `pinyin_map.json`（单读音表），补一层「词组覆盖表」解决「长安 → changan」这类多音字，
  并对常见多音字额外索引候选读音（如 长 → chang/zhang），保证 `changan` 与 `zhangan` 都能命中。
- **搜索**：SQLite FTS5 前缀检索为主（中文标题、别名、全拼、首字母、演员、导演），
  无 FTS5 时回落到 LIKE 打分；`search_doc` 表让设备端能复刻同一套排序。
- **压缩**：用标准库 gzip（确定性写入，mtime=0），manifest 里声明 `compression`，不引入第三方依赖。
- **增量**：对比两版数据库逐表主键差异，生成 `INSERT OR REPLACE` / `DELETE` 语句包；
  设备端先在副本上应用，`PRAGMA integrity_check` + SHA-256 通过后再原子替换。
- **发布原子性**：先写临时目录并校验，再 `os.replace` 就位；`manifest.json` 最后落盘，避免读到半成品。
- **数据来源**：以现有快照为种子（VPS 上刷新失败也能用上一份好数据），Provider 抽象保留 TMDB/IMDb 接入位；
  本轮不新写第三方抓取器。
- **不做的事**：不改 `proxy.py` / `douban_catalog.py` 的线上路径，不发 APK、不发热更
  （本仓库还有另一个对话在改，按 AGENTS 的收尾规则本轮不打包发布）。

## 任务清单

- [x] T1 统一数据模型与 SQLite schema（含 FTS5 搜索表）
- [x] T2 拼音与多音字（词组覆盖 + 候选读音）
- [x] T3 Provider 抽象与快照导入
- [x] T4 匹配/合并与人工复核队列
- [x] T5 构建器（ID 分配、完整性检查、计数报告）
- [x] T6 发布器（全量/增量/manifest/原子落盘/保留策略）
- [x] T7 只读查询服务与 CLI
- [x] T8 单元测试
- [x] T9 VPS 部署与新容器/Nginx 路由
- [x] T10 VPS 上真实构建发布 + 线上验证证据
- [x] T11 文档与提交推送

## 验收口径

- 构建产物 `PRAGMA integrity_check` = `ok`，计数与源数据一致，重复运行得到相同 `mytv_id`。
- 搜索：输入 `bwbj` / `bawang` / `霸王` / `changanshiershichen` 能命中对应条目。
- 线上：`manifest.json` 可匿名 HTTPS 拉取，sha256/size 与本地一致，断点续传（Range）返回 206。
- 回归：`/activate`、`/hotupdate/check`、`/iptv/latest`、`/web/`、后台登录与 `/health` 行为不变。

## 执行结果与实测证据（2026-09-24）

| 验收项 | 实测 |
|---|---|
| 构建完整性 | `integrity=ok`，`data_version=2026092401` ✅ |
| 规模 | media 565 / media_title 992 / person 3512 / credit 4321 / image 1130 / rating 565 ✅ |
| 体积 | 未压缩 2,093,056 B；gzip 867,287 B ✅ |
| `mytv_id` 稳定性 | 重跑 `reused_ids=565 / new_ids=0`，内容指纹不变 → `unchanged` ✅ |
| 搜索 | `bwbj` / `bawang` → 霸王别姬；`changandelizhi` → 长安的荔枝；`zhangan…` 变体同样命中 ✅ |
| 线上分发 | 200 + SHA-256 与 manifest 一致；`Range: bytes=0-99` → `206` + 100 B ✅ |
| 回归 | `/health`、`/activate`、`/hotupdate/check`、`/iptv/latest`、`/web/`、后台 `/` 全部与迁移验收一致 ✅ |
| 自动化 | `mytv-metadata-refresh.timer` 已启用，下次 2026-09-23 21:31 UTC（05:30 Asia/Shanghai）✅ |

执行中的三处修正（都记在提交历史里）：

1. 二级表（人物/类型/地区）原先按原始名建索引、按归一化名查表，重建时会重发新 ID；改成两边都用归一化键，
   并保留上一版拼写。
2. 图片行曾按「内容未变就跳过写入」处理，但数据库每次都是重建，跳过等于丢行；改为始终写入、只沿用缓存副本。
3. 版本号先按 UTC 生成，会出现中国凌晨发布却标前一天；改为固定 `UTC+8`。

仍未完成的部分：设备端（APK 内置后端 / Android）读这套数据库尚未接线，需要单独一次客户端改动与打包；
本轮按 AGENTS 的并发收尾规则没有打包发布。
