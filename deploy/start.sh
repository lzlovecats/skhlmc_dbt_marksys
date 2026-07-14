#!/usr/bin/env bash
set -euo pipefail

PROXY_PORT="${PORT:-8000}"
UVICORN_LIMIT_CONCURRENCY=""
UVICORN_WS_MAX_SIZE=""
UVICORN_WS_MAX_QUEUE=""
MALLOC_ARENA_LIMIT=""
MALLOC_TRIM_LIMIT=""
while IFS='=' read -r name value; do
    case "$name" in
        UVICORN_LIMIT_CONCURRENCY) UVICORN_LIMIT_CONCURRENCY="$value" ;;
        UVICORN_WS_MAX_SIZE) UVICORN_WS_MAX_SIZE="$value" ;;
        UVICORN_WS_MAX_QUEUE) UVICORN_WS_MAX_QUEUE="$value" ;;
        MALLOC_ARENA_MAX) MALLOC_ARENA_LIMIT="$value" ;;
        MALLOC_TRIM_THRESHOLD_) MALLOC_TRIM_LIMIT="$value" ;;
        *) echo "Unknown system limit in startup contract: $name" >&2; exit 1 ;;
    esac
done < <(python system_limits.py --startup)

: "${UVICORN_LIMIT_CONCURRENCY:?missing UVICORN_LIMIT_CONCURRENCY}"
: "${UVICORN_WS_MAX_SIZE:?missing UVICORN_WS_MAX_SIZE}"
: "${UVICORN_WS_MAX_QUEUE:?missing UVICORN_WS_MAX_QUEUE}"
: "${MALLOC_ARENA_LIMIT:?missing MALLOC_ARENA_MAX}"
: "${MALLOC_TRIM_LIMIT:?missing MALLOC_TRIM_THRESHOLD_}"

# Memory tuning for the 512 MB instance.
# Python's threaded runtime makes glibc malloc spawn one arena per thread
# (default 8 × CPU cores), which fragments the heap and holds a high steady
# RSS even when idle. Capping arenas and lowering the trim threshold lets
# freed memory return to the OS — the single biggest lever on baseline RSS.
export MALLOC_ARENA_MAX="$MALLOC_ARENA_LIMIT"
export MALLOC_TRIM_THRESHOLD_="$MALLOC_TRIM_LIMIT"

exec uvicorn deploy.proxy:app --host 0.0.0.0 --port "$PROXY_PORT" \
    --limit-concurrency "$UVICORN_LIMIT_CONCURRENCY" \
    --ws-max-size "$UVICORN_WS_MAX_SIZE" \
    --ws-max-queue "$UVICORN_WS_MAX_QUEUE"
