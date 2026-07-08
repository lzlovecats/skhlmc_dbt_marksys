#!/usr/bin/env bash
#
# update.sh — one-click updater for the appliance's local copy of the repo.
#
# Pulls the latest appliance scripts (kiosk chooser, health overlay, backup,
# systemd units) from the git remote and hard-resets to it, so the machine no
# longer needs a manual `git pull`. Safe to run repeatedly; it only touches the
# repo checkout, never the cloud app (that deploys separately on Render).
#
# Typically invoked from the boot chooser's "🔄 更新系統" entry (see
# marksys-kiosk.sh), which runs this then restarts the X session so the freshly
# pulled scripts take effect. Can also be run by hand:
#
#     /opt/skhlmc-dbt-marksys/appliance/update.sh
#
# For the chooser to run this without a password prompt, the repo checkout must
# be writable by the marksys user (see appliance/README.md → 一鍵更新).

set -uo pipefail

APP_DIR="${MARKSYS_APP_DIR:-/opt/skhlmc-dbt-marksys}"
BRANCH="${MARKSYS_UPDATE_BRANCH:-main}"

log() { printf '[update] %s\n' "$*"; }

if [ ! -d "$APP_DIR/.git" ]; then
    log "唔係 git repo：$APP_DIR"
    exit 1
fi

cd "$APP_DIR" || { log "入唔到 $APP_DIR"; exit 1; }

before="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
log "而家版本：$before，拉取 origin/$BRANCH …"

if ! git fetch --quiet origin "$BRANCH"; then
    log "git fetch 失敗（網絡？）"
    exit 1
fi

# Hard-reset so a half-edited working tree on the appliance never blocks updates.
if ! git reset --hard "origin/$BRANCH"; then
    log "git reset 失敗"
    exit 1
fi

after="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
if [ "$before" = "$after" ]; then
    log "已經係最新（$after），冇嘢更新。"
else
    log "已更新：$before → $after"
fi

# Restart the health light so any change to health_overlay.py takes effect. The
# kiosk chooser itself is reloaded by the caller restarting the X session.
pkill -f health_overlay.py 2>/dev/null || true

exit 0
