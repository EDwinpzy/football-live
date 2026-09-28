\set ON_ERROR_STOP on
\getenv license_importer_password LICENSE_IMPORTER_PASSWORD
\getenv importer_database POSTGRES_DB

SET LOCAL log_statement = 'none';
SET LOCAL log_duration = off;
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_min_duration_sample = -1;
SET LOCAL log_statement_sample_rate = 0;
SET LOCAL log_parameter_max_length = 0;
SET LOCAL log_min_error_statement = 'panic';

SELECT 'CREATE ROLE license_importer NOLOGIN'
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'license_importer'
)
\gexec

ALTER ROLE license_importer
    WITH LOGIN
    NOINHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

-- Quote the password as a SQL literal without ever printing it to the psql
-- output stream. It is supplied only through the mounted secret file.
SELECT format(
    'ALTER ROLE license_importer PASSWORD %L',
    :'license_importer_password'
)
\gexec

SELECT format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM license_importer',
    :'importer_database'
)
\gexec
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO license_importer',
    :'importer_database'
)
\gexec

DO $revoke_memberships$
DECLARE
    v_role_name text;
BEGIN
    FOR v_role_name IN
        SELECT granted_role.rolname
        FROM pg_catalog.pg_auth_members membership
        JOIN pg_catalog.pg_roles granted_role ON granted_role.oid = membership.roleid
        WHERE membership.member = 'license_importer'::regrole
    LOOP
        EXECUTE format('REVOKE %I FROM license_importer', v_role_name);
    END LOOP;
END
$revoke_memberships$;

REVOKE ALL PRIVILEGES ON SCHEMA public FROM license_importer;
GRANT USAGE ON SCHEMA public TO license_importer;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM license_importer;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM license_importer;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM license_importer;
-- SECURITY DEFINER functions receive EXECUTE for PUBLIC by default on older
-- databases. Remove that ambient grant globally; explicit anon/authenticated
-- grants below and in the migrations remain intact.
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.licenses FROM license_importer;
GRANT INSERT (code, plan, days, ticket, note), SELECT (code)
    ON TABLE public.licenses TO license_importer;

-- Re-establish the documented client RPC boundary after removing legacy
-- PUBLIC grants. PostgREST roles are explicit database roles, not PUBLIC.
GRANT EXECUTE ON FUNCTION public.activate_code(text, text, text, integer, text, text)
    TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.hotupdate_check(integer, integer)
    TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.hotupdate_report(text, integer, integer, integer, text, text)
    TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.announcement_latest()
    TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.vod_source_public_check(bigint)
    TO anon, authenticated;

-- Ensure the signed-in admin role keeps its privileged RPCs while anon and
-- the importer remain excluded from the management surface.
GRANT EXECUTE ON FUNCTION public.admin_mark_sold(text, boolean), public.admin_stats()
    TO authenticated;
GRANT EXECUTE ON FUNCTION public.vod_source_admin_list(),
    public.vod_source_admin_save(uuid,text,text,text,text,boolean,integer),
    public.vod_source_admin_set_enabled(uuid,boolean),
    public.vod_source_admin_set_priority(uuid),
    public.vod_source_admin_delete(uuid),
    public.vod_source_admin_publish()
    TO authenticated;

DO $validate_importer_privileges$
DECLARE
    v_column record;
    v_admin_rpc text;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles importer_role
        WHERE importer_role.rolname = 'license_importer'
          AND (
              importer_role.rolsuper
              OR importer_role.rolcreatedb
              OR importer_role.rolcreaterole
              OR importer_role.rolreplication
              OR importer_role.rolbypassrls
              OR importer_role.rolinherit
          )
    ) THEN
        RAISE EXCEPTION 'license_importer role attributes are not restricted';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE member = 'license_importer'::regrole
    ) THEN
        RAISE EXCEPTION 'license_importer must not inherit any role memberships';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class rel
        JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'public'
          AND rel.relowner = 'license_importer'::regrole
    ) THEN
        RAISE EXCEPTION 'license_importer must not own public relations';
    END IF;

    IF has_table_privilege('license_importer', 'public.licenses', 'SELECT')
       OR has_table_privilege('license_importer', 'public.licenses', 'UPDATE')
       OR has_table_privilege('license_importer', 'public.licenses', 'DELETE')
       OR has_table_privilege('license_importer', 'public.licenses', 'TRUNCATE') THEN
        RAISE EXCEPTION 'license_importer has prohibited table privileges';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class rel
        JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'public'
          AND rel.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND rel.relname <> 'licenses'
          AND (
              has_table_privilege('license_importer', rel.oid, 'SELECT')
              OR has_table_privilege('license_importer', rel.oid, 'INSERT')
              OR has_table_privilege('license_importer', rel.oid, 'UPDATE')
              OR has_table_privilege('license_importer', rel.oid, 'DELETE')
          )
    ) THEN
        RAISE EXCEPTION 'license_importer has privileges on another public relation';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc proc
        JOIN pg_catalog.pg_namespace nsp ON nsp.oid = proc.pronamespace
        WHERE nsp.nspname = 'public'
          AND has_function_privilege('license_importer', proc.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'license_importer has execute privileges on public functions';
    END IF;

    IF NOT has_function_privilege(
        'anon', 'public.activate_code(text,text,text,integer,text,text)', 'EXECUTE'
    ) OR NOT has_function_privilege(
        'anon', 'public.hotupdate_check(integer,integer)', 'EXECUTE'
    ) OR NOT has_function_privilege(
        'anon', 'public.hotupdate_report(text,integer,integer,integer,text,text)', 'EXECUTE'
    ) OR NOT has_function_privilege(
        'anon', 'public.announcement_latest()', 'EXECUTE'
    ) OR NOT has_function_privilege(
        'anon', 'public.vod_source_public_check(bigint)', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'anon is missing an explicitly granted client RPC';
    END IF;

    IF NOT has_function_privilege(
        'authenticated', 'public.admin_mark_sold(text,boolean)', 'EXECUTE'
    ) OR NOT has_function_privilege(
        'authenticated', 'public.admin_stats()', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'authenticated is missing required admin RPC privileges';
    END IF;

    FOREACH v_admin_rpc IN ARRAY ARRAY[
        'public.vod_source_admin_list()',
        'public.vod_source_admin_save(uuid,text,text,text,text,boolean,integer)',
        'public.vod_source_admin_set_enabled(uuid,boolean)',
        'public.vod_source_admin_set_priority(uuid)',
        'public.vod_source_admin_delete(uuid)',
        'public.vod_source_admin_publish()'
    ] LOOP
        IF NOT has_function_privilege('authenticated', v_admin_rpc, 'EXECUTE')
           OR has_function_privilege('anon', v_admin_rpc, 'EXECUTE')
           OR has_function_privilege('license_importer', v_admin_rpc, 'EXECUTE') THEN
            RAISE EXCEPTION 'incorrect VOD admin RPC privilege: %', v_admin_rpc;
        END IF;
    END LOOP;

    IF has_function_privilege(
        'anon', 'public.admin_mark_sold(text,boolean)', 'EXECUTE'
    ) OR has_function_privilege(
        'license_importer', 'public.admin_mark_sold(text,boolean)', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'an unauthenticated importer can execute an admin RPC';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class rel
        JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'public'
          AND rel.relkind = 'S'
          AND (
              has_sequence_privilege('license_importer', rel.oid, 'USAGE')
              OR has_sequence_privilege('license_importer', rel.oid, 'SELECT')
              OR has_sequence_privilege('license_importer', rel.oid, 'UPDATE')
          )
    ) THEN
        RAISE EXCEPTION 'license_importer has sequence privileges';
    END IF;

    IF NOT has_column_privilege('license_importer', 'public.licenses', 'code', 'SELECT')
       OR NOT has_column_privilege('license_importer', 'public.licenses', 'code', 'INSERT')
       OR NOT has_column_privilege('license_importer', 'public.licenses', 'plan', 'INSERT')
       OR NOT has_column_privilege('license_importer', 'public.licenses', 'days', 'INSERT')
       OR NOT has_column_privilege('license_importer', 'public.licenses', 'ticket', 'INSERT')
       OR NOT has_column_privilege('license_importer', 'public.licenses', 'note', 'INSERT') THEN
        RAISE EXCEPTION 'license_importer is missing required column privileges';
    END IF;

    FOR v_column IN
        SELECT attribute.attname
        FROM pg_catalog.pg_attribute attribute
        WHERE attribute.attrelid = 'public.licenses'::regclass
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
    LOOP
        IF v_column.attname <> 'code'
           AND has_column_privilege(
               'license_importer', 'public.licenses', v_column.attname, 'SELECT'
           ) THEN
            RAISE EXCEPTION 'license_importer can read prohibited license columns';
        END IF;
        IF has_column_privilege(
            'license_importer', 'public.licenses', v_column.attname, 'UPDATE'
        ) THEN
            RAISE EXCEPTION 'license_importer can update license columns';
        END IF;
        IF v_column.attname NOT IN ('code', 'plan', 'days', 'ticket', 'note')
           AND has_column_privilege(
               'license_importer', 'public.licenses', v_column.attname, 'INSERT'
           ) THEN
            RAISE EXCEPTION 'license_importer can insert prohibited license columns';
        END IF;
    END LOOP;

    IF has_schema_privilege('license_importer', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'license_importer can create objects in public';
    END IF;
END
$validate_importer_privileges$;
