-- My TV 卡密备注编辑（幂等迁移）
-- 备注沿用 licenses.note；后台通过受保护 RPC 更新，并在激活流水留下审计记录。

ALTER TABLE public.licenses
  ADD COLUMN IF NOT EXISTS note text NOT NULL DEFAULT '';

CREATE OR REPLACE FUNCTION public.admin_update_note(
  p_code text,
  p_note text DEFAULT ''
) RETURNS json
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_code text := upper(trim(coalesce(p_code, '')));
  v_note text := left(trim(coalesce(p_note, '')), 200);
BEGIN
  IF coalesce(auth.uid(), '') <> '2094843486709620737' THEN
    RAISE EXCEPTION 'forbidden';
  END IF;
  IF v_code !~ '^OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$' THEN
    RETURN json_build_object('ok', false, 'msg', 'bad_code');
  END IF;
  UPDATE public.licenses
     SET note = v_note
   WHERE code = v_code;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'msg', 'not_found');
  END IF;
  INSERT INTO public.activate_log(code, device_id, ip, result)
    VALUES (v_code, NULL, NULL, 'admin_note');
  RETURN json_build_object('ok', true, 'note', v_note);
END;
$function$;

REVOKE ALL ON FUNCTION public.admin_update_note(text, text) FROM public, anon;
GRANT EXECUTE ON FUNCTION public.admin_update_note(text, text) TO authenticated;
