#!/usr/bin/env bash
#
# health_check.sh — write a small JSON health snapshot for the on-screen light.
#
# Checks only things measurable on the appliance itself (Tier 1: the app lives
# in the cloud, so we can't count judge devices — we check that the cloud app
# is reachable, the last backup is fresh, and the disk isn't full).
#
# Output: HEALTH_FILE (default /var/lib/marksys/health.json), read by
# health_overlay.py. Always exits 0 — it is a reporter, not a gate.

set -uo pipefail

ENV_FILE="${MARKSYS_ENV_FILE:-/etc/marksys/appliance.env}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

APP_URL="${APP_URL:-}"
HEALTH_FILE="${HEALTH_FILE:-/var/lib/marksys/health.json}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/marksys}"
DISK_WARN_PCT="${DISK_WARN_PCT:-15}"   # WARN when free% below this
DISK_FAIL_PCT="${DISK_FAIL_PCT:-5}"    # FAIL when free% below this
BACKUP_WARN_HOURS="${BACKUP_WARN_HOURS:-26}"

mkdir -p "$(dirname "$HEALTH_FILE")"

# ---- app reachable? --------------------------------------------------------
app_http=000
if [ -n "$APP_URL" ]; then
    app_http="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$APP_URL" 2>/dev/null || echo 000)"
fi
app_http=$((10#${app_http:-0}))   # strip leading zeros so it's valid JSON
if [ "$app_http" -eq 200 ]; then app_status=OK; else app_status=FAIL; fi

# ---- last backup fresh? ----------------------------------------------------
backup_status=WARN; backup_age_min=-1; backup_result=NONE
STATUS_FILE="$BACKUP_DIR/last_backup.status"
if [ -f "$STATUS_FILE" ]; then
    bts="$(grep -E '^timestamp=' "$STATUS_FILE" | cut -d= -f2- || true)"
    backup_result="$(grep -E '^result=' "$STATUS_FILE" | cut -d= -f2- || echo NONE)"
    if [ -n "${bts:-}" ]; then
        now="$(date +%s)"
        then_ts="$(date -d "$bts" +%s 2>/dev/null || echo 0)"
        [ "$then_ts" -gt 0 ] && backup_age_min=$(( (now - then_ts) / 60 ))
    fi
    if [ "$backup_result" = "OK" ]; then
        if [ "$backup_age_min" -ge 0 ] && [ "$backup_age_min" -le $((BACKUP_WARN_HOURS * 60)) ]; then
            backup_status=OK
        else
            backup_status=WARN
        fi
    else
        backup_status=FAIL
    fi
fi

# ---- disk (root filesystem) ------------------------------------------------
free_pct="$(df -P / | awk 'NR==2 {gsub("%","",$5); print 100-$5}')"
free_pct=$((10#${free_pct:-0}))
used_pct=$((100 - free_pct))
if   [ "$free_pct" -lt "$DISK_FAIL_PCT" ]; then disk_status=FAIL
elif [ "$free_pct" -lt "$DISK_WARN_PCT" ]; then disk_status=WARN
else disk_status=OK; fi

# ---- overall (worst of the three) ------------------------------------------
overall=OK
for s in "$app_status" "$backup_status" "$disk_status"; do
    if [ "$s" = FAIL ]; then overall=FAIL; break; fi
    if [ "$s" = WARN ]; then overall=WARN; fi
done

# ---- write JSON atomically -------------------------------------------------
tmp="${HEALTH_FILE}.tmp"
cat > "$tmp" <<EOF
{
  "ts": "$(date -Is)",
  "overall": "$overall",
  "app": {"status": "$app_status", "http": $app_http},
  "backup": {"status": "$backup_status", "age_min": $backup_age_min, "result": "$backup_result"},
  "disk": {"status": "$disk_status", "free_pct": $free_pct, "used_pct": $used_pct}
}
EOF
mv "$tmp" "$HEALTH_FILE"
