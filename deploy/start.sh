#!/usr/bin/env bash
set -euo pipefail

STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
PROXY_PORT="${PORT:-8000}"

if [ -f /etc/secrets/secrets.toml ]; then
    mkdir -p .streamlit
    cp /etc/secrets/secrets.toml .streamlit/secrets.toml
fi

# Memory tuning for the 512 MB instance.
# Python's threaded runtime makes glibc malloc spawn one arena per thread
# (default 8 × CPU cores), which fragments the heap and holds a high steady
# RSS even when idle. Capping arenas and lowering the trim threshold lets
# freed memory return to the OS — the single biggest lever on baseline RSS.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-65536}"

streamlit run main.py \
    --server.port "$STREAMLIT_PORT" \
    --server.address 127.0.0.1 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false \
    --server.fileWatcherType none \
    --server.maxUploadSize 30 \
    --browser.gatherUsageStats false &

exec uvicorn deploy.proxy:app --host 0.0.0.0 --port "$PROXY_PORT"
