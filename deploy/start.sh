#!/usr/bin/env bash
set -euo pipefail

PROXY_PORT="${PORT:-8000}"
read -r LIMIT_CONCURRENCY WS_MAX_SIZE MALLOC_ARENA_LIMIT MALLOC_TRIM_LIMIT \
    <<<"$(python system_limits.py --startup)"

if [ -f /etc/secrets/secrets.toml ]; then
    mkdir -p .streamlit
    cp /etc/secrets/secrets.toml .streamlit/secrets.toml
fi

# Memory tuning for the 512 MB instance.
# Python's threaded runtime makes glibc malloc spawn one arena per thread
# (default 8 × CPU cores), which fragments the heap and holds a high steady
# RSS even when idle. Capping arenas and lowering the trim threshold lets
# freed memory return to the OS — the single biggest lever on baseline RSS.
export MALLOC_ARENA_MAX="$MALLOC_ARENA_LIMIT"
export MALLOC_TRIM_THRESHOLD_="$MALLOC_TRIM_LIMIT"

exec uvicorn deploy.proxy:app --host 0.0.0.0 --port "$PROXY_PORT" \
    --limit-concurrency "$LIMIT_CONCURRENCY" \
    --ws-max-size "$WS_MAX_SIZE"
