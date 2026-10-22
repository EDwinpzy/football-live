-- The old license-import API is retired. Keep any existing role disabled so
-- deployments stop requiring its credential without dropping database data.
DO $retire_license_importer$
DECLARE
    v_role_name text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'license_importer'
    ) THEN
        RETURN;
    END IF;

    EXECUTE 'ALTER ROLE license_importer WITH NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
    EXECUTE format('REVOKE ALL PRIVILEGES ON DATABASE %I FROM license_importer', current_database());
    EXECUTE 'REVOKE ALL PRIVILEGES ON SCHEMA public FROM license_importer';
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM license_importer';
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM license_importer';
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM license_importer';

    FOR v_role_name IN
        SELECT granted_role.rolname
        FROM pg_catalog.pg_auth_members membership
        JOIN pg_catalog.pg_roles granted_role ON granted_role.oid = membership.roleid
        WHERE membership.member = 'license_importer'::regrole
    LOOP
        EXECUTE format('REVOKE %I FROM license_importer', v_role_name);
    END LOOP;
END
$retire_license_importer$;
