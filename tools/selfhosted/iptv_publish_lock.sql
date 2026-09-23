-- PostgREST requests are stateless, so keep the publisher lock in a short
-- database lease instead of relying on a session lock held by a pooled backend.
CREATE TABLE IF NOT EXISTS public.iptv_publish_lock (
    lock_key text PRIMARY KEY CHECK (lock_key = 'publisher'),
    owner text NOT NULL DEFAULT '',
    lease_until timestamptz NOT NULL DEFAULT '-infinity'::timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.iptv_publish_lock (lock_key)
VALUES ('publisher')
ON CONFLICT (lock_key) DO NOTHING;

CREATE OR REPLACE FUNCTION public.iptv_admin_publish_lock(
    p_owner text,
    p_action text DEFAULT 'acquire'
)
RETURNS json
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public
AS $$
DECLARE
    v_locked boolean := false;
    v_released boolean := false;
BEGIN
    IF auth.uid() IS DISTINCT FROM '2094843486709620737' THEN
        RAISE EXCEPTION 'forbidden';
    END IF;
    IF p_owner IS NULL OR p_owner !~ '^[0-9a-fA-F-]{36}$' THEN
        RAISE EXCEPTION 'invalid lock owner';
    END IF;
    IF p_action NOT IN ('acquire', 'renew', 'release') THEN
        RAISE EXCEPTION 'invalid lock action';
    END IF;

    -- Serialize claim/renew/release transitions before updating the lease row.
    PERFORM pg_advisory_xact_lock(17493, 62031);

    IF p_action IN ('acquire', 'renew') THEN
        UPDATE public.iptv_publish_lock
        SET owner = p_owner,
            lease_until = clock_timestamp() + interval '5 minutes',
            updated_at = clock_timestamp()
        WHERE lock_key = 'publisher'
          AND (
              (p_action = 'acquire' AND (lease_until <= clock_timestamp() OR owner = p_owner))
              OR (p_action = 'renew' AND owner = p_owner AND lease_until > clock_timestamp())
          );
        v_locked := FOUND;
        RETURN json_build_object('ret', 0, 'locked', v_locked);
    END IF;

    UPDATE public.iptv_publish_lock
    SET owner = '', lease_until = '-infinity'::timestamptz, updated_at = clock_timestamp()
    WHERE lock_key = 'publisher' AND owner = p_owner;
    v_released := FOUND;
    RETURN json_build_object('ret', 0, 'released', v_released);
END
$$;

REVOKE ALL ON TABLE public.iptv_publish_lock FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.iptv_admin_publish_lock(text, text) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.iptv_admin_publish_lock(text, text) TO authenticated;

NOTIFY pgrst, 'reload schema';
