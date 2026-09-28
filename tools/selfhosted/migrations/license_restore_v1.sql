\set ON_ERROR_STOP on

-- Device restore support for the card authorization protocol.
--
-- This lives outside tools/license_v2_migration.sql on purpose. A self-hosted
-- database that predates the migration ledger already has the v2
-- activate_code(text,text,text,integer,text,text) signature installed, so the
-- migrator would treat that file as already applied and never execute it.
-- Keeping restore in its own ledger entry guarantees it runs exactly once on
-- every database, legacy or freshly bootstrapped.
--
-- The body below matches the restore branch of activate_code: it resolves the
-- active, unredeemed card already bound to the requesting device and returns
-- its ticket without changing inventory.

create or replace function public.activate_code(
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
  v_code text := case when lower(trim(coalesce(p_action, ''))) = 'restore'
                      then '' else upper(trim(coalesce(p_code, ''))) end;
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
  if v_device !~ '^[a-f0-9]{64}$'
     or v_action not in ('activate', 'verify', 'renew', 'restore') then
    insert into activate_log(code, device_id, ip, result)
      values (nullif(v_code, ''), nullif(v_device, ''), p_ip, 'bad_request');
    return json_build_object('ret', 400, 'msg', 'bad_request');
  end if;

  -- OTV-RESTORE-MARKER-BEGIN
  if v_action = 'restore' then
    select l.* into v_card
      from public.licenses l
      where l.status = 'active'
        and l.activated_at is not null
        and (l.expire_at is null or l.expire_at > v_now)
        and l.redeemed_at is null
        and l.redeemed_to is null
        and coalesce(l.devices, '[]'::jsonb) ? v_device
      order by l.activated_at desc, l.updated_at desc, l.code desc
      limit 1;
    if v_card.code is null then
      insert into activate_log(code, device_id, ip, result)
        values (null, v_device, p_ip, 'restore_not_found');
      return json_build_object('ret', 404, 'msg', 'restore_not_found');
    end if;
    insert into activate_log(code, device_id, ip, result)
      values (v_card.code, v_device, p_ip, 'restore_ok');
    return json_build_object(
      'ret', 0,
      'ticket', v_card.ticket,
      'licenseCode', v_card.code,
      'activatedAt', v_card.activated_at,
      'expireAt', v_card.expire_at,
      'serverNow', v_now
    );
  end if;
  -- OTV-RESTORE-MARKER-END

  if v_code !~ '^OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$' then
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

revoke all on function public.activate_code(text,text,text,integer,text,text) from public;
grant execute on function public.activate_code(text,text,text,integer,text,text) to anon, authenticated;
