#!/usr/bin/env bash
#
# backup_db.sh — Dump the remote Supabase PostgreSQL to a local backup file.
#
# Tier 1 appliance: the app keeps using Supabase as its live database. This
# script is pure insurance — a scheduled local snapshot so a Supabase account
# problem, accidental delete, or bad migration never means total data loss.
#
# It reads the SAME connection block the app uses ([connections.postgresql] in
# secrets.toml), so there is nothing extra to configure. Meant to be run by the
# systemd timer (marksys-backup.timer) but is safe to run by hand too.
#
# Exit codes: 0 ok, 2 config error, 3 missing DB credentials, 4 dump failed.

set -euo pipefail

# ---- config (override via /etc/marksys/appliance.env) ----------------------
ENV_FILE="${MARKSYS_ENV_FILE:-/etc/marksys/appliance.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

SECRETS_FILE="${SECRETS_FILE:-/etc/marksys/secrets.toml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/marksys}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
PGSSLMODE="${PGSSLMODE:-require}"
# Optional: mount point of a USB stick to mirror the newest dump onto.
USB_MOUNT="${USB_MOUNT:-}"

log() { printf '%s %s\n' "$(date -Is)" "$*"; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

# ---- preflight -------------------------------------------------------------
command -v pg_dump >/dev/null 2>&1 || fail "pg_dump not found — install postgresql-client (matching the Supabase major version)" 2
command -v python3 >/dev/null 2>&1 || fail "python3 not found" 2
python3 -c 'import tomllib' 2>/dev/null || python3 -c 'import tomli' 2>/dev/null \
    || fail "no TOML parser — need Python 3.11+ (stdlib tomllib) or the 'python3-tomli' package (Ubuntu 22.04)" 2
[ -f "$SECRETS_FILE" ] || fail "secrets file not found: $SECRETS_FILE" 2

mkdir -p "$BACKUP_DIR"

# ---- read the DB connection straight from secrets.toml ---------------------
# Uses Python's tomllib (3.11+) so quoting/edge cases are handled correctly.
CONN="$(python3 - "$SECRETS_FILE" <<'PY'
import sys, shlex
try:
    import tomllib
except ModuleNotFoundError:            # Python < 3.11 (e.g. Ubuntu 22.04)
    import tomli as tomllib
with open(sys.argv[1], "rb") as f:
    data = tomllib.load(f)
c = data.get("connections", {}).get("postgresql", {})
req = ["host", "port", "database", "username", "password"]
missing = [k for k in req if not c.get(k)]
if missing:
    sys.stderr.write("missing keys in [connections.postgresql]: " + ",".join(missing) + "\n")
    sys.exit(3)
for var, key in (("PG_HOST","host"),("PG_PORT","port"),("PG_DB","database"),
                 ("PG_USER","username"),("PG_PASSWORD","password")):
    print(f"{var}={shlex.quote(str(c[key]))}")
PY
)" || fail "could not read database credentials from $SECRETS_FILE" 3
eval "$CONN"

# Supabase note: pg_dump needs a DIRECT or SESSION connection (port 5432).
# The transaction pooler (port 6543) does not support pg_dump.
if [ "${PG_PORT}" = "6543" ]; then
    log "WARN: port 6543 is Supabase's transaction pooler; pg_dump may fail. Use the direct/session connection (5432)."
fi

# ---- do the dump -----------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP_FILE="$BACKUP_DIR/.marksys-${STAMP}.dump.partial"
FINAL_FILE="$BACKUP_DIR/marksys-${STAMP}.dump"
STATUS_FILE="$BACKUP_DIR/last_backup.status"

log "Backing up ${PG_DB} @ ${PG_HOST}:${PG_PORT} -> ${FINAL_FILE}"

write_status() {
    # Consumed by health_check.sh so the on-screen light can show backup age.
    {
        echo "timestamp=$(date -Is)"
        echo "result=$1"
        echo "file=${2:-}"
        echo "size_bytes=${3:-0}"
    } > "$STATUS_FILE"
}

if ! PGPASSWORD="$PG_PASSWORD" PGSSLMODE="$PGSSLMODE" \
        pg_dump -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
        --format=custom --no-owner --no-privileges \
        --file="$TMP_FILE"; then
    rm -f "$TMP_FILE"
    write_status "FAILED" "" 0
    fail "pg_dump failed" 4
fi

mv "$TMP_FILE" "$FINAL_FILE"
SIZE="$(stat -c%s "$FINAL_FILE" 2>/dev/null || echo 0)"
if [ "$SIZE" -lt 1024 ]; then
    write_status "FAILED" "$FINAL_FILE" "$SIZE"
    fail "dump suspiciously small (${SIZE} bytes) — treating as failure" 4
fi

log "OK: wrote ${FINAL_FILE} (${SIZE} bytes)"
write_status "OK" "$FINAL_FILE" "$SIZE"

# ---- optional: mirror newest dump to a USB stick ---------------------------
if [ -n "$USB_MOUNT" ] && mountpoint -q "$USB_MOUNT" 2>/dev/null; then
    if cp "$FINAL_FILE" "$USB_MOUNT/"; then
        log "Mirrored to USB: $USB_MOUNT"
    else
        log "WARN: could not copy to USB at $USB_MOUNT"
    fi
elif [ -n "$USB_MOUNT" ]; then
    log "WARN: USB_MOUNT set ($USB_MOUNT) but nothing mounted there — skipping USB copy"
fi

# ---- rotate old backups ----------------------------------------------------
if [ "$RETENTION_DAYS" -gt 0 ]; then
    DELETED="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'marksys-*.dump' -mtime +"$RETENTION_DAYS" -print -delete | wc -l | tr -d ' ')"
    [ "$DELETED" != "0" ] && log "Rotated out ${DELETED} backup(s) older than ${RETENTION_DAYS} days"
fi

log "Done."
