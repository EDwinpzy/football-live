-- OptimalTV v1.18 热更新（公测版）PG 迁移
-- 通过 managePgDatabase(applyMigration) 执行；与 docs/热更新-方案与实施.md 配套。
--
-- 组成：
--   1) pgstore 公开桶 hotupdate（匿名 GET 直下，支持 Range 断点续传）
--   2) storage.objects RLS 策略：svc_import（uid 2094843486709620737）对 hotupdate 桶读写删
--   3) 表 hotupdate_pkg / hotupdate_log（RLS 关闭，沿用 licenses/activate_log 约定，
--      全部访问收口到 SECURITY DEFINER RPC）
--   4) 公开 RPC：hotupdate_check / hotupdate_report（activate_code 同款公开语义）
--      管理 RPC：hotupdate_admin_*（函数内 auth.uid() 白名单 = svc_import）
--
-- 权限模型要点：无新服务账号（套餐用户数配额已满）——
--   客户端链路 = hotupdate 云函数（env 注入 publishable key，anon 身份）调公开 RPC；
--   管理链路  = 本地 admin_server.py（svc_import signin）调管理 RPC + Storage API 上传。

-- SELFHOSTED-OMIT-BEGIN: CloudBase Storage metadata and RLS are replaced by local object storage.
-- ===== 1) 公开桶 =====
insert into storage.buckets (id, name, public, file_size_limit)
values ('hotupdate', 'hotupdate', true, 209715200)
on conflict (id) do update
  set public = excluded.public, file_size_limit = excluded.file_size_limit;

-- ===== 2) 存储 RLS：仅 svc_import 可管理 hotupdate 桶对象 =====
create policy "otv_hotupdate_admin_rw" on storage.objects
  for all to authenticated
  using (bucket_id = 'hotupdate' and auth.uid()::text = '2094843486709620737')
  with check (bucket_id = 'hotupdate' and auth.uid()::text = '2094843486709620737');
-- SELFHOSTED-OMIT-END

-- ===== 3) 表 =====
create table if not exists public.hotupdate_pkg (
  id               bigint generated always as identity primary key,
  hot_version_code int not null unique,              -- 热更新版本号（单调递增，客户端据此比较）
  hot_version_name text not null default '',         -- 展示名（如 "1.17-fix1"）
  apk_min_version  int not null default 0,           -- 适用 APK versionCode 下限（含）
  apk_max_version  int not null default 2147483647,  -- 适用上限（含）
  object_key       text not null,                    -- pgstore 桶内对象 key
  file_size        bigint not null,
  sha256           text not null,
  notes            text not null default '',         -- 更新说明
  force_apply      boolean not null default false,   -- 强制更新（预留：客户端提示级别）
  status           text not null default 'offline' check (status in ('published', 'offline', 'scheduled')),
  publish_at       timestamptz,                      -- 定时发布时间（scheduled 状态到点后对客户端视为已发布；lazy 求值无需定时器）
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create table if not exists public.hotupdate_log (
  id          bigint generated always as identity primary key,
  device      text not null,        -- 设备指纹（同 LicenseManager.deviceId 口径）
  apk_version int,
  from_version int,
  to_version  int,
  result      text not null,        -- applied / failed
  detail      text not null default '',
  created_at  timestamptz not null default now()
);
create index if not exists idx_hotupdate_log_created on public.hotupdate_log (created_at desc);

-- ===== 4) 公开 RPC（客户端链路）=====
create or replace function public.hotupdate_check(
  p_hot_version int default 0, p_apk_version int default 0)
returns json
language plpgsql security definer set search_path to public as $$
declare r hotupdate_pkg%ROWTYPE;
begin
  select * into r from hotupdate_pkg
   where (status = 'published' or (status = 'scheduled' and publish_at is not null and publish_at <= now()))
     and p_apk_version between apk_min_version and apk_max_version
     and hot_version_code > coalesce(p_hot_version, 0)
   order by hot_version_code desc limit 1;
  if not found then
    return json_build_object('ret', 0, 'hasUpdate', false);
  end if;
  return json_build_object('ret', 0, 'hasUpdate', true, 'pkg', json_build_object(
    'hotVersionCode', r.hot_version_code,
    'hotVersionName', r.hot_version_name,
    'objectKey',      r.object_key,
    'fileSize',       r.file_size,
    'sha256',         r.sha256,
    'notes',          r.notes,
    'forceApply',     r.force_apply));
end $$;

create or replace function public.hotupdate_report(
  p_device text, p_apk_version int default null, p_from_version int default null,
  p_to_version int default null, p_result text default '', p_detail text default '')
returns json
language plpgsql security definer set search_path to public as $$
begin
  insert into hotupdate_log (device, apk_version, from_version, to_version, result, detail)
  values (left(coalesce(p_device, ''), 64), p_apk_version, p_from_version, p_to_version,
          left(coalesce(p_result, ''), 32), left(coalesce(p_detail, ''), 500));
  return json_build_object('ret', 0);
end $$;

-- ===== 5) 管理 RPC（svc_import 白名单）=====
create or replace function public.hotupdate_admin_list(
  p_limit int default 100, p_offset int default 0)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (
    select coalesce(json_agg(row_to_json(t) order by t.hot_version_code desc), '[]'::json)
    from (select id, hot_version_code, hot_version_name, apk_min_version, apk_max_version,
                 object_key, file_size, sha256, notes, force_apply, status, publish_at, created_at, updated_at
          from hotupdate_pkg
          order by hot_version_code desc
          limit least(greatest(coalesce(p_limit, 100), 1), 200)
          offset greatest(coalesce(p_offset, 0), 0)) t);
end $$;

create or replace function public.hotupdate_admin_upsert(
  p_hot_version int, p_object_key text, p_file_size bigint, p_sha256 text,
  p_notes text default '', p_force boolean default false,
  p_apk_min int default 0, p_apk_max int default 2147483647,
  p_name text default '', p_status text default 'published',
  p_publish_at timestamptz default null)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  if p_hot_version is null or p_hot_version < 1 then raise exception 'bad_version'; end if;
  if coalesce(p_object_key, '') !~ '^[A-Za-z0-9._-]{1,120}$' then raise exception 'bad_object_key'; end if;
  if coalesce(p_file_size, 0) < 1 then raise exception 'bad_size'; end if;
  if coalesce(p_sha256, '') !~ '^[0-9a-f]{64}$' then raise exception 'bad_sha256'; end if;
  if p_status not in ('published', 'offline', 'scheduled') then raise exception 'bad_status'; end if;
  if p_status = 'scheduled' and p_publish_at is null then raise exception 'bad_publish_at'; end if;
  insert into hotupdate_pkg (hot_version_code, hot_version_name, apk_min_version, apk_max_version,
                             object_key, file_size, sha256, notes, force_apply, status, publish_at)
  values (p_hot_version, coalesce(p_name, ''), coalesce(p_apk_min, 0), coalesce(p_apk_max, 2147483647),
          p_object_key, p_file_size, p_sha256, coalesce(p_notes, ''), coalesce(p_force, false), p_status,
          case when p_status = 'scheduled' then p_publish_at else null end)
  on conflict (hot_version_code) do update set
    hot_version_name = excluded.hot_version_name,
    apk_min_version  = excluded.apk_min_version,
    apk_max_version  = excluded.apk_max_version,
    object_key       = excluded.object_key,
    file_size        = excluded.file_size,
    sha256           = excluded.sha256,
    notes            = excluded.notes,
    force_apply      = excluded.force_apply,
    status           = excluded.status,
    publish_at       = excluded.publish_at,
    updated_at       = now();
  return json_build_object('ret', 0);
end $$;

create or replace function public.hotupdate_admin_set_status(
  p_hot_version int, p_status text)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  if p_status not in ('published', 'offline') then raise exception 'bad_status'; end if;
  update hotupdate_pkg set status = p_status, publish_at = null, updated_at = now()
   where hot_version_code = p_hot_version;
  if not found then raise exception 'not_found'; end if;
  return json_build_object('ret', 0);
end $$;

create or replace function public.hotupdate_admin_delete(p_hot_version int)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  delete from hotupdate_pkg where hot_version_code = p_hot_version;
  if not found then raise exception 'not_found'; end if;
  return json_build_object('ret', 0);
end $$;

create or replace function public.hotupdate_admin_logs(p_limit int default 100)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (
    select coalesce(json_agg(row_to_json(t) order by t.created_at desc), '[]'::json)
    from (select * from hotupdate_log
          order by created_at desc
          limit least(greatest(coalesce(p_limit, 100), 1), 500)) t);
end $$;

-- ===== 6) 授权 =====
-- 公开 RPC：anon + authenticated 均可执行（activate_code 同款）
grant execute on function public.hotupdate_check(int, int) to anon, authenticated;
grant execute on function public.hotupdate_report(text, int, int, int, text, text) to anon, authenticated;
-- 管理 RPC：仅 authenticated（函数内再锁 uid），收回 anon/public 默认执行权
revoke execute on function public.hotupdate_admin_list(int, int) from public, anon;
revoke execute on function public.hotupdate_admin_upsert(int, text, bigint, text, text, boolean, int, int, text, text, timestamptz) from public, anon;
revoke execute on function public.hotupdate_admin_set_status(int, text) from public, anon;
revoke execute on function public.hotupdate_admin_delete(int) from public, anon;
revoke execute on function public.hotupdate_admin_logs(int) from public, anon;
grant execute on function public.hotupdate_admin_list(int, int) to authenticated;
grant execute on function public.hotupdate_admin_upsert(int, text, bigint, text, text, boolean, int, int, text, text, timestamptz) to authenticated;
grant execute on function public.hotupdate_admin_set_status(int, text) to authenticated;
grant execute on function public.hotupdate_admin_delete(int) to authenticated;
grant execute on function public.hotupdate_admin_logs(int) to authenticated;
