"""Single source of truth for production resource and quota limits.

Every operational limit that protects Render, Supabase, Cloudflare R2 or an AI
provider belongs here.  Modules import the resolved constants; environment
variables remain optional overrides, so existing Render settings continue to
work.  Field-format validation (for example phone-number length) stays beside
its Pydantic model because it is an API contract, not a system resource limit.

Run ``python system_limits.py --json`` to inspect the effective values that a
new worker will use.  Values are read once at process startup; changing a
Render environment variable therefore requires a service restart/redeploy.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass

KIB = 1024
MIB = 1024 * KIB
GB = 1_000_000_000


@dataclass(frozen=True)
class LimitSpec:
    name: str
    value: int
    default: int
    minimum: int
    maximum: int | None
    group: str
    description: str


LIMIT_SPECS: dict[str, LimitSpec] = {}


def _limit(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    group: str,
    description: str,
) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw not in (None, "") else int(default)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    LIMIT_SPECS[name] = LimitSpec(
        name=name, value=value, default=int(default), minimum=int(minimum),
        maximum=int(maximum) if maximum is not None else None,
        group=group, description=description,
    )
    return value


# Render process, RAM and database connections.
UVICORN_LIMIT_CONCURRENCY = _limit("UVICORN_LIMIT_CONCURRENCY", 20, minimum=1, group="runtime", description="Uvicorn concurrent requests")
UVICORN_WS_MAX_SIZE = _limit("UVICORN_WS_MAX_SIZE", 2 * MIB, minimum=64 * KIB, group="runtime", description="Incoming WebSocket frame bytes")
MALLOC_ARENA_MAX = _limit("MALLOC_ARENA_MAX", 2, minimum=1, group="runtime", description="glibc malloc arenas")
MALLOC_TRIM_THRESHOLD_BYTES = _limit("MALLOC_TRIM_THRESHOLD_", 64 * KIB, minimum=0, group="runtime", description="glibc trim threshold bytes")
MAX_HTTP_BODY_BYTES = _limit("MAX_HTTP_BODY_BYTES", 5 * MIB, minimum=KIB, group="runtime", description="Actual streamed HTTP request-body bytes")
REQUEST_BODY_BUFFER_CONCURRENCY = _limit("REQUEST_BODY_BUFFER_CONCURRENCY", 4, minimum=1, group="runtime", description="Simultaneous buffered request bodies")
DB_POOL_SIZE = _limit("DB_POOL_SIZE", 3, minimum=1, group="runtime", description="Persistent SQLAlchemy pool connections")
DB_MAX_OVERFLOW = _limit("DB_MAX_OVERFLOW", 2, minimum=0, group="runtime", description="Temporary SQLAlchemy overflow connections")
DB_POOL_TIMEOUT = _limit("DB_POOL_TIMEOUT", 10, minimum=1, group="runtime", description="DB pool checkout timeout seconds")
DB_POOL_RECYCLE = _limit("DB_POOL_RECYCLE", 300, minimum=30, group="runtime", description="DB connection recycle seconds")
GZIP_MINIMUM_SIZE = _limit("GZIP_MINIMUM_SIZE", KIB, minimum=256, group="runtime", description="Minimum response bytes before gzip")
GZIP_COMPRESS_LEVEL = _limit("GZIP_COMPRESS_LEVEL", 5, minimum=1, maximum=9, group="runtime", description="gzip compression level")
MAINTENANCE_PRUNE_INTERVAL_SECONDS = _limit("MAINTENANCE_PRUNE_INTERVAL_SECONDS", 86_400, minimum=300, group="runtime", description="Minimum interval between lazy retention sweeps")
ROOM_WS_SEND_TIMEOUT_SECONDS = _limit("ROOM_WS_SEND_TIMEOUT_SECONDS", 1, minimum=1, maximum=10, group="runtime", description="Per-member room WebSocket send timeout")
CACHE_HTML_MAX_AGE_SECONDS = _limit("CACHE_HTML_MAX_AGE_SECONDS", 300, minimum=0, group="bandwidth", description="Browser/edge HTML cache lifetime")
CACHE_HTML_STALE_SECONDS = _limit("CACHE_HTML_STALE_SECONDS", 3_600, minimum=0, group="bandwidth", description="HTML stale-while-revalidate lifetime")
CACHE_MANIFEST_MAX_AGE_SECONDS = _limit("CACHE_MANIFEST_MAX_AGE_SECONDS", 86_400, minimum=0, group="bandwidth", description="Web manifest cache lifetime")
CACHE_STATIC_MAX_AGE_SECONDS = _limit("CACHE_STATIC_MAX_AGE_SECONDS", 31_536_000, minimum=0, group="bandwidth", description="Versioned static asset cache lifetime")
CACHE_SHARED_MAX_AGE_SECONDS = _limit("CACHE_SHARED_MAX_AGE_SECONDS", 86_400, minimum=0, group="bandwidth", description="Shared public response cache lifetime")
CACHE_SHARED_STALE_SECONDS = _limit("CACHE_SHARED_STALE_SECONDS", 604_800, minimum=0, group="bandwidth", description="Shared response stale-while-revalidate lifetime")

# Shared API, pagination and privileged console.
API_PAGE_SIZE = _limit("API_PAGE_SIZE", 20, minimum=1, maximum=200, group="api", description="Rows per server-paginated page")
EXPORT_MAX_ROWS = _limit("EXPORT_MAX_ROWS", 5_000, minimum=1, group="api", description="Rows per CSV or JSONL export")
EXPORT_MAX_BYTES = _limit("EXPORT_MAX_BYTES", 5 * MIB, minimum=KIB, group="api", description="Bytes per CSV or JSONL export")
OPEN_DB_CACHE_TTL_SECONDS = _limit("OPEN_DB_CACHE_TTL_SECONDS", 60, minimum=1, group="api", description="Public topic-bank memory-cache TTL")
OPEN_DB_STALE_REVALIDATE_SECONDS = _limit("OPEN_DB_STALE_REVALIDATE_SECONDS", 300, minimum=1, group="api", description="Public topic-bank stale revalidation window")
ADMIN_SESSION_TTL_SECONDS = _limit("ADMIN_SESSION_TTL_SECONDS", 4 * 60 * 60, minimum=60, group="api", description="Developer/admin session TTL")
MAX_ADMIN_CONSOLE_SESSIONS = _limit("MAX_ADMIN_CONSOLE_SESSIONS", 256, minimum=16, group="api", description="In-memory privileged console sessions")
ADMIN_RECENT_LOGIN_LIMIT = _limit("ADMIN_RECENT_LOGIN_LIMIT", 50, minimum=1, group="api", description="Recent logins returned on admin overview")
SQL_RESULT_MAX_ROWS = _limit("SQL_RESULT_MAX_ROWS", 500, minimum=1, group="api", description="SQL-console result rows")
SQL_RESULT_MAX_BYTES = _limit("SQL_RESULT_MAX_BYTES", MIB, minimum=KIB, group="api", description="SQL-console serialized bytes")
SQL_RESULT_MAX_CELL_CHARS = _limit("SQL_RESULT_MAX_CELL_CHARS", 10_000, minimum=128, group="api", description="SQL-console characters per cell")
SQL_STATEMENT_TIMEOUT_MS = _limit("SQL_STATEMENT_TIMEOUT_MS", 10_000, minimum=1_000, group="api", description="SQL-console statement timeout")

# Render monthly bandwidth and Live WebSocket protection.
BANDWIDTH_WARN_BYTES = _limit("BANDWIDTH_WARN_BYTES", 3 * GB, minimum=MIB, group="bandwidth", description="Monthly warning threshold")
BANDWIDTH_STOP_LIVE_BYTES = _limit("BANDWIDTH_STOP_LIVE_BYTES", 3_500_000_000, minimum=MIB, group="bandwidth", description="Monthly active/new Live hard gate")
BANDWIDTH_ESSENTIAL_ONLY_BYTES = _limit("BANDWIDTH_ESSENTIAL_ONLY_BYTES", 4 * GB, minimum=MIB, group="bandwidth", description="Monthly nonessential feature hard gate")
BANDWIDTH_CHECKPOINT_SECONDS = _limit("BANDWIDTH_CHECKPOINT_SECONDS", 30, minimum=10, group="bandwidth", description="Live usage checkpoint interval")
BANDWIDTH_LOG_RETENTION_DAYS = _limit("BANDWIDTH_LOG_RETENTION_DAYS", 62, minimum=31, group="bandwidth", description="Bandwidth log retention")
GEMINI_WS_MAX_SIZE = _limit("GEMINI_WS_MAX_SIZE", 4 * MIB, minimum=64 * KIB, group="bandwidth", description="Gemini upstream WebSocket frame bytes")
GEMINI_RELAY_MAX_BYTES = _limit("GEMINI_RELAY_MAX_BYTES", 96 * MIB, minimum=MIB, group="bandwidth", description="Bytes per solo Gemini relay")
GEMINI_RELAY_SIGNATURE_TTL_SECONDS = _limit("GEMINI_RELAY_SIGNATURE_TTL_SECONDS", 2 * 60 * 60, minimum=60, group="bandwidth", description="Signed Gemini relay claim lifetime")
GEMINI_RELAY_MIN_SECONDS = _limit("GEMINI_RELAY_MIN_SECONDS", 30, minimum=10, maximum=60, group="bandwidth", description="Minimum signed relay session duration")
GEMINI_RELAY_MAX_SECONDS = _limit("GEMINI_RELAY_MAX_SECONDS", 30 * 60, minimum=60, maximum=60 * 60, group="bandwidth", description="Maximum signed relay session duration")

# Live practice rooms, quotas and in-memory history.
SOLO_FREE_MONTHLY_LIMIT = _limit("SOLO_FREE_MONTHLY_LIMIT", 20, minimum=1, group="live", description="System solo Free De sessions per month")
SOLO_MOCK_MONTHLY_LIMIT = _limit("SOLO_MOCK_MONTHLY_LIMIT", 10, minimum=1, group="live", description="System solo Mock sessions per month")
MULTIPLAYER_FREE_MONTHLY_ROOMS = _limit("MULTIPLAYER_FREE_MONTHLY_ROOMS", 20, minimum=1, group="live", description="System multiplayer Free De rooms per month")
MULTIPLAYER_MOCK_MONTHLY_ROOMS = _limit("MULTIPLAYER_MOCK_MONTHLY_ROOMS", 10, minimum=1, group="live", description="System multiplayer Mock rooms per month")
MAX_ROOMS = _limit("MAX_ROOMS", 2, minimum=1, group="live", description="Concurrent in-memory rooms")
ROOM_MAX_CAPACITY = _limit("ROOM_MAX_CAPACITY", 4, minimum=4, maximum=8, group="live", description="Members per room")
ROOM_EMPTY_GRACE_SECONDS = _limit("ROOM_EMPTY_GRACE_SECONDS", 60, minimum=10, group="live", description="Empty-room reconnect grace")
ROOM_MAX_AGE_SECONDS = _limit("ROOM_MAX_AGE_SECONDS", 90 * 60, minimum=60, group="live", description="Room hard TTL")
ROOM_TRANSCRIPT_MAX_ITEMS = _limit("ROOM_TRANSCRIPT_MAX_ITEMS", 80, minimum=10, group="live", description="Transcript items retained in RAM")
ROOM_TRANSCRIPT_ITEM_MAX_CHARS = _limit("ROOM_TRANSCRIPT_ITEM_MAX_CHARS", 2_000, minimum=100, group="live", description="Characters per transcript item")
ROOM_NATIVE_AUDIO_BUFFER_MAX_BYTES = _limit("ROOM_NATIVE_AUDIO_BUFFER_MAX_BYTES", 8 * MIB, minimum=64 * KIB, group="live", description="Native-audio fallback buffer per room")
ROOM_PENDING_TRANSCRIPT_MAX_CHARS = _limit("ROOM_PENDING_TRANSCRIPT_MAX_CHARS", 8_000, minimum=500, group="live", description="Pending AI transcript characters")
PRACTICE_LIVE_MAX_PER_HOUR = _limit("PRACTICE_LIVE_MAX_PER_HOUR", 30, minimum=1, group="live", description="Token-mint requests per user/hour")
PRACTICE_LIVE_MIN_GAP_SECONDS = _limit("PRACTICE_LIVE_MIN_GAP_SECONDS", 3, minimum=1, group="live", description="Minimum seconds between token mints")
PRACTICE_LIVE_RATE_WINDOW_SECONDS = _limit("PRACTICE_LIVE_RATE_WINDOW_SECONDS", 60 * 60, minimum=60, group="live", description="Token-mint rolling quota window")
LIVE_FREE_MAX_MINUTES = _limit("LIVE_FREE_MAX_MINUTES", 10, minimum=1, maximum=30, group="live", description="Server-authoritative Free De duration")
PROJECTOR_MATCH_LIMIT = _limit("PROJECTOR_MATCH_LIMIT", 200, minimum=1, group="live", description="Matches exposed to projector")

# TTS, AI Coach, training, RAG and provider prompt protection.
TTS_CONCURRENCY = _limit("TTS_CONCURRENCY", 2, minimum=1, group="ai", description="Concurrent TTS synthesis")
TTS_MAX_RESPONSE_BYTES = _limit("TTS_MAX_RESPONSE_BYTES", 4 * MIB, minimum=64 * KIB, group="ai", description="TTS response bytes")
TTS_TEXT_MAX_CHARS = _limit("TTS_TEXT_MAX_CHARS", 1_200, minimum=100, group="ai", description="Characters per TTS synthesis request")
TTS_LEXICON_LIMIT = _limit("TTS_LEXICON_LIMIT", 2_000, minimum=100, group="ai", description="TTS lexicon entries loaded")
TTS_LEXICON_CACHE_TTL_SECONDS = _limit("TTS_LEXICON_CACHE_TTL_SECONDS", 60, minimum=1, group="ai", description="TTS lexicon RAM-cache lifetime")
MODEL_DEPLOYABLE_CACHE_TTL_SECONDS = _limit("MODEL_DEPLOYABLE_CACHE_TTL_SECONDS", 60, minimum=1, group="ai", description="Deployable-model RAM-cache lifetime")
TTS_PROVIDER_TIMEOUT_SECONDS = _limit("TTS_PROVIDER_TIMEOUT_SECONDS", 30, minimum=1, group="ai", description="TTS provider request timeout")
TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS = _limit("TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS", 5, minimum=1, group="ai", description="Custom TTS connection timeout")
AI_TRAINING_PROVIDER_TIMEOUT_SECONDS = _limit("AI_TRAINING_PROVIDER_TIMEOUT_SECONDS", 60, minimum=1, group="ai", description="AI training provider request timeout")
AI_PROVIDER_OPENROUTER_TIMEOUT_SECONDS = _limit("AI_PROVIDER_OPENROUTER_TIMEOUT_SECONDS", 70, minimum=1, group="ai", description="OpenRouter generation timeout")
AI_PROVIDER_GEMINI_TIMEOUT_SECONDS = _limit("AI_PROVIDER_GEMINI_TIMEOUT_SECONDS", 90, minimum=1, group="ai", description="Gemini generation timeout")
AI_PROVIDER_RESPONSE_MAX_BYTES = _limit("AI_PROVIDER_RESPONSE_MAX_BYTES", 2 * MIB, minimum=64 * KIB, group="ai", description="Provider JSON response bytes buffered in RAM")
RAG_PROVIDER_TIMEOUT_SECONDS = _limit("RAG_PROVIDER_TIMEOUT_SECONDS", 30, minimum=1, group="ai", description="RAG embedding provider timeout")
ROOM_JUDGEMENT_TIMEOUT_SECONDS = _limit("ROOM_JUDGEMENT_TIMEOUT_SECONDS", 45, minimum=1, group="ai", description="Room judgement provider timeout")
MEDIA_PROBE_TIMEOUT_SECONDS = _limit("MEDIA_PROBE_TIMEOUT_SECONDS", 10, minimum=1, group="ai", description="ffprobe recording inspection timeout")
MAX_AUDIO_BYTES = _limit("MAX_AUDIO_BYTES", 2 * MIB, minimum=KIB, group="ai", description="TTS training recording bytes")
TTS_MAX_DURATION_SECONDS = _limit("TTS_MAX_DURATION_SECONDS", 60, minimum=1, maximum=300, group="ai", description="TTS/AI recording duration")
TTS_UPLOAD_INTENTS_PER_USER_DAY = _limit("TTS_UPLOAD_INTENTS_PER_USER_DAY", 30, minimum=1, group="ai", description="TTS upload intents per user/day")
TTS_UPLOAD_INTENTS_GLOBAL_MONTH = _limit("TTS_UPLOAD_INTENTS_GLOBAL_MONTH", 1_000, minimum=1, group="ai", description="TTS upload intents system/month")
TTS_REVIEW_CONCURRENCY = _limit("TTS_REVIEW_CONCURRENCY", 2, minimum=1, group="ai", description="Concurrent TTS quality reviews")
AI_COACH_MAX_AUDIO_BYTES = _limit("AI_COACH_MAX_AUDIO_BYTES", 2 * MIB, minimum=KIB, group="ai", description="AI Coach decoded audio bytes")
AI_COACH_MAX_AUDIO_SECONDS = _limit("AI_COACH_MAX_AUDIO_SECONDS", 60, minimum=1, maximum=300, group="ai", description="AI Coach browser recording duration")
AI_COACH_CONCURRENCY = _limit("AI_COACH_CONCURRENCY", 3, minimum=1, group="ai", description="Concurrent AI Coach requests")
PREPARE_LIVE_USER_HOURLY_LIMIT = _limit("PREPARE_LIVE_USER_HOURLY_LIMIT", 1, minimum=1, group="ai", description="Prepare-live calls per user/hour")
PREPARE_LIVE_USER_DAILY_LIMIT = _limit("PREPARE_LIVE_USER_DAILY_LIMIT", 3, minimum=1, group="ai", description="Prepare-live calls per user/day")
PREPARE_LIVE_USAGE_RETENTION_DAYS = _limit("PREPARE_LIVE_USAGE_RETENTION_DAYS", 2, minimum=1, group="ai", description="Prepare-live quota-row retention")
LIVE_BRIEF_TTL_MINUTES = _limit("LIVE_BRIEF_TTL_MINUTES", 15, minimum=1, group="ai", description="Prepared Live research brief TTL")
LIVE_BRIEF_MAX_CHARS = _limit("LIVE_BRIEF_MAX_CHARS", 4_500, minimum=500, group="ai", description="Prepared Live research brief characters")
AI_COACH_TOPIC_LIMIT = _limit("AI_COACH_TOPIC_LIMIT", 2_000, minimum=100, group="ai", description="Topics loaded by AI Coach")
AI_COACH_MATCH_LIMIT = _limit("AI_COACH_MATCH_LIMIT", 500, minimum=50, group="ai", description="Matches loaded by AI Coach")
LLM_CONTENT_MAX_CHARS = _limit("LLM_CONTENT_MAX_CHARS", 20_000, minimum=1_000, group="ai", description="Characters per LLM training submission")
LLM_SUBMISSIONS_PER_USER_DAY = _limit("LLM_SUBMISSIONS_PER_USER_DAY", 10, minimum=1, group="ai", description="LLM submissions per user/day")
LLM_SUBMISSION_MAX_TOTAL = _limit("LLM_SUBMISSION_MAX_TOTAL", 5_000, minimum=100, group="ai", description="LLM submissions retained")
LLM_REVIEW_CONCURRENCY = _limit("LLM_REVIEW_CONCURRENCY", 2, minimum=1, group="ai", description="Concurrent LLM reviews")
DATASET_SNAPSHOT_MAX_ITEMS = _limit("DATASET_SNAPSHOT_MAX_ITEMS", 500, minimum=1, group="ai", description="Items per dataset snapshot")
DATASET_SNAPSHOT_MAX_COUNT = _limit("DATASET_SNAPSHOT_MAX_COUNT", 200, minimum=10, group="ai", description="Dataset snapshots retained")
RECORDING_MANIFEST_MAX_ROWS = _limit("RECORDING_MANIFEST_MAX_ROWS", 2_000, minimum=1, group="ai", description="Rows in recording manifests")
RAG_REINDEX_MAX_DOCUMENTS = _limit("RAG_REINDEX_MAX_DOCUMENTS", 10, minimum=1, group="ai", description="RAG documents per reindex")
RAG_REINDEX_MAX_CHUNKS = _limit("RAG_REINDEX_MAX_CHUNKS", 100, minimum=1, group="ai", description="RAG chunks per reindex")
RAG_DOCUMENT_MAX_TOTAL = _limit("RAG_DOCUMENT_MAX_TOTAL", 1_000, minimum=100, group="ai", description="Active RAG documents")
RAG_EMBED_CONCURRENCY = _limit("RAG_EMBED_CONCURRENCY", 3, minimum=1, group="ai", description="Concurrent embedding requests")
RAG_FALLBACK_CANDIDATE_LIMIT = _limit("RAG_FALLBACK_CANDIDATE_LIMIT", 1_000, minimum=1, group="ai", description="RAG fallback candidates")
RAG_CONTEXT_MAX_CHARS = _limit("RAG_CONTEXT_MAX_CHARS", 12_000, minimum=1_000, group="ai", description="RAG context characters appended to a provider prompt")
AI_TRAINING_INVENTORY_LIMIT = _limit("AI_TRAINING_INVENTORY_LIMIT", 2_000, minimum=100, group="ai", description="TTS training scripts retained")
AI_TRAINING_JSON_MAX_BYTES = _limit("AI_TRAINING_JSON_MAX_BYTES", 100 * KIB, minimum=KIB, group="ai", description="AI training JSON column bytes")
AI_EVAL_CASE_LIMIT = _limit("AI_EVAL_CASE_LIMIT", 200, minimum=30, group="ai", description="AI evaluation cases")
AI_MODEL_VERSION_LIMIT = _limit("AI_MODEL_VERSION_LIMIT", 200, minimum=10, group="ai", description="AI model versions retained")
TTS_AI_ANALYSIS_SCRIPT_LIMIT = _limit("TTS_AI_ANALYSIS_SCRIPT_LIMIT", 500, minimum=50, group="ai", description="Scripts per AI coverage analysis")
AI_TRAINING_PROMPT_MAX_CHARS = _limit("AI_TRAINING_PROMPT_MAX_CHARS", 60_000, minimum=10_000, group="ai", description="AI training prompt characters")
AI_TRAINING_AUDIT_RETENTION_DAYS = _limit("AI_TRAINING_AUDIT_RETENTION_DAYS", 400, minimum=90, group="ai", description="AI training audit retention")
AI_TRAINING_ADMIN_PAGE_SIZE = _limit("AI_TRAINING_ADMIN_PAGE_SIZE", 5, minimum=1, maximum=100, group="ai", description="AI training admin rows/page")
AI_TRAINING_READINESS_GROUP_LIMIT = _limit("AI_TRAINING_READINESS_GROUP_LIMIT", 200, minimum=1, group="ai", description="Readiness aggregate groups")
AI_SUGGESTION_BATCH_MAX = _limit("AI_SUGGESTION_BATCH_MAX", 50, minimum=1, maximum=500, group="ai", description="AI script suggestions applied per request")
AI_PROVIDER_PROMPT_MAX_CHARS = _limit("AI_PROVIDER_PROMPT_MAX_CHARS", 60_000, minimum=4_000, group="ai", description="Provider prompt characters")
AI_PROVIDER_MAX_OUTPUT_TOKENS = _limit("AI_PROVIDER_MAX_OUTPUT_TOKENS", 4_096, minimum=256, group="ai", description="Provider output tokens")
AI_PROVIDER_SOURCE_LIMIT = _limit("AI_PROVIDER_SOURCE_LIMIT", 20, minimum=1, group="ai", description="Grounded sources returned")
DATASET_ARCHIVE_MAX_ITEMS = _limit("DATASET_ARCHIVE_MAX_ITEMS", 5_000, minimum=100, group="ai", description="Files accepted from an offline training archive")
DATASET_ARCHIVE_MAX_BYTES = _limit("DATASET_ARCHIVE_MAX_BYTES", 10 * GB, minimum=MIB, group="ai", description="Uncompressed bytes accepted from an offline training archive")
DATASET_MANIFEST_MAX_BYTES = _limit("DATASET_MANIFEST_MAX_BYTES", 10 * MIB, minimum=64 * KIB, group="ai", description="Offline recordings manifest bytes loaded in RAM")
DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS = _limit("DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS", 120, minimum=10, group="ai", description="Offline ffmpeg or ffprobe timeout per training clip")
VOTE_AI_PROMPT_MAX_CHARS = _limit("VOTE_AI_PROMPT_MAX_CHARS", 60_000, minimum=4_000, group="ai", description="Vote-AI prompt characters")
VOTE_AI_MAX_OUTPUT_TOKENS = _limit("VOTE_AI_MAX_OUTPUT_TOKENS", 2_048, minimum=256, group="ai", description="Vote-AI output tokens")
VOTE_AI_TOPIC_SAMPLE_LIMIT = _limit("VOTE_AI_TOPIC_SAMPLE_LIMIT", 500, minimum=50, group="ai", description="Topics sampled for Vote AI")
VOTE_AI_DISCUSSION_COMMENT_LIMIT = _limit("VOTE_AI_DISCUSSION_COMMENT_LIMIT", 30, minimum=1, group="ai", description="Comments sent to Vote AI")
VOTE_AI_CATEGORY_EXAMPLE_LIMIT = _limit("VOTE_AI_CATEGORY_EXAMPLE_LIMIT", 15, minimum=1, group="ai", description="Category examples sent to Vote AI")

# Cloudflare R2, photos and upload lifecycle.
R2_STORAGE_WARN_BYTES = _limit("R2_STORAGE_WARN_BYTES", 7 * GB, minimum=MIB, group="storage", description="R2 warning threshold")
R2_STORAGE_STOP_BYTES = _limit("R2_STORAGE_STOP_BYTES", 8 * GB, minimum=MIB, group="storage", description="R2 new-upload hard gate")
R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS = _limit("R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS", 6 * 60 * 60, minimum=300, group="storage", description="R2 exact-usage snapshot TTL")
R2_INTENT_RETENTION_DAYS = _limit("R2_INTENT_RETENTION_DAYS", 90, minimum=30, group="storage", description="Completed/orphan upload-intent retention")
R2_ORPHAN_MIN_AGE_HOURS = _limit("R2_ORPHAN_MIN_AGE_HOURS", 48, minimum=24, group="storage", description="Minimum age before orphan cleanup")
R2_FINALIZER_BATCH_SIZE = _limit("R2_FINALIZER_BATCH_SIZE", 200, minimum=1, group="storage", description="Metadata rows per finalizer batch")
R2_ORPHAN_DRY_RUN_DISPLAY_LIMIT = _limit("R2_ORPHAN_DRY_RUN_DISPLAY_LIMIT", 100, minimum=1, group="storage", description="Orphans printed by a dry run")
R2_UPLOAD_URL_TTL_SECONDS = _limit("R2_UPLOAD_URL_TTL_SECONDS", 300, minimum=60, maximum=900, group="storage", description="Default direct-upload URL lifetime")
R2_DOWNLOAD_URL_TTL_SECONDS = _limit("R2_DOWNLOAD_URL_TTL_SECONDS", 600, minimum=60, maximum=3_600, group="storage", description="Default direct-download URL lifetime")
R2_MEDIA_LINK_TTL_SECONDS = _limit("R2_MEDIA_LINK_TTL_SECONDS", 1_800, minimum=60, maximum=3_600, group="storage", description="Authenticated photo/audio playback URL lifetime")
R2_BULK_LINK_TTL_SECONDS = _limit("R2_BULK_LINK_TTL_SECONDS", 3_600, minimum=60, maximum=3_600, group="storage", description="Bulk recording manifest URL lifetime")
R2_UPLOAD_CLAIM_TTL_SECONDS = _limit("R2_UPLOAD_CLAIM_TTL_SECONDS", 600, minimum=60, maximum=900, group="storage", description="Signed upload-claim lifetime")
TTS_REVIEW_CLAIM_TTL_SECONDS = _limit("TTS_REVIEW_CLAIM_TTL_SECONDS", 900, minimum=60, maximum=1_800, group="storage", description="Signed TTS quality-review claim lifetime")
R2_OBJECT_CACHE_MAX_AGE_SECONDS = _limit("R2_OBJECT_CACHE_MAX_AGE_SECONDS", 86_400, minimum=0, group="storage", description="Private R2 object cache metadata lifetime")
R2_URL_MIN_TTL_SECONDS = _limit("R2_URL_MIN_TTL_SECONDS", 60, minimum=1, maximum=60, group="storage", description="Minimum presigned URL lifetime")
R2_UPLOAD_URL_MAX_TTL_SECONDS = _limit("R2_UPLOAD_URL_MAX_TTL_SECONDS", 900, minimum=60, maximum=3_600, group="storage", description="Maximum presigned upload URL lifetime")
R2_DOWNLOAD_URL_MAX_TTL_SECONDS = _limit("R2_DOWNLOAD_URL_MAX_TTL_SECONDS", 3_600, minimum=60, maximum=86_400, group="storage", description="Maximum presigned download URL lifetime")
R2_CLAIM_MAX_TTL_SECONDS = _limit("R2_CLAIM_MAX_TTL_SECONDS", 1_800, minimum=60, maximum=3_600, group="storage", description="Maximum signed R2 claim lifetime")
R2_CLIENT_MAX_ATTEMPTS = _limit("R2_CLIENT_MAX_ATTEMPTS", 3, minimum=1, maximum=10, group="storage", description="R2 SDK request attempts")
PHOTO_DAILY_USER_LIMIT = _limit("PHOTO_DAILY_USER_LIMIT", 20, minimum=1, group="storage", description="Photos per user/day")
PHOTO_MONTHLY_GLOBAL_LIMIT = _limit("PHOTO_MONTHLY_GLOBAL_LIMIT", 500, minimum=1, group="storage", description="Photos system/month")
PHOTO_BATCH_MAX_ITEMS = _limit("PHOTO_BATCH_MAX_ITEMS", 5, minimum=1, maximum=20, group="storage", description="Photos per completion request")
PHOTO_MAX_BYTES = _limit("PHOTO_MAX_BYTES", 2 * MIB, minimum=KIB, group="storage", description="Compressed original photo bytes")
PHOTO_THUMBNAIL_MAX_BYTES = _limit("PHOTO_THUMBNAIL_MAX_BYTES", 300 * KIB, minimum=500, group="storage", description="Photo thumbnail bytes")
PHOTO_MAX_DIMENSION = _limit("PHOTO_MAX_DIMENSION", 2_000, minimum=100, group="storage", description="Photo width/height pixels")
PHOTO_THUMBNAIL_MAX_DIMENSION = _limit("PHOTO_THUMBNAIL_MAX_DIMENSION", 480, minimum=100, group="storage", description="Photo thumbnail width/height pixels")

# Supabase row inventories, retention and interaction growth.
ACCOUNT_INVENTORY_LIMIT = _limit("ACCOUNT_INVENTORY_LIMIT", 1_000, minimum=100, group="database", description="Accounts retained")
ACCOUNT_LIST_LIMIT = _limit("ACCOUNT_LIST_LIMIT", 1_000, minimum=100, group="database", description="Accounts loaded per list")
MATCH_INVENTORY_LIMIT = _limit("MATCH_INVENTORY_LIMIT", 500, minimum=100, group="database", description="Matches retained/loaded")
JUDGE_MAX_PER_MATCH = _limit("JUDGE_MAX_PER_MATCH", 50, minimum=5, group="database", description="Judges per match")
REGISTRATION_MAX_PER_EDITION = _limit("REGISTRATION_MAX_PER_EDITION", 500, minimum=1, group="database", description="Registrations per edition")
REGISTRATION_EDITION_HISTORY_LIMIT = _limit("REGISTRATION_EDITION_HISTORY_LIMIT", 100, minimum=1, group="database", description="Registration editions listed")
OPEN_DB_MAX_TOPICS = _limit("OPEN_DB_MAX_TOPICS", 2_000, minimum=100, group="database", description="Public topic-bank rows")
TOPIC_BANK_MAX = _limit("TOPIC_BANK_MAX", 2_000, minimum=100, group="database", description="Topic bank rows")
COMMENT_HISTORY_LIMIT = _limit("COMMENT_HISTORY_LIMIT", 100, minimum=1, group="database", description="Comments returned per motion")
COMMENT_MAX_PER_MOTION = _limit("COMMENT_MAX_PER_MOTION", 100, minimum=1, group="database", description="Comments retained per motion")
VOTE_ANALYSIS_MOTION_LIMIT = _limit("VOTE_ANALYSIS_MOTION_LIMIT", 200, minimum=1, group="database", description="Motions per vote analysis")
VOTE_PENDING_MOTION_LIMIT = _limit("VOTE_PENDING_MOTION_LIMIT", 10, minimum=1, group="database", description="Pending topic/deposition motions")
VIDEO_REPLAY_LIST_LIMIT = _limit("VIDEO_REPLAY_LIST_LIMIT", 200, minimum=1, group="database", description="Videos loaded on replay page")
VIDEO_OPTION_LIMIT = _limit("VIDEO_OPTION_LIMIT", 500, minimum=1, group="database", description="Video options loaded")
VIDEO_IMPORT_MAX_ROWS = _limit("VIDEO_IMPORT_MAX_ROWS", 500, minimum=1, group="database", description="Rows per video import")
VIDEO_TOTAL_LIMIT = _limit("VIDEO_TOTAL_LIMIT", 2_000, minimum=VIDEO_IMPORT_MAX_ROWS, group="database", description="Videos retained")
VIDEO_COMMENT_MAX_PER_VIDEO = _limit("VIDEO_COMMENT_MAX_PER_VIDEO", 1_000, minimum=1, group="database", description="Comments per video")
VIDEO_COMMENT_MAX_PER_USER_DAY = _limit("VIDEO_COMMENT_MAX_PER_USER_DAY", 50, minimum=1, group="database", description="Video comments per user/day")
VIDEO_VIEW_DEDUPE_HOURS = _limit("VIDEO_VIEW_DEDUPE_HOURS", 24, minimum=1, group="database", description="Video-view deduplication window")
VIDEO_COMMENT_RATE_WINDOW_HOURS = _limit("VIDEO_COMMENT_RATE_WINDOW_HOURS", 24, minimum=1, group="database", description="Video-comment user quota window")
VIDEO_PROGRESS_MAX_SECONDS = _limit("VIDEO_PROGRESS_MAX_SECONDS", 24 * 60 * 60, minimum=60, group="database", description="Maximum accepted video progress timestamp")
BUG_REPORT_MAX_TOTAL = _limit("BUG_REPORT_MAX_TOTAL", 5_000, minimum=100, group="database", description="Bug reports retained")
BUG_REPORT_MAX_PER_USER_DAY = _limit("BUG_REPORT_MAX_PER_USER_DAY", 10, minimum=1, group="database", description="Bug reports per user/day")
BUG_REPORT_RATE_WINDOW_HOURS = _limit("BUG_REPORT_RATE_WINDOW_HOURS", 24, minimum=1, group="database", description="Bug-report user quota window")
BUG_REPORT_RECENT_LIMIT = _limit("BUG_REPORT_RECENT_LIMIT", 30, minimum=1, group="database", description="Recent bug reports loaded")
PUSH_RECIPIENT_LIMIT = _limit("PUSH_RECIPIENT_LIMIT", 500, minimum=1, group="database", description="Recipients per push operation")
PUSH_SEND_CONCURRENCY = _limit("PUSH_SEND_CONCURRENCY", 8, minimum=1, maximum=32, group="runtime", description="Concurrent outbound Web Push requests")
PUSH_ACTIVE_DEVICES_PER_USER = _limit("PUSH_ACTIVE_DEVICES_PER_USER", 5, minimum=1, group="database", description="Active push devices per user")
PUSH_INACTIVE_RETENTION_DAYS = _limit("PUSH_INACTIVE_RETENTION_DAYS", 90, minimum=1, group="database", description="Inactive push subscription retention")
PUSH_SUBSCRIPTION_MAX_BYTES = _limit("PUSH_SUBSCRIPTION_MAX_BYTES", 4 * KIB, minimum=512, group="database", description="Serialized push subscription bytes")
PUSH_ENDPOINT_MAX_CHARS = _limit("PUSH_ENDPOINT_MAX_CHARS", 2_048, minimum=128, group="database", description="Push endpoint characters")
PUSH_KEY_MAX_CHARS = _limit("PUSH_KEY_MAX_CHARS", 512, minimum=64, group="database", description="Push key characters")
LOGIN_RECORD_RETENTION_DAYS = _limit("LOGIN_RECORD_RETENTION_DAYS", 400, minimum=31, group="database", description="Login record retention")
NOTIFICATION_READ_RETENTION_DAYS = _limit("NOTIFICATION_READ_RETENTION_DAYS", 400, minimum=31, group="database", description="Notification-read retention")
AI_USAGE_RETENTION_DAYS = _limit("AI_USAGE_RETENTION_DAYS", 400, minimum=90, group="database", description="AI usage telemetry retention")
COMMITTEE_COOKIE_MAX_AGE_DAYS = _limit("COMMITTEE_COOKIE_MAX_AGE_DAYS", 180, minimum=1, group="database", description="Committee login cookie lifetime")
HOME_ACTIVE_MEMBER_WINDOW_HOURS = _limit("HOME_ACTIVE_MEMBER_WINDOW_HOURS", 24, minimum=1, group="database", description="Home active-member window")
LLM_SUBMISSION_RATE_WINDOW_HOURS = _limit("LLM_SUBMISSION_RATE_WINDOW_HOURS", 24, minimum=1, group="database", description="LLM per-user quota window")
OPENROUTER_CREDIT_TIMEOUT_SECONDS = _limit("OPENROUTER_CREDIT_TIMEOUT_SECONDS", 12, minimum=1, group="ai", description="OpenRouter credit lookup timeout")

# Bounded utility workflows.
SCHEDULE_MAX_TEAMS = _limit("SCHEDULE_MAX_TEAMS", 128, minimum=2, group="workflow", description="Teams per schedule draw")
SCHEDULE_MAX_TEAM_NAME_CHARS = _limit("SCHEDULE_MAX_TEAM_NAME_CHARS", 100, minimum=1, group="workflow", description="Team-name characters in draw")


def _validate_relationships() -> None:
    if not BANDWIDTH_WARN_BYTES < BANDWIDTH_STOP_LIVE_BYTES < BANDWIDTH_ESSENTIAL_ONLY_BYTES:
        raise RuntimeError(
            "Bandwidth limits must satisfy BANDWIDTH_WARN_BYTES < "
            "BANDWIDTH_STOP_LIVE_BYTES < BANDWIDTH_ESSENTIAL_ONLY_BYTES"
        )
    if not R2_STORAGE_WARN_BYTES < R2_STORAGE_STOP_BYTES:
        raise RuntimeError("R2_STORAGE_WARN_BYTES must be lower than R2_STORAGE_STOP_BYTES")
    if not R2_URL_MIN_TTL_SECONDS <= R2_UPLOAD_URL_TTL_SECONDS <= R2_UPLOAD_URL_MAX_TTL_SECONDS:
        raise RuntimeError("R2 upload URL TTL must be between its minimum and maximum")
    if not R2_URL_MIN_TTL_SECONDS <= R2_DOWNLOAD_URL_TTL_SECONDS <= R2_DOWNLOAD_URL_MAX_TTL_SECONDS:
        raise RuntimeError("R2 download URL TTL must be between its minimum and maximum")
    if max(R2_MEDIA_LINK_TTL_SECONDS, R2_BULK_LINK_TTL_SECONDS) > R2_DOWNLOAD_URL_MAX_TTL_SECONDS:
        raise RuntimeError("R2 media and bulk link TTLs cannot exceed the download URL maximum")
    if max(R2_UPLOAD_CLAIM_TTL_SECONDS, TTS_REVIEW_CLAIM_TTL_SECONDS) > R2_CLAIM_MAX_TTL_SECONDS:
        raise RuntimeError("R2 signed-claim TTLs cannot exceed R2_CLAIM_MAX_TTL_SECONDS")


_validate_relationships()


def effective_limits() -> dict[str, dict]:
    """Serializable effective registry, grouped by environment-variable name."""
    return {name: asdict(spec) for name, spec in sorted(LIMIT_SPECS.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect effective system limits")
    parser.add_argument("--json", action="store_true", help="print the complete limit registry")
    parser.add_argument("--startup", action="store_true", help="print shell startup limits")
    args = parser.parse_args()
    if args.startup:
        print(UVICORN_LIMIT_CONCURRENCY, UVICORN_WS_MAX_SIZE, MALLOC_ARENA_MAX, MALLOC_TRIM_THRESHOLD_BYTES)
    else:
        print(json.dumps(effective_limits(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
