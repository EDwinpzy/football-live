-- My TV playback-source draft and immutable published-version storage.
create extension if not exists pgcrypto;

create table if not exists vod_source_drafts (
  id uuid primary key,
  name text not null check (length(trim(name)) between 1 and 80),
  site_url text not null,
  api_url text not null default '',
  adapter_type text not null check (adapter_type in ('hhkan','macms_json','macms_xml')),
  enabled boolean not null default true,
  sort_order integer not null default 0,
  revision integer not null default 1,
  updated_at timestamptz not null default now()
);

create table if not exists vod_source_versions (
  version bigint primary key,
  config jsonb not null,
  sha256 text not null,
  published_at timestamptz not null default now()
);

insert into vod_source_drafts(id,name,site_url,api_url,adapter_type,enabled,sort_order) values
('10000000-0000-0000-0000-000000000001','好好看','https://www.haohankan.com','','hhkan',true,10),
('10000000-0000-0000-0000-000000000002','暴风','https://bfzyapi.com','https://bfzyapi.com/api.php/provide/vod/','macms_json',true,20),
('10000000-0000-0000-0000-000000000003','极速','https://jszyapi.com','https://jszyapi.com/api.php/provide/vod/','macms_json',true,30),
('10000000-0000-0000-0000-000000000004','金鹰','https://jyzyapi.com','https://jyzyapi.com/provide/vod/from/jinyingm3u8/','macms_json',true,40),
('10000000-0000-0000-0000-000000000005','虎牙','https://www.huyaapi.com','https://www.huyaapi.com/api.php/provide/vod/','macms_json',true,50),
('10000000-0000-0000-0000-000000000006','豪华','https://hhzyapi.com','https://hhzyapi.com/api.php/provide/vod/','macms_json',true,60),
('10000000-0000-0000-0000-000000000007','红牛','https://www.hongniuzy2.com','https://www.hongniuzy2.com/api.php/provide/vod/','macms_json',true,70),
('10000000-0000-0000-0000-000000000008','速播','https://subocaiji.com','https://subocaiji.com/api.php/provide/vod/','macms_json',true,80),
('10000000-0000-0000-0000-000000000009','360','https://360zy.com','https://360zy.com/api.php/provide/vod/','macms_json',true,90)
on conflict (id) do nothing;

create or replace function vod_source_admin_list()
returns jsonb language sql security definer set search_path=public as $$
  select jsonb_build_object(
    'drafts', coalesce(jsonb_agg(to_jsonb(d) order by d.sort_order), '[]'::jsonb),
    'publishedVersion', coalesce((select max(version) from vod_source_versions), 0)
  ) from vod_source_drafts d;
$$;

create or replace function vod_source_admin_save(
  p_id uuid, p_name text, p_site_url text, p_api_url text,
  p_adapter_type text, p_enabled boolean, p_sort_order integer default 0)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_id uuid := coalesce(p_id, gen_random_uuid());
begin
  if p_site_url !~ '^https?://' or p_api_url !~ '^$|^https?://' then
    raise exception 'invalid source url';
  end if;
  insert into vod_source_drafts(id,name,site_url,api_url,adapter_type,enabled,sort_order)
  values(v_id,trim(p_name),p_site_url,p_api_url,p_adapter_type,coalesce(p_enabled,true),coalesce(p_sort_order,0))
  on conflict(id) do update set name=excluded.name, site_url=excluded.site_url,
    api_url=excluded.api_url, adapter_type=excluded.adapter_type, enabled=excluded.enabled,
    sort_order=excluded.sort_order, revision=vod_source_drafts.revision+1, updated_at=now();
  return jsonb_build_object('ok',true,'id',v_id);
end;
$$;

create or replace function vod_source_admin_set_enabled(p_id uuid, p_enabled boolean)
returns jsonb language plpgsql security definer set search_path=public as $$
begin
  if not exists(select 1 from vod_source_drafts where id=p_id) then
    raise exception '影视源不存在';
  end if;
  if not coalesce(p_enabled,false)
     and (select count(*) from vod_source_drafts where enabled) <= 1 then
    raise exception '至少需要一个启用中的影视源';
  end if;
  update vod_source_drafts
     set enabled=coalesce(p_enabled,false), revision=revision+1, updated_at=now()
   where id=p_id;
  return jsonb_build_object('ok',true,'id',p_id,'enabled',coalesce(p_enabled,false));
end;
$$;

create or replace function vod_source_admin_set_priority(p_id uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
begin
  if not exists(select 1 from vod_source_drafts where id=p_id) then
    raise exception '影视源不存在';
  end if;
  if not exists(select 1 from vod_source_drafts where id=p_id and enabled) then
    raise exception '停用中的影视源不能设为默认优先';
  end if;
  with ordered as (
    select id, (row_number() over(order by (id=p_id) desc, sort_order, id)-1)::integer as next_sort
      from vod_source_drafts
  )
  update vod_source_drafts d
     set sort_order=o.next_sort, revision=d.revision+1, updated_at=now()
    from ordered o
   where d.id=o.id and d.sort_order is distinct from o.next_sort;
  return jsonb_build_object('ok',true,'id',p_id);
end;
$$;

create or replace function vod_source_admin_delete(p_id uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
begin
  delete from vod_source_drafts where id=p_id;
  return jsonb_build_object('ok',found);
end;
$$;

create or replace function vod_source_admin_publish()
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_version bigint; v_config jsonb; v_sha256 text;
begin
  -- Serialize publishers. FOR UPDATE also makes the last immutable version the lock anchor.
  perform version from vod_source_versions order by version desc limit 1 for update;
  lock table vod_source_versions in share row exclusive mode;
  if not exists(select 1 from vod_source_drafts where enabled) then
    raise exception 'at least one enabled source is required';
  end if;
  select coalesce(max(version),0)+1 into v_version from vod_source_versions;
  select jsonb_build_object('schema',1,'version',v_version,'sources',
    jsonb_agg(jsonb_build_object('id',id,'name',name,'site_url',site_url,'api_url',api_url,
      'adapter_type',adapter_type,'enabled',enabled,'revision',revision) order by sort_order))
    into v_config from vod_source_drafts;
  v_sha256 := encode(digest(convert_to(v_config::text,'UTF8'),'sha256'),'hex');
  insert into vod_source_versions(version,config,sha256) values(v_version,v_config,v_sha256);
  return jsonb_build_object('ok',true,'version',v_version,'sha256',v_sha256,'config',v_config);
end;
$$;

create or replace function vod_source_public_check(p_version bigint default 0)
returns jsonb language sql security definer set search_path=public as $$
  with latest as (select version,config,sha256 from vod_source_versions order by version desc limit 1)
  select case when not exists(select 1 from latest) then
    jsonb_build_object('ret',0,'hasUpdate',false,'version',0)
  else (select jsonb_build_object('ret',0,'hasUpdate',version<>coalesce(p_version,0),
    'version',version,'sha256',sha256,'config',case when version<>coalesce(p_version,0) then config else null end) from latest)
  end;
$$;

revoke all on vod_source_drafts, vod_source_versions from anon, authenticated;
revoke all on function vod_source_admin_list() from public;
revoke all on function vod_source_admin_save(uuid,text,text,text,text,boolean,integer) from public;
revoke all on function vod_source_admin_set_enabled(uuid,boolean) from public;
revoke all on function vod_source_admin_set_priority(uuid) from public;
revoke all on function vod_source_admin_delete(uuid) from public;
revoke all on function vod_source_admin_publish() from public;
grant execute on function vod_source_public_check(bigint) to anon, authenticated;
