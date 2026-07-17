#!/usr/bin/env bash
#
# update.sh — administrator-only updater for the appliance's local repo.
#
# Pulls the latest appliance scripts (kiosk chooser, health overlay, backup,
# systemd units) from the git remote and hard-resets to it, so the machine no
# longer needs a manual `git pull`. Safe to run repeatedly; it only touches the
# repo checkout, never the cloud app (that deploys separately on Render).
#
# 呢個腳本只畀管理員由維修 shell 手動執行；學生／主席嘅 kiosk chooser
# 不會再提供更新入口。更新後要由管理員核對 diff、重新安裝有變更嘅
# systemd unit，並安排重啟 kiosk session：
#
#     /opt/skhlmc-dbt-marksys/appliance/update.sh
#
# Repo checkout 權限不應為咗 kiosk operator 而放寬。

set -euo pipefail

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
