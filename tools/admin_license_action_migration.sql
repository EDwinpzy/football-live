-- 后台「设为已售 / 解绑设备」两个动作的 RPC（BUG-05）。
--
-- 现象：`admin_server.py` 的 `h_unbind` / `h_set_status` 分别调用
-- `rpc("admin_unbind", …)` 与 `rpc("admin_set_status", …)`，但这两个函数
-- **从未在任何迁移里定义过**——库里只有 admin_licenses / admin_mark_sold /
-- admin_stats / admin_update_note / admin_license_v2_* 八个。
-- 于是页面上任何一次「解绑」或「改状态」都必然报：
--     Could not find the function public.admin_unbind(p_code, p_device)
--                    in the schema cache
-- 此前被登录门禁挡住不易发现；后台开免登录（OTV_ADMIN_NOAUTH=1）后才暴露。
--
-- 范式对齐既有的 admin_mark_sold：同样的 auth.uid() 守卫、同样的
-- activate_log 留痕、同样的 json_build_object 返回。

-- 设为已售 / 未售之外，还要能改卡密状态（active / banned）。
create or replace function public.admin_set_status(
    p_code text, p_status text)
    returns json
    language plpgsql security definer set search_path to public as $$
    begin
      if coalesce(auth.uid(), '') <> '2094843486709620737' then
        raise exception 'forbidden';
      end if;
      if p_status is null or p_status not in ('active', 'banned') then
        return json_build_object('ok', false, 'msg', 'bad_status');
      end if;
      update licenses set status = p_status, updated_at = now()
       where code = upper(trim(p_code));
      if not found then
        return json_build_object('ok', false, 'msg', 'not_found');
      end if;
      insert into activate_log(code, device_id, ip, result)
       values (upper(trim(p_code)), null, null, 'admin_set_' || p_status);
      return json_build_object('ok', true, 'status', p_status);
    end $$;

-- 解绑：p_device 为空时清空全部设备（换机场景），否则只摘掉指定设备。
create or replace function public.admin_unbind(
    p_code text, p_device text)
    returns json
    language plpgsql security definer set search_path to public as $$
    declare
      v_code text := upper(trim(coalesce(p_code, '')));
      v_before jsonb;
      v_after jsonb;
    begin
      if coalesce(auth.uid(), '') <> '2094843486709620737' then
        raise exception 'forbidden';
      end if;
      select devices into v_before from licenses where code = v_code for update;
      if not found then
        return json_build_object('ok', false, 'msg', 'not_found');
      end if;

      if p_device is null or btrim(p_device) = '' then
        -- 全量解绑：设备列表清空，并把已激活态一并复位，
        -- 否则卡密仍占着 max_dev 名额，用户换机后绑不上。
        v_after := '[]'::jsonb;
        update licenses
           set devices = v_after,
               activated_at = null,
               redeemed_at = null,
               redeemed_to = null,
               expire_at = null,
               updated_at = now()
         where code = v_code;
      else
        -- 定向解绑：只从 jsonb 数组里去掉这一个设备，其余保留
        -- 比较必须用 jsonb 等值：`value::text` 出来是带引号的
        -- `"dbe9…"`（实测 length 66，而裸设备串是 64），拿它跟裸串比
        -- 永远不相等，会导致「返回 ok 但设备一个没少」。
        v_after := coalesce((
          select jsonb_agg(value)
            from jsonb_array_elements(v_before) as value
           where value <> to_jsonb(btrim(p_device)::text)
        ), '[]'::jsonb);
        update licenses
           set devices = v_after,
               activated_at = case when v_after = '[]'::jsonb then null else activated_at end,
               redeemed_at = case when v_after = '[]'::jsonb then null else redeemed_at end,
               redeemed_to = case when v_after = '[]'::jsonb then null else redeemed_to end,
               expire_at = case when v_after = '[]'::jsonb then null else expire_at end,
               updated_at = now()
         where code = v_code;
      end if;

      insert into activate_log(code, device_id, ip, result)
       values (v_code, nullif(btrim(coalesce(p_device, '')), ''), null, 'admin_unbind');
      return json_build_object('ok', true, 'devices', v_after);
    end $$;

-- 授权：只给已认证角色（与既有 admin_* 一致），anon/public 一律不给。
revoke execute on function public.admin_set_status(text, text) from public, anon;
revoke execute on function public.admin_unbind(text, text) from public, anon;
grant execute on function public.admin_set_status(text, text) to authenticated;
grant execute on function public.admin_unbind(text, text) to authenticated;
