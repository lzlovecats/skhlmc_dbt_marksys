#!/usr/bin/env bash
set -euo pipefail

STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
PROXY_PORT="${PORT:-8000}"

if [ -f /etc/secrets/secrets.toml ]; then
    mkdir -p .streamlit
    cp /etc/secrets/secrets.toml .streamlit/secrets.toml
fi

streamlit run main.py \
    --server.port "$STREAMLIT_PORT" \
    --server.address 127.0.0.1 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false &

exec uvicorn deploy.proxy:app --host 0.0.0.0 --port "$PROXY_PORT"
