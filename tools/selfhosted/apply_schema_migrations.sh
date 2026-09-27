#!/bin/sh
set -eu

password_file=${LICENSE_IMPORTER_PASSWORD_FILE:-}
if [ -z "$password_file" ] || [ ! -r "$password_file" ]; then
    printf '%s\n' "schema migration failed: importer password file is unavailable" >&2
    exit 1
fi

LICENSE_IMPORTER_PASSWORD=$(tr -d '\r\n' < "$password_file")
if [ -z "$LICENSE_IMPORTER_PASSWORD" ]; then
    printf '%s\n' "schema migration failed: importer password file is empty" >&2
    exit 1
fi
export LICENSE_IMPORTER_PASSWORD

: "${POSTGRES_USER:?schema migration failed: POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?schema migration failed: POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?schema migration failed: POSTGRES_DB is required}"
: "${POSTGREST_DB_PASSWORD:?schema migration failed: POSTGREST_DB_PASSWORD is required}"

export PGHOST=${PGHOST:-postgres}
export PGPORT=${PGPORT:-5432}
export PGUSER=$POSTGRES_USER
export PGPASSWORD=$POSTGRES_PASSWORD
export PGDATABASE=$POSTGRES_DB

tools_root=${MIGRATION_TOOLS_ROOT:-/app/tools}
if [ ! -d "$tools_root" ]; then
    printf '%s\n' "schema migration failed: migration files are unavailable" >&2
    exit 1
fi

temp_prefix=${TMPDIR:-/tmp}/otv-schema-migrations.
migration_root=$(mktemp -d "${temp_prefix}XXXXXX")
case "$migration_root" in
    "$temp_prefix"*) ;;
    *)
        printf '%s\n' "schema migration failed: unsafe temporary directory" >&2
        exit 1
        ;;
esac
trap 'rm -rf -- "$migration_root"' EXIT
trap 'exit 1' HUP INT TERM

relative_files='
selfhosted/apply_schema_migrations.psql
selfhosted/db_bootstrap.sql
selfhosted/iptv_publish_lock.sql
selfhosted/base_schema.sql
selfhosted/license_importer_role.sql
selfhosted/license_importer_smoke.psql
license_v2_migration.sql
license_note_migration.sql
admin_sold_migration.sql
hotupdate_migration.sql
announcement_migration.sql
iptv_migration.sql
vod_sources_migration.sql
pg_cu_optimization_migration.sql
'
for relative_path in $relative_files; do
    destination="$migration_root/tools/$relative_path"
    mkdir -p "$(dirname "$destination")"
    if ! cp "$tools_root/$relative_path" "$destination"; then
        printf '%s\n' "schema migration failed: required migration file is unavailable" >&2
        exit 1
    fi
done

if ! awk '
    /^-- SELFHOSTED-OMIT-BEGIN:/ {
        if (skip) exit 2
        skip = 1
        begin_count++
        next
    }
    /^-- SELFHOSTED-OMIT-END/ {
        if (!skip) exit 2
        skip = 0
        end_count++
        next
    }
    !skip { print }
    END {
        if (skip || begin_count != end_count) exit 2
    }
' "$tools_root/hotupdate_migration.sql" > "$migration_root/tools/hotupdate_migration.sql"; then
    printf '%s\n' "schema migration failed: self-hosted migration filtering failed" >&2
    exit 1
fi

output_file="$migration_root/psql-output.log"

run_psql_file() {
    if psql -X -q -v ON_ERROR_STOP=1 -f "$1" >"$output_file" 2>&1; then
        return 0
    fi
    printf '%s\n' "schema migration failed; dependent API services will remain stopped" >&2
    return 1
}

run_psql_file "$migration_root/tools/selfhosted/apply_schema_migrations.psql" || exit 1
