-- My TV card authorization protocol v2.
-- Idempotent schema/RPC migration. Deliberately contains no inventory deletion.

alter table public.licenses add column if not exists activated_at timestamptz;
alter table public.licenses add column if not exists expire_at timestamptz;
alter table public.licenses add column if not exists redeemed_at timestamptz;
alter table public.licenses add column if not exists redeemed_to text;

create index if not exists idx_licenses_expire_at on public.licenses(expire_at);
create index if not exists idx_licenses_redeemed_to on public.licenses(redeemed_to);

drop function if exists public.activate_code(text, text, text);
drop function if exists public.activate_code(text, text, text, integer, text, text);

create function public.activate_code(
  p_code text,
  p_device text,
  p_ip text default null,
  p_protocol integer default null,
  p_action text default null,
  p_current_code text default null
) returns json
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  v_code text := upper(trim(coalesce(p_code, '')));
  v_device text := lower(trim(coalesce(p_device, '')));
  v_action text := lower(trim(coalesce(p_action, '')));
  v_current_code text := upper(trim(coalesce(p_current_code, '')));
  v_now timestamptz := clock_timestamp();
  v_card public.licenses%rowtype;
  v_current public.licenses%rowtype;
  v_renew public.licenses%rowtype;
  v_new_expire timestamptz;
begin
  if coalesce(p_protocol, 0) <> 2 then
    insert into activate_log(code, device_id, ip, result)
      values (nullif(v_code, ''), nullif(v_device, ''), p_ip, 'protocol_upgrade_required');
    return json_build_object('ret', 426, 'msg', 'upgrade_required');
  end if;
  if v_code !~ '^OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$'
     or v_device !~ '^[a-f0-9]{64}$'
     or v_action not in ('activate', 'verify', 'renew') then
    insert into activate_log(code, device_id, ip, result)
      values (nullif(v_code, ''), nullif(v_device, ''), p_ip, 'bad_request');
    return json_build_object('ret', 400, 'msg', 'bad_request');
  end if;

  if v_action = 'renew' then
    if v_current_code !~ '^OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$'
       or v_current_code = v_code then
      return json_build_object('ret', 400, 'msg', 'bad_current_code');
    end if;

    -- Take both row locks in a deterministic order to avoid renewal deadlocks.
    perform code from public.licenses
      where code in (v_code, v_current_code)
      order by code for update;
    select * into v_current from public.licenses where code = v_current_code;
    select * into v_renew from public.licenses where code = v_code;

    if v_current.code is null or v_renew.code is null then
      return json_build_object('ret', 404, 'msg', 'not_found');
    end if;
    if v_current.status = 'banned' or v_renew.status = 'banned' then
      return json_build_object('ret', 403, 'msg', 'banned');
    end if;
    if v_current.activated_at is null or not (coalesce(v_current.devices, '[]'::jsonb) ? v_device) then
      return json_build_object('ret', 406, 'msg', 'device_unbound');
    end if;
    if v_renew.redeemed_at is not null or v_renew.redeemed_to is not null then
      return json_build_object('ret', 409, 'msg', 'already_redeemed');
    end if;
    if v_renew.activated_at is not null or jsonb_array_length(coalesce(v_renew.devices, '[]'::jsonb)) > 0 then
      return json_build_object('ret', 409, 'msg', 'renewal_card_already_used');
    end if;
    if v_current.expire_at is null then
      return json_build_object('ret', 410, 'msg', 'already_lifetime');
    end if;

    if v_renew.days = 0 then
      v_new_expire := null;
    else
      v_new_expire := greatest(v_current.expire_at, v_now) + make_interval(days => v_renew.days);
    end if;
    update public.licenses
      set expire_at = v_new_expire, updated_at = v_now
      where code = v_current_code
      returning * into v_current;
    update public.licenses
      set redeemed_at = v_now, redeemed_to = v_current_code, updated_at = v_now
      where code = v_code;
    insert into activate_log(code, device_id, ip, result)
      values (v_current_code, v_device, p_ip, 'renew_ok:' || v_code);
    return json_build_object(
      'ret', 0,
      'ticket', v_current.ticket,
      'licenseCode', v_current.code,
      'activatedAt', v_current.activated_at,
      'expireAt', v_current.expire_at,
      'serverNow', v_now
    );
  end if;

  select * into v_card from public.licenses where code = v_code for update;
  if v_card.code is null then
    insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'not_found');
    return json_build_object('ret', 404, 'msg', 'not_found');
  end if;
  if v_card.status = 'banned' then
    insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'banned');
    return json_build_object('ret', 403, 'msg', 'banned');
  end if;
  if v_card.redeemed_at is not null or v_card.redeemed_to is not null then
    insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'already_redeemed');
    return json_build_object('ret', 409, 'msg', 'already_redeemed');
  end if;

  if v_action = 'verify' then
    if v_card.activated_at is null then
      insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'not_activated');
      return json_build_object('ret', 404, 'msg', 'not_activated');
    end if;
    if v_card.expire_at is not null and v_card.expire_at <= v_now then
      insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'expired');
      return json_build_object('ret', 405, 'msg', 'expired', 'expireAt', v_card.expire_at, 'serverNow', v_now);
    end if;
    if not (coalesce(v_card.devices, '[]'::jsonb) ? v_device) then
      insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'device_unbound');
      return json_build_object('ret', 406, 'msg', 'device_unbound');
    end if;
    insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'verify_ok');
    return json_build_object(
      'ret', 0, 'ticket', v_card.ticket, 'licenseCode', v_card.code,
      'activatedAt', v_card.activated_at, 'expireAt', v_card.expire_at, 'serverNow', v_now
    );
  elsif v_action = 'renew' then
    return json_build_object('ret', 400, 'msg', 'bad_renew_request');
  end if;

  -- Explicit activation may bind a new device, but expiry is initialized only once.
  if v_card.activated_at is null then
    v_card.activated_at := v_now;
    v_card.expire_at := case when v_card.days = 0 then null
                             else v_now + make_interval(days => v_card.days) end;
  elsif v_card.expire_at is not null and v_card.expire_at <= v_now then
    insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'expired');
    return json_build_object('ret', 405, 'msg', 'expired', 'expireAt', v_card.expire_at, 'serverNow', v_now);
  end if;

  if not (coalesce(v_card.devices, '[]'::jsonb) ? v_device) then
    if jsonb_array_length(coalesce(v_card.devices, '[]'::jsonb)) >= coalesce(v_card.max_dev, 2) then
      insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'device_full');
      return json_build_object('ret', 402, 'msg', 'device_full');
    end if;
    v_card.devices := coalesce(v_card.devices, '[]'::jsonb) || jsonb_build_array(v_device);
  end if;
  update public.licenses
    set activated_at = v_card.activated_at,
        expire_at = v_card.expire_at,
        devices = v_card.devices,
        sold_at = coalesce(sold_at, v_now)
    where code = v_code
    returning * into v_card;
  insert into activate_log(code, device_id, ip, result) values (v_code, v_device, p_ip, 'activate_ok');
  return json_build_object(
    'ret', 0, 'ticket', v_card.ticket, 'licenseCode', v_card.code,
    'activatedAt', v_card.activated_at, 'expireAt', v_card.expire_at, 'serverNow', v_now
  );
end;
$function$;

-- Admin listing keeps the existing signature and adds v2 fields/status.
create or replace function public.admin_licenses(
  p_q text default null,
  p_plan text default null,
  p_status text default null,
  p_limit integer default 50,
  p_offset integer default 0,
  p_sort text default 'updated_at',
  p_dir text default 'desc'
) returns json
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  v_total integer;
  v_rows json;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  select count(*) into v_total from public.licenses l
   where (coalesce(p_q, '') = '' or l.code ilike '%' || upper(p_q) || '%')
     and (coalesce(p_plan, '') = '' or l.plan = p_plan)
     and (coalesce(p_status, '') = ''
       or (p_status = 'active' and l.status = 'active' and l.activated_at is not null and l.redeemed_at is null)
       or (p_status = 'redeemed' and l.redeemed_at is not null)
       or (p_status = 'sold' and l.status = 'active' and l.activated_at is null and l.sold_at is not null and l.redeemed_at is null)
       or (p_status = 'unsold' and l.status = 'active' and l.activated_at is null and l.sold_at is null and l.redeemed_at is null)
       or (p_status = 'banned' and l.status = 'banned'));
  select coalesce(json_agg(to_jsonb(x)), '[]'::json) into v_rows
    from (
      select l.code, l.plan, l.days, l.status, l.max_dev, l.devices, l.note,
             l.sold_at, l.created_at, l.updated_at,
             l.activated_at, l.expire_at, l.redeemed_at, l.redeemed_to
      from public.licenses l
      where (coalesce(p_q, '') = '' or l.code ilike '%' || upper(p_q) || '%')
        and (coalesce(p_plan, '') = '' or l.plan = p_plan)
        and (coalesce(p_status, '') = ''
          or (p_status = 'active' and l.status = 'active' and l.activated_at is not null and l.redeemed_at is null)
          or (p_status = 'redeemed' and l.redeemed_at is not null)
          or (p_status = 'sold' and l.status = 'active' and l.activated_at is null and l.sold_at is not null and l.redeemed_at is null)
          or (p_status = 'unsold' and l.status = 'active' and l.activated_at is null and l.sold_at is null and l.redeemed_at is null)
          or (p_status = 'banned' and l.status = 'banned'))
      order by
        case when p_dir = 'asc' and p_sort = 'code' then l.code end asc nulls last,
        case when p_dir <> 'asc' and p_sort = 'code' then l.code end desc nulls last,
        case when p_dir = 'asc' and p_sort = 'plan' then l.plan end asc nulls last,
        case when p_dir <> 'asc' and p_sort = 'plan' then l.plan end desc nulls last,
        case when p_dir = 'asc' and p_sort = 'status' then l.status end asc nulls last,
        case when p_dir <> 'asc' and p_sort = 'status' then l.status end desc nulls last,
        case when p_dir = 'asc' and p_sort = 'devices' then jsonb_array_length(coalesce(l.devices, '[]'::jsonb)) end asc nulls last,
        case when p_dir <> 'asc' and p_sort = 'devices' then jsonb_array_length(coalesce(l.devices, '[]'::jsonb)) end desc nulls last,
        case when p_dir = 'asc' then case p_sort
          when 'sold_at' then l.sold_at when 'created_at' then l.created_at
          when 'updated_at' then l.updated_at when 'activated_at' then l.activated_at
          when 'expire_at' then l.expire_at when 'redeemed_at' then l.redeemed_at end
        end asc nulls last,
        case when p_dir <> 'asc' then case p_sort
          when 'sold_at' then l.sold_at when 'created_at' then l.created_at
          when 'updated_at' then l.updated_at when 'activated_at' then l.activated_at
          when 'expire_at' then l.expire_at when 'redeemed_at' then l.redeemed_at end
        end desc nulls last,
        l.updated_at desc
      limit least(coalesce(p_limit, 50), 200) offset greatest(coalesce(p_offset, 0), 0)
    ) x;
  return json_build_object('total', v_total, 'rows', v_rows);
end;
$function$;

-- Full local backup export, restricted to the existing admin service UID.
create or replace function public.admin_license_v2_export(p_limit integer default 200, p_offset integer default 0)
returns json language plpgsql security definer set search_path to 'public'
as $function$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return json_build_object(
    'total', (select count(*) from public.licenses),
    'rows', (select coalesce(json_agg(to_jsonb(x)), '[]'::json) from (
      select * from public.licenses order by created_at, code
      limit least(coalesce(p_limit, 200), 500) offset greatest(coalesce(p_offset, 0), 0)
    ) x)
  );
end;
$function$;

create or replace function public.admin_license_v2_log_export(p_limit integer default 200, p_offset integer default 0)
returns json language plpgsql security definer set search_path to 'public'
as $function$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then raise exception 'forbidden'; end if;
  return json_build_object(
    'total', (select count(*) from public.activate_log),
    'rows', (select coalesce(json_agg(to_jsonb(x)), '[]'::json) from (
      select * from public.activate_log order by at, code
      limit least(coalesce(p_limit, 200), 500) offset greatest(coalesce(p_offset, 0), 0)
    ) x)
  );
end;
$function$;

revoke all on function public.activate_code(text,text,text,integer,text,text) from public;
grant execute on function public.activate_code(text,text,text,integer,text,text) to anon, authenticated;

revoke all on function public.admin_license_v2_export(integer,integer) from public, anon;
revoke all on function public.admin_license_v2_log_export(integer,integer) from public, anon;
grant execute on function public.admin_license_v2_export(integer,integer) to authenticated;
grant execute on function public.admin_license_v2_log_export(integer,integer) to authenticated;
