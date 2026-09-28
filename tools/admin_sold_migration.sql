-- 后台管理系统二期（2026-09-02）：卡密「已售」维度 + 品牌/界面扩展
-- 全部语句幂等，可重复执行（已在线上应用，存档备重建/复跑）。
-- 注意：CloudBase managePgDatabase applyMigration 会静默失败，须用 execute 分块逐条执行
-- 并用只读查询验证对象存在（见 docs/卡密付费-方案与需求.md §12.8）。

-- 1) 已售时间戳：NULL = 未售库存；非 NULL = 已标记出售（手动，激活路径不写此字段）
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS sold_at timestamptz;

-- 2) 新增 RPC：标记/取消出售（SECURITY DEFINER + svc_import uid 白名单，写 admin_sold/admin_unsold 审计）
CREATE OR REPLACE FUNCTION public.admin_mark_sold(p_code text, p_sold boolean)
 RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public'
AS $function$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then
    raise exception 'forbidden';
  end if;
  update licenses set sold_at = case when coalesce(p_sold, true) then now() else null end
   where code = upper(trim(p_code));
  if not found then return json_build_object('ok', false, 'msg', 'not_found'); end if;
  insert into activate_log(code, device_id, ip, result)
   values (upper(trim(p_code)), null, null,
           case when coalesce(p_sold, true) then 'admin_sold' else 'admin_unsold' end);
  return json_build_object('ok', true);
end;
$function$;

-- 3) admin_licenses：返回 sold_at；p_status 扩为四态筛选
--    active=已激活 / sold=已售未绑定 / unsold=未售库存 / banned=已封禁（恰好划分全集）
CREATE OR REPLACE FUNCTION public.admin_licenses(p_q text DEFAULT NULL::text, p_plan text DEFAULT NULL::text, p_status text DEFAULT NULL::text, p_limit integer DEFAULT 50, p_offset integer DEFAULT 0)
 RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public'
AS $function$
declare total int;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then
    raise exception 'forbidden';
  end if;
  select count(*) into total from licenses
   where (p_q is null or p_q = '' or code ilike '%' || upper(p_q) || '%')
     and (p_plan is null or p_plan = '' or plan = p_plan)
     and (p_status is null or p_status = ''
       or (p_status = 'active' and status = 'active' and devices <> '[]'::jsonb)
       or (p_status = 'sold' and status = 'active' and devices = '[]'::jsonb and sold_at is not null)
       or (p_status = 'unsold' and status = 'active' and devices = '[]'::jsonb and sold_at is null)
       or (p_status = 'banned' and status = 'banned'));
  return json_build_object('total', total, 'rows', (
    select coalesce(json_agg(to_jsonb(t) order by created_at desc), '[]'::json)
    from (
      select code, plan, days, status, max_dev, devices, note, sold_at, created_at
      from licenses
      where (p_q is null or p_q = '' or code ilike '%' || upper(p_q) || '%')
        and (p_plan is null or p_plan = '' or plan = p_plan)
        and (p_status is null or p_status = ''
          or (p_status = 'active' and status = 'active' and devices <> '[]'::jsonb)
          or (p_status = 'sold' and status = 'active' and devices = '[]'::jsonb and sold_at is not null)
          or (p_status = 'unsold' and status = 'active' and devices = '[]'::jsonb and sold_at is null)
          or (p_status = 'banned' and status = 'banned'))
      order by created_at desc
      limit least(coalesce(p_limit, 50), 200) offset greatest(coalesce(p_offset, 0), 0)
    ) t));
end;
$function$;

-- 4) admin_stats：unused 拆为 unsold（未售库存）与 sold（已售未绑定）
CREATE OR REPLACE FUNCTION public.admin_stats()
 RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public'
AS $function$
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then
    raise exception 'forbidden';
  end if;
  return json_build_object(
    'plans', (
      select coalesce(json_agg(to_jsonb(t)), '[]'::json) from (
        select plan,
               count(*) as total,
               count(*) filter (where status = 'active' and devices = '[]'::jsonb and sold_at is null) as unsold,
               count(*) filter (where status = 'active' and devices = '[]'::jsonb and sold_at is not null) as sold,
               count(*) filter (where status = 'banned') as banned
        from licenses group by plan order by plan
      ) t
    ),
    'log24h', (select count(*) from activate_log where at > now() - interval '24 hours')
  );
end;
$function$;

-- ============================================================
-- 后台三期（2026-09-02）：全表排序 + 卡密「最近操作」时间
-- ============================================================

-- 5) 最近操作时间：任何 UPDATE（激活/绑定/解绑/封禁/出售标记…）自动刷新
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- 6) 存量行回填（须在创建触发器之前执行，否则会被刷成 now()）
UPDATE licenses SET updated_at = created_at;

-- 7) 触发器：行级 BEFORE UPDATE 刷新 updated_at（新建 RPC 无需记得维护此字段）
CREATE OR REPLACE FUNCTION public.licenses_touch_updated_at()
 RETURNS trigger LANGUAGE plpgsql
AS $function$
begin
  NEW.updated_at := now();
  RETURN NEW;
end;
$function$;

DROP TRIGGER IF EXISTS trg_licenses_updated_at ON licenses;
CREATE TRIGGER trg_licenses_updated_at BEFORE UPDATE ON licenses
 FOR EACH ROW EXECUTE FUNCTION public.licenses_touch_updated_at();

-- 8) admin_licenses：新增 p_sort/p_dir（默认 updated_at desc）。
--    排序列白名单：code/plan/status/created_at/sold_at/devices(=jsonb_array_length)/updated_at。
--    动态排序经 EXECUTE format，列名/dir 均出自白名单变量。
CREATE OR REPLACE FUNCTION public.admin_licenses(p_q text DEFAULT NULL::text, p_plan text DEFAULT NULL::text, p_status text DEFAULT NULL::text, p_limit integer DEFAULT 50, p_offset integer DEFAULT 0, p_sort text DEFAULT 'updated_at', p_dir text DEFAULT 'desc')
 RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public'
AS $function$
declare total int; v_out json; v_ord text; v_dir text;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then
    raise exception 'forbidden';
  end if;
  v_dir := case when p_dir = 'asc' then 'asc' else 'desc' end;
  v_ord := case p_sort
    when 'code' then 'code'
    when 'plan' then 'plan'
    when 'status' then 'status'
    when 'created_at' then 'created_at'
    when 'sold_at' then 'sold_at'
    when 'devices' then 'jsonb_array_length(devices)'
    else 'updated_at' end;
  select count(*) into total from licenses
   where (p_q is null or p_q = '' or code ilike '%' || upper(p_q) || '%')
     and (p_plan is null or p_plan = '' or plan = p_plan)
     and (p_status is null or p_status = ''
       or (p_status = 'active' and status = 'active' and devices <> '[]'::jsonb)
       or (p_status = 'sold' and status = 'active' and devices = '[]'::jsonb and sold_at is not null)
       or (p_status = 'unsold' and status = 'active' and devices = '[]'::jsonb and sold_at is null)
       or (p_status = 'banned' and status = 'banned'));
  execute format($q$select coalesce(json_agg(to_jsonb(t) order by ord), '[]'::json)
    from (select row_number() over () as ord, x.* from (
      select code, plan, days, status, max_dev, devices, note, sold_at, created_at, updated_at
      from licenses
      where ($1 is null or $1 = '' or code ilike '%%' || upper($1) || '%%')
        and ($2 is null or $2 = '' or plan = $2)
        and ($3 is null or $3 = ''
          or ($3 = 'active' and status = 'active' and devices <> '[]'::jsonb)
          or ($3 = 'sold' and status = 'active' and devices = '[]'::jsonb and sold_at is not null)
          or ($3 = 'unsold' and status = 'active' and devices = '[]'::jsonb and sold_at is null)
          or ($3 = 'banned' and status = 'banned'))
      order by %s %s nulls last
      limit least(coalesce($4, 50), 200) offset greatest(coalesce($5, 0), 0)
    ) x) t$q$, v_ord, v_dir, v_ord, v_dir)
    into v_out using p_q, p_plan, p_status, p_limit, p_offset;  -- EXECUTE 动态 SQL 必须显式 USING 绑定
  return json_build_object('total', total, 'rows', v_out);
end;
$function$;

-- PostgREST checks table-level EXECUTE before the function body ever runs, so
-- the auth.uid() whitelist inside these functions is not sufficient on its
-- own. Without these grants the admin console gets
-- "permission denied for function admin_licenses" and the card list page
-- stays empty. Both admin_licenses overloads must be granted.
grant execute on function
  public.admin_licenses(text, text, text, integer, integer),
  public.admin_licenses(text, text, text, integer, integer, text, text)
  to authenticated;
grant execute on function public.admin_mark_sold(text, boolean) to authenticated;
grant execute on function public.admin_stats() to authenticated;

notify pgrst, 'reload schema';
