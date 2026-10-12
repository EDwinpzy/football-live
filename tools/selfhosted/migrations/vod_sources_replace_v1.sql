-- 2026-10-12 影视源清单重置：只保留「好好看」，其余替换为实测可用的「非凡资源」。
--
-- 依据 artifacts/可用源清单-2026-10-12.md（HTTP 可达 + JSON/XML 格式校验 + 条目抽样）：
--   * 保留 hhkan（好好看）——目录/卡片/详情的主数据源，且该清单实测站点仍在线；
--   * 新增 非凡资源（cj.ffzyapi.com，苹果 CMS 标准 provide/vod）——?ac=list 返回
--     total 98533，?ac=detail&wd= 可关键词搜索，直链组 ffm3u8 能出 index.m3u8；
--   * 移除 暴风 / 极速 / 金鹰 / 虎牙 / 豪华 / 红牛 / 速播 / 360 / 猫眼 / VN影视。
--     其中「暴风采集」在同一份清单里明确标 ❌（多份配置均失败），其余不再采用。
--
-- 为什么不直接改 vod_sources_migration.sql：那份迁移已经进 schema_migrations
-- 账本，SQL 摘要被 apply_schema_migrations.psql 逐字节锁定——改动它会让
-- migration_hashes_match 为假并 \quit 3，整次部署失败。所以清单变更必须走新迁移。
--
-- 迁移后立刻发布一个版本：客户端只在 version 变大时才把发布的配置原子写进
-- vod_sources.runtime.json（HotUpdateManager.syncVodSources），只改草稿不发版
-- 等于没改。
--
-- 已知边界：apply_schema_migrations.psql 的「空账本 + 完整 legacy schema」兜底分支
-- 会把 expected_schema_migration_hashes 里的每一项都记为已应用（不重放数据变更
-- SQL），所以那种库不会执行本文件。现网账本非空（2026-10-12 实测 14 行），走的是
-- 逐项 pending 判定，本迁移会正常执行。

delete from vod_source_drafts;

insert into vod_source_drafts(id,name,site_url,api_url,adapter_type,enabled,sort_order) values
('10000000-0000-0000-0000-000000000001','好好看','https://www.haohankan.com','','hhkan',true,10),
('10000000-0000-0000-0000-000000000011','非凡资源','https://cj.ffzyapi.com','https://cj.ffzyapi.com/api.php/provide/vod/','macms_json',true,20);

select vod_source_admin_publish();
