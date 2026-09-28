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
selfhosted/migrations/license_restore_v1.sql
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

if ! command -v sha256sum >/dev/null 2>&1; then
    printf '%s\n' "schema migration failed: sha256sum is unavailable" >&2
    exit 1
fi

sha256_file() {
    hash_output=$(sha256sum "$1") || return 1
    hash_value=${hash_output%% *}
    case "$hash_value" in
        *[!0123456789abcdef]*|'') return 1 ;;
    esac
    if [ "${#hash_value}" -ne 64 ]; then
        return 1
    fi
    printf '%s' "$hash_value"
}

sha256_bootstrap_bundle() {
    hash_output=$(
        {
            cat "$migration_root/tools/selfhosted/db_bootstrap.sql"
            printf '\n-- included SQL: iptv_publish_lock.sql --\n'
            cat "$migration_root/tools/selfhosted/iptv_publish_lock.sql"
        } | sha256sum
    ) || return 1
    hash_value=${hash_output%% *}
    case "$hash_value" in
        *[!0123456789abcdef]*|'') return 1 ;;
    esac
    if [ "${#hash_value}" -ne 64 ]; then
        return 1
    fi
    printf '%s' "$hash_value"
}

migration_hash_db_bootstrap=$(sha256_bootstrap_bundle) || {
    printf '%s\n' "schema migration failed: could not hash bootstrap SQL" >&2
    exit 1
}
migration_hash_base_schema=$(sha256_file "$migration_root/tools/selfhosted/base_schema.sql") || {
    printf '%s\n' "schema migration failed: could not hash base schema SQL" >&2
    exit 1
}
migration_hash_license_v2=$(sha256_file "$migration_root/tools/license_v2_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash license v2 SQL" >&2
    exit 1
}
migration_hash_license_note=$(sha256_file "$migration_root/tools/license_note_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash license note SQL" >&2
    exit 1
}
migration_hash_license_restore=$(sha256_file "$migration_root/tools/selfhosted/migrations/license_restore_v1.sql") || {
    printf '%s\n' "schema migration failed: could not hash license restore SQL" >&2
    exit 1
}
migration_hash_admin_sold=$(sha256_file "$migration_root/tools/admin_sold_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash admin sold SQL" >&2
    exit 1
}
migration_hash_hotupdate=$(sha256_file "$migration_root/tools/hotupdate_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash filtered hot-update SQL" >&2
    exit 1
}
migration_hash_announcement=$(sha256_file "$migration_root/tools/announcement_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash announcement SQL" >&2
    exit 1
}
migration_hash_iptv=$(sha256_file "$migration_root/tools/iptv_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash IPTV SQL" >&2
    exit 1
}
migration_hash_vod_sources=$(sha256_file "$migration_root/tools/vod_sources_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash VOD source SQL" >&2
    exit 1
}
migration_hash_pg_cu_optimization=$(sha256_file "$migration_root/tools/pg_cu_optimization_migration.sql") || {
    printf '%s\n' "schema migration failed: could not hash PostgreSQL optimization SQL" >&2
    exit 1
}
migration_hash_license_importer_role=$(sha256_file "$migration_root/tools/selfhosted/license_importer_role.sql") || {
    printf '%s\n' "schema migration failed: could not hash importer role SQL" >&2
    exit 1
}

output_file="$migration_root/psql-output.log"

run_psql_file() {
    if psql -X -q -v ON_ERROR_STOP=1 \
        -v "db_bootstrap_sha256=$migration_hash_db_bootstrap" \
        -v "base_schema_sha256=$migration_hash_base_schema" \
        -v "license_v2_sha256=$migration_hash_license_v2" \
        -v "license_note_sha256=$migration_hash_license_note" \
        -v "license_restore_sha256=$migration_hash_license_restore" \
        -v "admin_sold_sha256=$migration_hash_admin_sold" \
        -v "hotupdate_sha256=$migration_hash_hotupdate" \
        -v "announcement_sha256=$migration_hash_announcement" \
        -v "iptv_sha256=$migration_hash_iptv" \
        -v "vod_sources_sha256=$migration_hash_vod_sources" \
        -v "pg_cu_optimization_sha256=$migration_hash_pg_cu_optimization" \
        -v "license_importer_role_sha256=$migration_hash_license_importer_role" \
        -f "$1" >"$output_file" 2>&1; then
        return 0
    fi
    printf '%s\n' "schema migration failed; dependent API services will remain stopped" >&2
    return 1
}

run_psql_file "$migration_root/tools/selfhosted/apply_schema_migrations.psql" || exit 1
