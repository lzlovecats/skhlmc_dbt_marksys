#!/bin/bash
set -euo pipefail

echo "LMC AI Workstation diagnostic (no secrets, prompts, transcripts or signed URLs)"
echo "Timestamp: $(date --iso-8601=seconds)"
echo "OS: $(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")"
echo "Kernel: $(uname -r)"
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used,temperature.gpu \
  --format=csv,noheader || true
systemctl is-active lmc-ai-privileged.service lmc-ai-manager.service \
  lmc-ai-node.service lmc-ai-gui.service ollama.service \
  skhlmc-lmc-ai-node.service || true
systemctl list-timers --no-pager 'lmc-ai-*' || true
tailscale status --json 2>/dev/null | python3 -c \
  'import json,sys; x=json.load(sys.stdin); print({"BackendState":x.get("BackendState"),"TailscaleIPs":x.get("TailscaleIPs"),"SelfOnline":(x.get("Self") or {}).get("Online")})' \
  || true
ss -ltnH | awk '$4 ~ /:(22|3389|11434|8765|9880)$/ {print $4}' || true
df -h /srv/lmc-ai /var/lib/lmc-ai-workstation || true
/usr/bin/python3 -m workstation.scripts.workstationctl status || true
