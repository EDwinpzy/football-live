-- 补齐管理 RPC 的 EXECUTE 授权。
--
-- 背景：切栈到自建 PostgreSQL 时，admin_sold_migration.sql 和
-- iptv_migration.sql 只创建了函数体（内部用 auth.uid() 白名单授权），
-- 却漏掉了 GRANT。PostgREST 在进入函数体之前先检查连接角色的 EXECUTE
-- 权限，于是后台直接收到
--   {"error":"permission denied for function admin_licenses"}
-- 卡密列表、电视源、版本列表全部不可用，而页面本身能正常打开，
-- 人工不点进去看不到。
--
-- 为什么需要这个独立文件：admin_sold_migration.sql / iptv_migration.sql
-- 已经在生产库的 schema_migrations 账本里，改了内容也不会重跑。
-- 授权是幂等的（GRANT 可重复执行），所以单独做成一步，每次部署都跑，
-- 既能修好存量库，也能保证新建库和源码始终一致。
--
-- 授权对象本身不构成越权：这些函数的第一行仍然是
--   if coalesce(auth.uid(), '') <> '<管理员 uid>' then raise exception 'forbidden';
-- GRANT 只是让请求进得去，真正放行仍由函数体内的白名单决定。

-- 先补齐 IPTV 发布锁本体。iptv_publish_lock.sql 原本只通过
-- db_bootstrap.sql 的 \ir 引入，而生产库的 db_bootstrap 早已记进账本，
-- 于是锁表和锁函数从未真正建出来：发布锁 RPC 不存在，后台每次发布
-- 电视源都返回"电视源发布锁不可用"。这两个对象是幂等创建，放进这个
-- 每次都跑的修复步骤里，正好补上这个历史缺口。
\ir iptv_publish_lock.sql

-- Verify the lease really exists now. Without this check a failing \ir above
-- would let the GRANT block below error out on a missing signature, and psql's
-- ON_ERROR_STOP does not reliably propagate out of \ir — the migration could
-- report success while the lease was never created. Raise instead, so the
-- failure surfaces here and dependent services stay stopped.
DO $assert_publish_lock_ready$
BEGIN
    IF to_regclass('public.iptv_publish_lock') IS NULL
       OR to_regprocedure('public.iptv_admin_publish_lock(text,text)') IS NULL THEN
        RAISE EXCEPTION
            'IPTV publish lease is still missing after iptv_publish_lock.sql';
    END IF;
END
$assert_publish_lock_ready$;

-- Football now connects directly. Retire the old public RPC access without
-- deleting saved configuration or functions needed for rollback.
DO $retire_football_proxy$
BEGIN
    IF to_regprocedure('public.football_vpn_config_get()') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.football_vpn_config_get() FROM PUBLIC, anon, authenticated;
    END IF;
    IF to_regprocedure('public.football_vpn_config_save(jsonb)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.football_vpn_config_save(jsonb) FROM PUBLIC, anon, authenticated;
    END IF;
END
$retire_football_proxy$;

grant execute on function
  public.admin_licenses(text, text, text, integer, integer),
  public.admin_licenses(text, text, text, integer, integer, text, text),
  public.admin_mark_sold(text, boolean),
  public.admin_stats(),
  public.admin_license_v2_log_export(integer, integer),
  public.admin_license_v2_export(integer, integer)
  to authenticated;

grant execute on function
  public.iptv_admin_list(),
  public.iptv_admin_export(),
  public.iptv_admin_get_mode(),
  public.iptv_admin_group_list(),
  public.iptv_admin_versions(integer),
  public.iptv_admin_logs(integer),
  public.iptv_admin_rollback_begin(integer),
  public.iptv_admin_publish_begin(text),
  public.iptv_admin_set_mode(text),
  public.iptv_admin_delete_channel(bigint),
  public.iptv_admin_batch(jsonb),
  public.iptv_admin_import(jsonb, text),
  public.iptv_admin_group_create(text),
  public.iptv_admin_group_rename(bigint, text),
  public.iptv_admin_group_delete(bigint, text),
  public.iptv_admin_group_move(bigint, integer),
  public.iptv_admin_upsert_channel(bigint, text, text, text, integer, boolean, text, jsonb),
  public.iptv_admin_publish_confirm(integer, text, text, bigint, integer, integer, text),
  public.iptv_admin_publish_lock(text, text),
  public.iptv_log(text, text)
  to authenticated;

-- 客户端要读的公共接口保持 anon 可读。
grant execute on function
  public.announcement_latest(),
  public.hotupdate_check(integer, integer),
  public.hotupdate_report(text, integer, integer, integer, text, text)
  to anon, authenticated;

grant execute on function
  public.announcement_admin_get(),
  public.announcement_admin_publish(text, text, text),
  public.hotupdate_admin_list(integer, integer),
  public.hotupdate_admin_upsert(integer, text, bigint, text, text, boolean, integer, integer, text, text, timestamptz),
  public.hotupdate_admin_set_status(integer, text),
  public.hotupdate_admin_logs(integer),
  public.vod_source_admin_list(),
  public.vod_source_public_check(bigint)
  to authenticated;

-- 反向收紧：anon 不该碰到任何管理接口。
revoke execute on function
  public.admin_licenses(text, text, text, integer, integer),
  public.admin_licenses(text, text, text, integer, integer, text, text),
  public.admin_mark_sold(text, boolean),
  public.admin_stats(),
  public.admin_license_v2_log_export(integer, integer),
  public.admin_license_v2_export(integer, integer)
  from anon;

revoke execute on function
  public.iptv_admin_list(),
  public.iptv_admin_export(),
  public.iptv_admin_get_mode(),
  public.iptv_admin_group_list(),
  public.iptv_admin_versions(integer),
  public.iptv_admin_logs(integer),
  public.iptv_admin_rollback_begin(integer),
  public.iptv_admin_publish_begin(text),
  public.iptv_admin_set_mode(text),
  public.iptv_admin_delete_channel(bigint),
  public.iptv_admin_batch(jsonb),
  public.iptv_admin_import(jsonb, text),
  public.iptv_admin_group_create(text),
  public.iptv_admin_group_rename(bigint, text),
  public.iptv_admin_group_delete(bigint, text),
  public.iptv_admin_group_move(bigint, integer),
  public.iptv_admin_upsert_channel(bigint, text, text, text, integer, boolean, text, jsonb),
  public.iptv_admin_publish_confirm(integer, text, text, bigint, integer, integer, text),
  public.iptv_admin_publish_lock(text, text),
  public.iptv_log(text, text)
  from anon;

notify pgrst, 'reload schema';
