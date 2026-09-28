-- OptimalTV v1.20 后台动态更新电视直播源（需求 后台#8）PG 迁移
-- 通过 managePgDatabase(applyMigration) 执行。
--
-- 组成：
--   1) 表 iptv_channel / iptv_line / iptv_version / iptv_admin_log
--     （RLS 关闭，沿用 licenses/hotupdate_pkg 约定：全部访问收口 SECURITY DEFINER RPC）
--   2) 管理 RPC：iptv_admin_*（函数内 auth.uid() 白名单 = svc_import 2094843486709620737）
--
-- 发布链路：admin 序列化 DB → m3u 上传 pgstore 公开桶 hotupdate 的
--   iptv/iptv-v{n}.m3u + iptv/iptv-latest.m3u（客户端 SOURCES 首位自动拉取，
--   ≤30 分钟 STALE 节流或下次冷启动生效 = 客户端自动更新）。
-- 托管模式：管理端发布的 m3u 携带非空 group-title → 客户端整表生效（频道/分组/
--   顺序全部以管理端为准）；每日 iptv-rebuild 云函数产物 group-title 为空 →
--   维持旧「精编名单 + 线路池」合并语义。互不干扰。

-- ===== 1) 表 =====
create table if not exists public.iptv_channel (
    id          bigint generated always as identity primary key,
    name        text not null,                       -- 频道名（展示名，含 CCTV-1 等规格）
    group_name  text not null default '其他',         -- 分组（央视/卫视/体育/地方/数字付费/港澳台/美国/英国…）
    logo        text not null default '',            -- 台标 URL（可空 = 客户端内置台标兜底）
    sort_order  int  not null default 0,             -- 组内排序（小在前）
    enabled     boolean not null default true,       -- 停用 = 不进发布产物
    note        text not null default '',
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists idx_iptv_channel_sort on public.iptv_channel (group_name, sort_order, id);

create table if not exists public.iptv_line (
    id          bigint generated always as identity primary key,
    channel_id  bigint not null references public.iptv_channel(id) on delete cascade,
    url         text not null,
    sort_order  int  not null default 0,             -- 线路顺序 = 主备线顺序（小在前）
    enabled     boolean not null default true,
    note        text not null default '',
    created_at  timestamptz not null default now()
);
create index if not exists idx_iptv_line_channel on public.iptv_line (channel_id, sort_order, id);

create table if not exists public.iptv_version (
    id           bigint generated always as identity primary key,
    version      int not null unique,                -- 发布版本号（单调递增）
    object_key   text not null default '',           -- 桶内归档对象 iptv/iptv-v{n}.m3u
    file_size    bigint not null default 0,
    sha256       text not null default '',
    channels     int not null default 0,             -- 频道数（发布时统计）
    lines        int not null default 0,             -- 线路数（发布时统计）
    notes        text not null default '',
    status       text not null default 'pending' check (status in ('pending', 'published', 'offline', 'failed')),
    created_at   timestamptz not null default now()
);

create table if not exists public.iptv_admin_log (
    id          bigint generated always as identity primary key,
    action      text not null,                       -- import / save / delete / batch / publish / rollback / sync / mode
    detail      text not null default '',
    created_at  timestamptz not null default now()
);
create index if not exists idx_iptv_admin_log_created on public.iptv_admin_log (created_at desc);

-- ===== 2) 管理 RPC（svc_import 白名单）=====

-- 运营日志（内部）
create or replace function public.iptv_log(p_action text, p_detail text default '')
returns void language plpgsql security definer set search_path to public as $$
begin
    insert into iptv_admin_log (action, detail) values (left(p_action, 40), left(p_detail, 800));
end $$;

-- 频道全量列表（含线路），按分组+排序返回
create or replace function public.iptv_admin_list()
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (
    select json_build_object(
      'channels', coalesce((
        select json_agg(json_build_object(
            'id', c.id, 'name', c.name, 'group', c.group_name, 'logo', c.logo,
            'sort', c.sort_order, 'enabled', c.enabled, 'note', c.note,
            'lines', coalesce((
              select json_agg(json_build_object(
                  'id', l.id, 'url', l.url, 'sort', l.sort_order,
                  'enabled', l.enabled, 'note', l.note)
                order by l.sort_order, l.id)
              from iptv_line l where l.channel_id = c.id), '[]'::json))
          order by c.group_name, c.sort_order, c.id)
        from iptv_channel c), '[]'::json),
      'latest', (
        select json_build_object('version', v.version, 'channels', v.channels, 'lines', v.lines,
                                 'notes', v.notes, 'status', v.status, 'createdAt', v.created_at)
          from iptv_version v where v.status = 'published'
          order by v.version desc limit 1)));
end $$;

-- 新增/编辑频道（p_id 空 = 新增）。p_lines = [{url, sort?, enabled?, note?}]，整组替换。
create or replace function public.iptv_admin_upsert_channel(
    p_id bigint default null, p_name text default '', p_group text default '其他', p_logo text default '',
    p_sort int default 0, p_enabled boolean default true, p_note text default '',
    p_lines jsonb default '[]')
returns json
language plpgsql security definer set search_path to public as $$
declare v_id bigint; v_cnt int;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  if coalesce(p_name, '') = '' or length(p_name) > 60 then raise exception 'bad_name'; end if;
  if coalesce(p_group, '') = '' then raise exception 'bad_group'; end if;
  if p_id is null and (p_lines is null or jsonb_array_length(p_lines) = 0) then
    raise exception 'no_lines';
  end if;

  if p_id is null then
    insert into iptv_channel (name, group_name, logo, sort_order, enabled, note)
    values (p_name, p_group, coalesce(p_logo, ''), coalesce(p_sort, 0), coalesce(p_enabled, true), coalesce(p_note, ''))
    returning id into v_id;
  else
    update iptv_channel set name = p_name, group_name = p_group, logo = coalesce(p_logo, ''),
        sort_order = coalesce(p_sort, 0), enabled = coalesce(p_enabled, true),
        note = coalesce(p_note, ''), updated_at = now()
     where id = p_id
    returning id into v_id;
    if v_id is null then raise exception 'not_found'; end if;
    delete from iptv_line where channel_id = v_id;
  end if;

  select count(*) into v_cnt from jsonb_array_elements(p_lines) e;
  insert into iptv_line (channel_id, url, sort_order, enabled, note)
  select v_id,
         e->>'url',
         coalesce((e->>'sort')::int, 0),
         coalesce((e->>'enabled')::boolean, true),
         coalesce(e->>'note', '')
    from jsonb_array_elements(p_lines) e
   where (e->>'url') ~ '^https?://' and length(coalesce(e->>'url', '')) between 5 and 500;
  perform iptv_log('save', format('%s %s（%s 线）', case when p_id is null then '新增' else '编辑' end,
                                   p_name, coalesce((select count(*) from iptv_line where channel_id = v_id), 0)));
  return json_build_object('ret', 0, 'id', v_id);
end $$;

-- 删除频道（连带线路）
create or replace function public.iptv_admin_delete_channel(p_id bigint)
returns json
language plpgsql security definer set search_path to public as $$
declare v_name text;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  delete from iptv_channel where id = p_id returning name into v_name;
  if v_name is null then raise exception 'not_found'; end if;
  perform iptv_log('delete', v_name);
  return json_build_object('ret', 0);
end $$;

-- 批量操作：p_ops = [{op: 'enable'|'disable'|'delete'|'move', id, sort?, delta?}]
create or replace function public.iptv_admin_batch(p_ops jsonb)
returns json
language plpgsql security definer set search_path to public as $$
declare e jsonb; op text; cid bigint; n int := 0;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  for e in select * from jsonb_array_elements(coalesce(p_ops, '[]'::jsonb)) loop
    op := e->>'op'; cid := nullif(e->>'id', '')::bigint;
    if cid is null then continue; end if;
    if op = 'enable' then
      update iptv_channel set enabled = true, updated_at = now() where id = cid; n := n + 1;
    elsif op = 'disable' then
      update iptv_channel set enabled = false, updated_at = now() where id = cid; n := n + 1;
    elsif op = 'delete' then
      delete from iptv_channel where id = cid; n := n + 1;
    elsif op = 'move' then
      if e ? 'sort' then
        update iptv_channel set sort_order = greatest((e->>'sort')::int, 0), updated_at = now() where id = cid; n := n + 1;
      elsif e ? 'delta' then
        update iptv_channel set sort_order = greatest(sort_order + (e->>'delta')::int, 0), updated_at = now()
         where id = cid; n := n + 1;
      end if;
    end if;
  end loop;
  perform iptv_log('batch', format('%s 项', n));
  return json_build_object('ret', 0, 'affected', n);
end $$;

-- 整表导入（replace = 清空后导入；merge = 按频道名并集，管理端同名覆盖/新名追加）
-- p_channels = [{name, group, logo?, lines: [url 或 {url, note}]}]
create or replace function public.iptv_admin_import(p_channels jsonb, p_mode text default 'merge')
returns json
language plpgsql security definer set search_path to public as $$
declare e jsonb; ch jsonb; v_id bigint; u text; li jsonb; added int := 0; updated int := 0; lines_n int := 0;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  if p_mode not in ('replace', 'merge') then raise exception 'bad_mode'; end if;
  if p_channels is null or jsonb_array_length(p_channels) = 0 then raise exception 'empty'; end if;
  if jsonb_array_length(p_channels) > 3000 then raise exception 'too_many'; end if;

  if p_mode = 'replace' then
    delete from iptv_channel;
  end if;

  for ch in select * from jsonb_array_elements(p_channels) loop
    if coalesce(ch->>'name', '') = '' then continue; end if;
    select id into v_id from iptv_channel where name = ch->>'name' limit 1;
    if v_id is null then
      insert into iptv_channel (name, group_name, logo, sort_order, enabled)
      values (ch->>'name', coalesce(nullif(ch->>'group', ''), '其他'), coalesce(ch->>'logo', ''), 0, true)
      returning id into v_id;
      added := added + 1;
    else
      update iptv_channel set group_name = coalesce(nullif(ch->>'group', ''), group_name),
          logo = coalesce(nullif(ch->>'logo', ''), logo), updated_at = now()
       where id = v_id;
      updated := updated + 1;
    end if;
    if p_mode = 'replace' then
      -- replace 模式整组替换线路
      delete from iptv_line where channel_id = v_id;
    end if;
    for li in select * from jsonb_array_elements(coalesce(ch->'lines', '[]'::jsonb)) loop
      u := case when jsonb_typeof(li) = 'string' then li #>> '{}' else li->>'url' end;
      if u ~ '^https?://' and length(coalesce(u, '')) between 5 and 500 then
        insert into iptv_line (channel_id, url, sort_order, enabled, note)
        values (v_id, u, 0, true, coalesce(li->>'note', ''));
        lines_n := lines_n + 1;
      end if;
    end loop;
  end loop;

  perform iptv_log('import', format('%s 模式：新增 %s 更新 %s 线路 %s', p_mode, added, updated, lines_n));
  return json_build_object('ret', 0, 'added', added, 'updated', updated, 'lines', lines_n);
end $$;

-- 导出（发布序列化用）：启用频道 + 启用线路
create or replace function public.iptv_admin_export()
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (
    select json_build_object('channels', coalesce((
      select json_agg(t.j order by t.g, t.sort, t.id) from (
        select json_build_object(
                 'name', c.name, 'group', c.group_name, 'logo', c.logo, 'lines',
                 coalesce((select json_agg(l.url order by l.sort_order, l.id)
                             from iptv_line l where l.channel_id = c.id and l.enabled), '[]'::json)
               ) as j, c.group_name as g, c.sort_order as sort, c.id as id
        from iptv_channel c where c.enabled
      ) t
    ), '[]'::json))
  );
end $$;

-- 发布登记：预占版本号（pending），上传完成后 confirm
create or replace function public.iptv_admin_publish_begin(p_notes text default '')
returns json
language plpgsql security definer set search_path to public as $$
declare v int;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  select coalesce(max(version), 0) + 1 into v from iptv_version;
  insert into iptv_version (version, notes, status) values (v, coalesce(p_notes, ''), 'pending');
  return json_build_object('ret', 0, 'version', v);
end $$;

-- 发布确认：写元数据 + 旧 published 转 offline（latest 唯一）
create or replace function public.iptv_admin_publish_confirm(
    p_version int, p_object_key text, p_sha256 text, p_file_size bigint,
    p_channels int, p_lines int, p_status text default 'published')
returns json
language plpgsql security definer set search_path to public as $$
declare v_notes text;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  if p_status not in ('published', 'offline', 'failed') then raise exception 'bad_status'; end if;
  select notes into v_notes from iptv_version where version = p_version;
  if v_notes is null then raise exception 'not_found'; end if;
  update iptv_version set status = 'offline' where status = 'published' and version <> p_version;
  update iptv_version set object_key = coalesce(p_object_key, ''), sha256 = coalesce(p_sha256, ''),
      file_size = coalesce(p_file_size, 0), channels = coalesce(p_channels, 0),
      lines = coalesce(p_lines, 0), status = p_status
   where version = p_version;
  perform iptv_log('publish', format('v%s（%s 频道 %s 线）%s', p_version, p_channels, p_lines, v_notes));
  return json_build_object('ret', 0);
end $$;

-- 版本列表
create or replace function public.iptv_admin_versions(p_limit int default 50)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (
    select coalesce(json_agg(row_to_json(t) order by t.version desc), '[]'::json)
    from (select version, object_key, file_size, sha256, channels, lines, notes, status, created_at
          from iptv_version
          order by version desc
          limit least(greatest(coalesce(p_limit, 50), 1), 200)) t);
end $$;

-- 回滚登记：克隆目标版本元数据为新 pending 版本（对象由管理端复制到 latest 后 confirm）
create or replace function public.iptv_admin_rollback_begin(p_target_version int)
returns json
language plpgsql security definer set search_path to public as $$
declare src iptv_version%rowtype; v int;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  select * into src from iptv_version where version = p_target_version;
  if src.id is null then raise exception 'not_found'; end if;
  select coalesce(max(version), 0) + 1 into v from iptv_version;
  insert into iptv_version (version, object_key, file_size, sha256, channels, lines, notes, status)
  values (v, src.object_key, src.file_size, src.sha256, src.channels, src.lines,
          format('回滚自 v%s（%s）', p_target_version, src.notes), 'pending');
  perform iptv_log('rollback', format('v%s ← v%s', v, p_target_version));
  return json_build_object('ret', 0, 'version', v);
end $$;

-- 操作日志
create or replace function public.iptv_admin_logs(p_limit int default 200)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (
    select coalesce(json_agg(row_to_json(t) order by t.id desc), '[]'::json)
    from (select id, action, detail, created_at from iptv_admin_log
          order by id desc limit least(greatest(coalesce(p_limit, 200), 1), 500)) t);
end $$;

-- 运行模式（auto = 每日 iptv-rebuild 云函数自动推流；manual = 仅管理端手动发布生效）
create table if not exists public.iptv_config (
    key text primary key,
    value text not null default '',
    updated_at timestamptz not null default now()
);
insert into public.iptv_config (key, value) values ('mode', 'auto')
on conflict (key) do nothing;

create or replace function public.iptv_admin_get_mode()
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (select json_build_object('ret', 0, 'mode', value) from iptv_config where key = 'mode');
end $$;

create or replace function public.iptv_admin_set_mode(p_mode text)
returns json
language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  if p_mode not in ('auto', 'manual') then raise exception 'bad_mode'; end if;
  insert into iptv_config (key, value, updated_at) values ('mode', p_mode, now())
  on conflict (key) do update set value = excluded.value, updated_at = now();
  perform iptv_log('mode', p_mode);
  return json_build_object('ret', 0, 'mode', p_mode);
end $$;

-- ===== 3) 频道分组管理（2026-09-05 需求③：新建/重命名/删除/排序分组）=====
-- 与生产库实际部署版本一致（id bigserial、排序列名 sort；「未分组」为保留兜底组）。
create table if not exists public.iptv_group (
    id bigserial primary key,
    name text not null unique,
    sort int not null default 0,
    created_at timestamptz not null default now()
);

-- 存量频道分组回填
insert into public.iptv_group (name, sort)
select group_name, (row_number() over (order by min(sort_order), min(id)) - 1)::int
from public.iptv_channel
where coalesce(group_name, '') <> ''
group by group_name
on conflict (name) do nothing;

create or replace function public.iptv_admin_group_list()
returns json language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return (
    select coalesce(json_agg(row_to_json(t) order by t.sort, t.id), '[]'::json)
    from (select g.id, g.name, g.sort,
                 (select count(*) from iptv_channel c where c.group_name = g.name) as channels,
                 (select count(*) from iptv_channel c where c.group_name = g.name and c.enabled) as enabled
            from iptv_group g) t);
end $$;

create or replace function public.iptv_admin_group_create(p_name text)
returns json language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  p_name := btrim(coalesce(p_name, ''));
  if p_name = '' or length(p_name) > 24 or p_name ~ '[
]' then raise exception 'bad_name'; end if;
  if p_name = '未分组' then raise exception 'reserved'; end if;
  insert into iptv_group(name) values (p_name) on conflict (name) do nothing;
  perform iptv_log('group_create', p_name);
  return json_build_object('ret', 0, 'name', p_name);
end $$;

create or replace function public.iptv_admin_group_rename(p_id bigint, p_name text)
returns json language plpgsql security definer set search_path to public as $$
declare v_old text;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  p_name := btrim(coalesce(p_name, ''));
  if p_name = '' or length(p_name) > 24 or p_name ~ '[
]' then raise exception 'bad_name'; end if;
  if p_name = '未分组' then raise exception 'reserved'; end if;
  select name into v_old from iptv_group where id = p_id;
  if v_old is null then raise exception 'not_found'; end if;
  if v_old = '未分组' then raise exception 'reserved'; end if;
  update iptv_group set name = p_name where id = p_id;
  update iptv_channel set group_name = p_name, updated_at = now() where group_name = v_old;
  perform iptv_log('group_rename', format('%s → %s', v_old, p_name));
  return json_build_object('ret', 0, 'name', p_name);
end $$;

create or replace function public.iptv_admin_group_delete(p_id bigint, p_move_to text)
returns json language plpgsql security definer set search_path to public as $$
declare v_name text; v_cnt int; v_target text;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  select name into v_name from iptv_group where id = p_id;
  if v_name is null then raise exception 'not_found'; end if;
  v_target := btrim(coalesce(p_move_to, '未分组'));
  if v_target = '' then v_target := '未分组'; end if;
  if v_target = v_name then raise exception 'bad_move_to'; end if;
  select count(*) into v_cnt from iptv_channel where group_name = v_name;
  if v_cnt > 0 then
    if v_target <> '未分组' and not exists (select 1 from iptv_group where name = v_target) then
      raise exception 'bad_move_to';
    end if;
    if v_target = '未分组' and not exists (select 1 from iptv_group where name = '未分组') then
      insert into iptv_group(name) values ('未分组');
    end if;
    update iptv_channel set group_name = v_target, updated_at = now() where group_name = v_name;
  end if;
  delete from iptv_group where id = p_id;
  perform iptv_log('group_delete', format('%s（%s 个频道移入 %s）', v_name, v_cnt, v_target));
  return json_build_object('ret', 0, 'moved', v_cnt, 'move_to', v_target);
end $$;

create or replace function public.iptv_admin_group_move(p_id bigint, p_delta int)
returns json language plpgsql security definer set search_path to public as $$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  update iptv_group set sort = greatest(sort + coalesce(p_delta, 0), 0) where id = p_id;
  if not found then raise exception 'not_found'; end if;
  return json_build_object('ret', 0);
end $$;

-- PostgREST rejects an RPC call with "permission denied for function ..." when
-- the connecting role lacks EXECUTE, before the function body (and its
-- auth.uid() whitelist) ever runs. These grants are therefore required for the
-- console's TV-source pages; without them every iptv_admin_* call fails.
-- Signatures are the identity forms reported by pg_get_function_identity_arguments.
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
  public.iptv_admin_publish_confirm(integer, text, text, bigint, integer, integer, text)
  to authenticated;

-- Internal audit trail written by the functions above; not called over HTTP.
grant execute on function public.iptv_log(text, text) to authenticated;

notify pgrst, 'reload schema';
