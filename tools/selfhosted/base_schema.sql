-- Base tables that existed before the repository's versioned CloudBase migrations.
-- Definitions were reconstructed from the live database catalog on 2026-09-23.

CREATE TABLE IF NOT EXISTS public.licenses (
    code text PRIMARY KEY,
    plan text NOT NULL CHECK (plan = ANY (ARRAY['monthly'::text, 'quarterly'::text, 'yearly'::text, 'lifetime'::text, 'weekly'::text])),
    days integer NOT NULL,
    status text NOT NULL DEFAULT 'active'::text,
    max_dev integer NOT NULL DEFAULT 2,
    devices jsonb NOT NULL DEFAULT '[]'::jsonb,
    ticket jsonb NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    sold_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    expire_at timestamptz,
    redeemed_at timestamptz,
    redeemed_to text
);

CREATE TABLE IF NOT EXISTS public.activate_log (
    id bigserial PRIMARY KEY,
    code text,
    device_id text,
    ip text,
    result text,
    at timestamptz NOT NULL DEFAULT now()
);
