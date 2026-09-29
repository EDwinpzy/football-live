-- 影视源「撤销 / 回滚」（BUG-06）。
--
-- 问题：`vod_source_admin_delete` 直接 `delete from vod_source_drafts where id=p_id`，
-- **不可逆**。误删一个源后只能靠 SQL 手工补回；想整体回退又会把其它源
-- 期间做的改动一并丢掉。
--
-- 基础已经有了：`vod_source_versions` 存着每次发布的**完整配置快照**
-- （config jsonb，含 sources 全字段）。所以撤销 = 把 drafts 按某个历史
-- 版本重建，而不是去动历史。
--
-- 两条设计约束（写在这里，任何调用方都绕不过）：
--   1. 历史版本是**只追加**账本：回滚只改 drafts，绝不删/改 versions
--      ——否则「撤销」本身也变得不可撤销。
--   2. 回滚**不隐式发布**：改完 drafts 仍需显式调用 publish 才对线上生效。
--      否则用户会以为「撤销了」而线上其实没变。

-- 列出可回滚的历史版本（新到旧）。附源数量便于前端展示。
create or replace function public.vod_source_admin_versions()
    returns json
    language sql security definer set search_path to public as $$
  select coalesce(jsonb_agg(
          jsonb_build_object(
            'version', v.version,
            'publishedAt', v.published_at,
            'sha256', v.sha256,
            'sourceCount', coalesce(jsonb_array_length(v.config->'sources'), 0)
          ) order by v.version desc
        ), '[]'::jsonb)
    from vod_source_versions v;
$$;

-- 回滚 drafts 到指定历史版本。
create or replace function public.vod_source_admin_rollback(p_version bigint)
    returns json
    language plpgsql security definer set search_path to public as $$
declare
  v_config jsonb;
  v_prev_version bigint;
  v_restored int := 0;
begin
  if coalesce(auth.uid(), '') <> '2094843486709620737' then
    raise exception 'forbidden';
  end if;

  select config into v_config
    from vod_source_versions
   where version = p_version;
  if v_config is null then
    return jsonb_build_object('ok', false, 'msg', 'not_found');
  end if;

  select coalesce(max(version), 0) into v_prev_version from vod_source_versions;

  -- 串行化：与 publish 用同一张 versions 表做锁锚，避免回滚与发布交叉
  perform version from vod_source_versions order by version desc limit 1 for update;
  lock table vod_source_versions in share row exclusive mode;

  -- drafts 按快照重建（先清后插，幂等）。sort_order 用快照里的数组下标，
  -- 保持与当时发布时一致的顺序。
  delete from vod_source_drafts;

  insert into vod_source_drafts
    (id, name, site_url, api_url, adapter_type, enabled, sort_order, revision, updated_at)
  select
    (s->>'id')::uuid,
    coalesce(s->>'name', ''),
    coalesce(s->>'site_url', ''),
    coalesce(s->>'api_url', ''),
    coalesce(s->>'adapter_type', 'macms_json'),
    coalesce((s->>'enabled')::boolean, true),
    ord - 1,
    coalesce((s->>'revision')::int, 1),
    now()
  from jsonb_array_elements(v_config->'sources') with ordinality as s(s, ord);

  get diagnostics v_restored = row_count;

  return jsonb_build_object(
    'ok', true,
    'restoredFrom', p_version,
    'publishedVersion', v_prev_version,
    'drafts', v_restored,
    'note', 'drafts 已回滚，需显式发布才对线上生效'
  );
end $$;

-- 授权：与既有 vod_source_admin_* 一致，anon/public 不给。
revoke execute on function public.vod_source_admin_versions() from public, anon;
revoke execute on function public.vod_source_admin_rollback(bigint) from public, anon;
grant execute on function public.vod_source_admin_versions() to authenticated;
grant execute on function public.vod_source_admin_rollback(bigint) to authenticated;
