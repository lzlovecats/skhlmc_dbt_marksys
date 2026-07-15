#!/usr/bin/env bash
#
# marksys-kiosk.sh — boot-time mode chooser + Chromium kiosk loop.
#
# On start it asks the operator to pick a mode:
#   日常練習 (practice)  — students / coaches self-service
#   比賽日   (contest)   — chairperson / competition
# then opens Chromium in kiosk fullscreen at the URL for that mode. When
# Chromium is closed (Alt+F4), it loops back to the chooser so you can switch
# modes without rebooting.
#
# Tier 1: both modes point at the cloud app. Set PRACTICE_URL / CONTEST_URL in
# /etc/marksys/appliance.env if you want them to open different paths.

set -uo pipefail   # not -e: the loop must survive a browser crash

ENV_FILE="${MARKSYS_ENV_FILE:-/etc/marksys/appliance.env}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

APP_URL="${APP_URL:-http://localhost:8000}"
PRACTICE_URL="${PRACTICE_URL:-${APP_URL%/}/practice}"
# kiosk=1 enables the private microphone/speaker command engine.  Plain
# /projector remains a safe public, display-only view.
CONTEST_URL="${CONTEST_URL:-${APP_URL%/}/projector?kiosk=1}"
CHOOSER_TIMEOUT="${CHOOSER_TIMEOUT:-0}"   # seconds; 0 = wait forever

CHROME="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
if [ -z "$CHROME" ]; then
    zenity --error --text="搵唔到 Chromium，請先安裝。" 2>/dev/null || true
    sleep 10
    exit 1
fi

# no screen blanking / no cursor
xset s off -dpms s noblank 2>/dev/null || true
command -v unclutter >/dev/null 2>&1 && unclutter -idle 3 &

set_max_volume() {
    # The practice page fixes its own bell mix at 100%; also make the dedicated
    # appliance's current default sink audible after reboot or speaker changes.
    if command -v wpctl >/dev/null 2>&1; then
        wpctl set-mute @DEFAULT_AUDIO_SINK@ 0 >/dev/null 2>&1 || true
        wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0 >/dev/null 2>&1 || true
    elif command -v pactl >/dev/null 2>&1; then
        pactl set-sink-mute @DEFAULT_SINK@ 0 >/dev/null 2>&1 || true
        pactl set-sink-volume @DEFAULT_SINK@ 100% >/dev/null 2>&1 || true
    elif command -v amixer >/dev/null 2>&1; then
        amixer -q sset Master 100% unmute >/dev/null 2>&1 || true
    fi
}

launch() {
    local url="$1"
    set_max_volume
    # clear stale singleton locks so a hard power-off doesn't block startup
    rm -f "$HOME/.marksys-chrome/Singleton"* 2>/dev/null || true
    "$CHROME" \
        --kiosk \
        --noerrdialogs --disable-infobars --no-first-run \
        --disable-session-crashed-bubble --hide-crash-restore-bubble \
        --disable-features=TranslateUI \
        --check-for-update-interval=31536000 \
        --autoplay-policy=no-user-gesture-required \
        --auto-accept-camera-and-microphone-capture \
        --user-data-dir="$HOME/.marksys-chrome" \
        "$url" || true
}

pick_mode() {
    local args=(--list --radiolist
        --title="聖呂中辯電子系統"
        --text="揀模式："
        --width=440 --height=280 --hide-header
        --column="" --column="k" --column="模式" --print-column=2
        TRUE  practice "🟢   日常練習（學生 / 教練）"
        FALSE contest  "🔴   比賽日（主席）"
        FALSE update   "🔄   更新系統（拉取最新版本）")
    if [ "$CHOOSER_TIMEOUT" -gt 0 ]; then
        timeout "$CHOOSER_TIMEOUT" zenity "${args[@]}" 2>/dev/null || echo ""
    else
        zenity "${args[@]}" 2>/dev/null || echo ""
    fi
}

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -n "$SCRIPT_DIR" ] || SCRIPT_DIR="/opt/skhlmc-dbt-marksys/appliance"

run_update() {
    ( zenity --info --title="更新系統" --width=360 \
        --text="正在更新，請稍候…\n完成後介面會自動重新啟動。" --timeout=4 2>/dev/null || true ) &
    "$SCRIPT_DIR/update.sh" >/tmp/marksys-update.log 2>&1
    # Drop the X session; agetty autologin + ~/.bash_profile relaunch startx
    # with the freshly pulled scripts.
    pkill -x xinit 2>/dev/null || pkill -x Xorg 2>/dev/null || pkill -x X 2>/dev/null
}

while true; do
    mode="$(pick_mode)"
    case "$mode" in
        contest) launch "$CONTEST_URL" ;;
        update)  run_update; exit 0 ;;
        *)       launch "$PRACTICE_URL" ;;   # practice / cancel / timeout
    esac
    # Chromium exited — loop back to the chooser.
done
