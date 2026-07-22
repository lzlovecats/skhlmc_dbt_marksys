#!/bin/bash
set -euo pipefail

failures=0

check_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "FAIL missing command: $command_name"
    failures=$((failures + 1))
  fi
}

if [[ ! -r /etc/os-release ]]; then
  echo "FAIL /etc/os-release unavailable"
  failures=$((failures + 1))
else
  source /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    echo "FAIL requires Ubuntu 24.04 LTS"
    failures=$((failures + 1))
  fi
fi

for required in nvidia-smi ffmpeg ffprobe ollama rtcwake systemd-inhibit ss tailscale ufw grdctl apt-config findmnt lsblk; do
  check_command "$required"
done

if command -v findmnt >/dev/null 2>&1 && command -v lsblk >/dev/null 2>&1; then
  data_source="$(findmnt -n -o SOURCE --target /srv/lmc-ai 2>/dev/null || true)"
  data_source="${data_source%%\[*}"
  if [[ -z "$data_source" ]]; then
    echo "FAIL unable to resolve the Workstation data filesystem"
    failures=$((failures + 1))
  elif lsblk -s -n -o TYPE "$data_source" | grep -qx crypt; then
    echo "FAIL v1 unattended cold boot is incompatible with encrypted data filesystem"
    failures=$((failures + 1))
  fi
fi

if command -v apt-config >/dev/null 2>&1; then
  apt_policy="$(apt-config dump)"
  for required_policy in \
    'APT::Periodic::Update-Package-Lists "1";' \
    'APT::Periodic::Unattended-Upgrade "1";' \
    'Unattended-Upgrade::Automatic-Reboot "false";'; do
    if ! grep -Fqx "$required_policy" <<<"$apt_policy"; then
      echo "FAIL unattended security-update policy is not effective"
      failures=$((failures + 1))
    fi
  done
  for required_origin in \
    '"${distro_id}:${distro_codename}"' \
    '"${distro_id}:${distro_codename}-security"' \
    '"${distro_id}ESMApps:${distro_codename}-apps-security"' \
    '"${distro_id}ESM:${distro_codename}-infra-security"'; do
    if ! grep -Fq "$required_origin" <<<"$apt_policy"; then
      echo "FAIL required Ubuntu security origin is not effective"
      failures=$((failures + 1))
    fi
  done
  if grep -Eq '^Unattended-Upgrade::(Allowed-Origins|Origins-Pattern).*(-(updates|proposed|backports)|LP-PPA)' <<<"$apt_policy"; then
    echo "FAIL non-security unattended-upgrade origin is enabled"
    failures=$((failures + 1))
  fi
fi
for apt_timer in apt-daily.timer apt-daily-upgrade.timer; do
  if ! systemctl is-enabled --quiet "$apt_timer"; then
    echo "FAIL Ubuntu security-update timer is not enabled: $apt_timer"
    failures=$((failures + 1))
  fi
done

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
fi

if command -v ss >/dev/null 2>&1; then
  rdp_listener=0
  while read -r listener; do
    address="$(awk '{print $4}' <<<"$listener")"
    port="${address##*:}"
    case "$port" in
      3389)
        rdp_listener=1
        ;;
      11434|8765|9880)
        if [[ "$address" != 127.0.0.1:* && "$address" != \[::1\]:* ]]; then
          echo "FAIL localhost service exposed: $address"
          failures=$((failures + 1))
        fi
        ;;
    esac
  done < <(ss -ltnH)
  if (( rdp_listener == 0 )); then
    echo "FAIL GNOME Remote Login is not listening on RDP port 3389"
    failures=$((failures + 1))
  fi
fi

if command -v tailscale >/dev/null 2>&1; then
  if ! tailscale status --json | python3 -c '
import json,sys
value=json.load(sys.stdin)
if value.get("BackendState") != "Running" or not (value.get("TailscaleIPs") or []):
    raise SystemExit(1)
'; then
    echo "FAIL Tailscale is not authenticated"
    failures=$((failures + 1))
  fi
  if ! tailscale debug prefs | python3 -c '
import json,sys
value=json.load(sys.stdin)
if value.get("RunSSH") is not True:
    raise SystemExit(1)
'; then
    echo "FAIL Tailscale SSH is not enabled"
    failures=$((failures + 1))
  fi
fi

if command -v rtcwake >/dev/null 2>&1; then
  if ! rtcwake --mode show >/dev/null; then
    echo "FAIL RTC wake capability probe failed"
    failures=$((failures + 1))
  fi
fi
if [[ ! -r /sys/power/state ]] || ! grep -qw mem /sys/power/state; then
  echo "FAIL system suspend-to-RAM is unavailable"
  failures=$((failures + 1))
fi

if command -v ufw >/dev/null 2>&1; then
  ufw_status="$(LC_ALL=C ufw status verbose)"
  if ! grep -q "Status: active" <<<"$ufw_status"; then
    echo "FAIL UFW is not active"
    failures=$((failures + 1))
  fi
  if ! grep -q "Default: deny (incoming)" <<<"$ufw_status"; then
    echo "FAIL UFW incoming default is not deny"
    failures=$((failures + 1))
  fi
  if ! grep -Eq '3389(/tcp)? on tailscale0[[:space:]]+ALLOW IN' <<<"$ufw_status"; then
    echo "FAIL RDP is not explicitly limited to tailscale0"
    failures=$((failures + 1))
  fi
  while IFS= read -r rule; do
    if [[ "$rule" != *tailscale0* ]]; then
      echo "FAIL remote-access port allowed outside tailscale0: $rule"
      failures=$((failures + 1))
    fi
  done < <(grep -E '(^|[[:space:]])(22|3389)(/tcp)?([[:space:]]|$).*ALLOW IN' <<<"$ufw_status" || true)
fi

if (( failures > 0 )); then
  echo "Preflight failed: $failures gate(s)"
  exit 1
fi

echo "Ubuntu base/security preflight passed. Run workstationctl health for workload probes."
