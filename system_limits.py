"""Single source of truth for production resource and technical safety limits.

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
UVICORN_LIMIT_CONCURRENCY = _limit("UVICORN_LIMIT_CONCURRENCY", 20, minimum=1, maximum=40, group="runtime", description="Uvicorn concurrent requests")
UVICORN_WS_MAX_SIZE = _limit("UVICORN_WS_MAX_SIZE", 2 * MIB, minimum=64 * KIB, maximum=2 * MIB, group="runtime", description="Incoming WebSocket frame bytes")
UVICORN_WS_MAX_QUEUE = _limit("UVICORN_WS_MAX_QUEUE", 4, minimum=1, maximum=4, group="runtime", description="Queued incoming WebSocket frames per connection")
MALLOC_ARENA_MAX = _limit("MALLOC_ARENA_MAX", 2, minimum=1, maximum=4, group="runtime", description="glibc malloc arenas")
MALLOC_TRIM_THRESHOLD_BYTES = _limit("MALLOC_TRIM_THRESHOLD_", 64 * KIB, minimum=0, maximum=64 * KIB, group="runtime", description="glibc trim threshold bytes")
MAX_HTTP_BODY_BYTES = _limit("MAX_HTTP_BODY_BYTES", 5 * MIB, minimum=KIB, maximum=5 * MIB, group="runtime", description="Actual streamed HTTP request-body bytes")
REQUEST_BODY_BUFFER_CONCURRENCY = _limit("REQUEST_BODY_BUFFER_CONCURRENCY", 4, minimum=1, maximum=4, group="runtime", description="Simultaneous retained request bodies")
DB_POOL_SIZE = _limit("DB_POOL_SIZE", 3, minimum=1, maximum=5, group="runtime", description="Persistent SQLAlchemy pool connections")
DB_MAX_OVERFLOW = _limit("DB_MAX_OVERFLOW", 2, minimum=0, maximum=5, group="runtime", description="Temporary SQLAlchemy overflow connections")
DB_POOL_TIMEOUT = _limit("DB_POOL_TIMEOUT", 10, minimum=1, maximum=10, group="runtime", description="DB pool checkout timeout seconds")
DB_POOL_RECYCLE = _limit("DB_POOL_RECYCLE", 300, minimum=30, maximum=3_600, group="runtime", description="DB connection recycle seconds")
GZIP_MINIMUM_SIZE = _limit("GZIP_MINIMUM_SIZE", KIB, minimum=256, maximum=64 * KIB, group="runtime", description="Minimum response bytes before gzip")
GZIP_COMPRESS_LEVEL = _limit("GZIP_COMPRESS_LEVEL", 5, minimum=1, maximum=9, group="runtime", description="gzip compression level")
MAINTENANCE_PRUNE_INTERVAL_SECONDS = _limit("MAINTENANCE_PRUNE_INTERVAL_SECONDS", 86_400, minimum=300, maximum=7 * 86_400, group="runtime", description="Minimum interval between lazy retention sweeps")
ROOM_WS_SEND_TIMEOUT_SECONDS = _limit("ROOM_WS_SEND_TIMEOUT_SECONDS", 1, minimum=1, maximum=10, group="runtime", description="Per-member room WebSocket send timeout")
CACHE_HTML_MAX_AGE_SECONDS = _limit("CACHE_HTML_MAX_AGE_SECONDS", 300, minimum=0, maximum=300, group="bandwidth", description="Browser HTML cache lifetime")
CACHE_HTML_STALE_SECONDS = _limit("CACHE_HTML_STALE_SECONDS", 3_600, minimum=0, maximum=3_600, group="bandwidth", description="HTML stale-while-revalidate lifetime")
CACHE_MANIFEST_MAX_AGE_SECONDS = _limit("CACHE_MANIFEST_MAX_AGE_SECONDS", 86_400, minimum=0, maximum=86_400, group="bandwidth", description="Web manifest cache lifetime")
CACHE_STATIC_MAX_AGE_SECONDS = _limit("CACHE_STATIC_MAX_AGE_SECONDS", 31_536_000, minimum=0, maximum=31_536_000, group="bandwidth", description="Versioned static asset cache lifetime")
CACHE_SHARED_MAX_AGE_SECONDS = _limit("CACHE_SHARED_MAX_AGE_SECONDS", 86_400, minimum=0, maximum=86_400, group="bandwidth", description="Shared public response cache lifetime")
CACHE_SHARED_STALE_SECONDS = _limit("CACHE_SHARED_STALE_SECONDS", 604_800, minimum=0, maximum=604_800, group="bandwidth", description="Shared response stale-while-revalidate lifetime")

# Shared API, pagination and privileged console.
API_PAGE_SIZE = _limit("API_PAGE_SIZE", 20, minimum=1, maximum=200, group="api", description="Rows per server-paginated page")
EXPORT_MAX_ROWS = _limit("EXPORT_MAX_ROWS", 5_000, minimum=1, maximum=5_000, group="api", description="Rows per CSV or JSONL export")
EXPORT_MAX_BYTES = _limit("EXPORT_MAX_BYTES", 5 * MIB, minimum=KIB, maximum=5 * MIB, group="api", description="Bytes per CSV or JSONL export")
OPEN_DB_CACHE_TTL_SECONDS = _limit("OPEN_DB_CACHE_TTL_SECONDS", 60, minimum=1, maximum=3_600, group="api", description="Public topic-bank memory-cache TTL")
OPEN_DB_STALE_REVALIDATE_SECONDS = _limit("OPEN_DB_STALE_REVALIDATE_SECONDS", 300, minimum=1, maximum=86_400, group="api", description="Public topic-bank stale revalidation window")
ADMIN_SESSION_TTL_SECONDS = _limit("ADMIN_SESSION_TTL_SECONDS", 4 * 60 * 60, minimum=60, maximum=24 * 60 * 60, group="api", description="Developer/admin session TTL")
JUDGING_SESSION_TTL_SECONDS = _limit("JUDGING_SESSION_TTL_SECONDS", 12 * 60 * 60, minimum=15 * 60, maximum=24 * 60 * 60, group="api", description="Match-scoped judging session TTL")
JUDGING_SESSION_CLOCK_SKEW_SECONDS = _limit("JUDGING_SESSION_CLOCK_SKEW_SECONDS", 60, minimum=0, maximum=300, group="api", description="Allowed future clock skew in judging session tokens")
JUDGING_SESSION_TOKEN_MAX_CHARS = _limit("JUDGING_SESSION_TOKEN_MAX_CHARS", 2_048, minimum=256, maximum=4_096, group="api", description="Judging session token characters")
REVIEW_SESSION_TOKEN_MAX_CHARS = _limit("REVIEW_SESSION_TOKEN_MAX_CHARS", 2_048, minimum=256, maximum=4_096, group="api", description="Match-review session token characters")
REGISTRATION_ADMIN_SESSION_TTL_SECONDS = _limit("REGISTRATION_ADMIN_SESSION_TTL_SECONDS", 12 * 60 * 60, minimum=60 * 60, maximum=24 * 60 * 60, group="api", description="Organiser registration and competition-control session TTL")
MAX_ADMIN_CONSOLE_SESSIONS = _limit("MAX_ADMIN_CONSOLE_SESSIONS", 256, minimum=16, maximum=256, group="api", description="In-memory privileged console sessions")
RECENT_MATCH_INVENTORY_LIMIT = _limit("RECENT_MATCH_INVENTORY_LIMIT", 500, minimum=10, maximum=2_000, group="api", description="Committee recent-match announcement rows")
HISTORY_EVENT_INVENTORY_LIMIT = _limit("HISTORY_EVENT_INVENTORY_LIMIT", 2_000, minimum=10, maximum=10_000, group="api", description="Team history timeline rows")
COMMITTEE_MEMBERSHIP_INVENTORY_LIMIT = _limit("COMMITTEE_MEMBERSHIP_INVENTORY_LIMIT", 2_000, minimum=10, maximum=10_000, group="api", description="Committee membership tenure rows")
GHOST_FORUM_THREAD_LIMIT = _limit("GHOST_FORUM_THREAD_LIMIT", 10_000, minimum=100, maximum=50_000, group="api", description="Senior committee forum thread rows")
GHOST_FORUM_POST_LIMIT = _limit("GHOST_FORUM_POST_LIMIT", 100_000, minimum=1_000, maximum=500_000, group="api", description="Senior committee forum post rows")
GHOST_FORUM_NOTIFICATION_CLAIM_TTL_SECONDS = _limit("GHOST_FORUM_NOTIFICATION_CLAIM_TTL_SECONDS", 15 * 60, minimum=60, maximum=60 * 60, group="api", description="Retry age for a claimed graduate-forum push")
RECENT_MATCH_NOTIFICATION_CLAIM_TTL_SECONDS = _limit("RECENT_MATCH_NOTIFICATION_CLAIM_TTL_SECONDS", 15 * 60, minimum=60, maximum=60 * 60, group="api", description="Retry age for a claimed recent-match push")
MATCH_TOPIC_RELEASE_GENERATION_LIMIT = _limit("MATCH_TOPIC_RELEASE_GENERATION_LIMIT", 50, minimum=3, maximum=100, group="database", description="Audited topic-release generations retained per match")

# Render monthly bandwidth and Live WebSocket protection.
BANDWIDTH_WARN_BYTES = _limit("BANDWIDTH_WARN_BYTES", 3 * GB, minimum=MIB, maximum=3 * GB, group="bandwidth", description="Monthly warning threshold")
BANDWIDTH_STOP_LIVE_BYTES = _limit("BANDWIDTH_STOP_LIVE_BYTES", 3_500_000_000, minimum=MIB, maximum=3_500_000_000, group="bandwidth", description="Monthly active/new Live hard gate")
BANDWIDTH_ESSENTIAL_ONLY_BYTES = _limit("BANDWIDTH_ESSENTIAL_ONLY_BYTES", 4 * GB, minimum=MIB, maximum=4 * GB, group="bandwidth", description="Monthly nonessential feature hard gate")
BANDWIDTH_CHECKPOINT_SECONDS = _limit("BANDWIDTH_CHECKPOINT_SECONDS", 30, minimum=10, maximum=300, group="bandwidth", description="Live usage checkpoint interval")
BANDWIDTH_LOG_RETENTION_DAYS = _limit("BANDWIDTH_LOG_RETENTION_DAYS", 62, minimum=31, maximum=400, group="bandwidth", description="Bandwidth log retention")
# Live practice rooms and in-memory safety bounds.
MAX_ROOMS = _limit("MAX_ROOMS", 2, minimum=1, maximum=2, group="live", description="Concurrent in-memory rooms")
ROOM_MAX_CAPACITY = _limit("ROOM_MAX_CAPACITY", 2, minimum=2, maximum=2, group="live", description="Members per P2P room")
ROOM_EMPTY_GRACE_SECONDS = _limit("ROOM_EMPTY_GRACE_SECONDS", 60, minimum=10, maximum=300, group="live", description="Empty-room reconnect grace")
ROOM_LOBBY_TTL_SECONDS = _limit("ROOM_LOBBY_TTL_SECONDS", 10 * 60, minimum=10 * 60, maximum=10 * 60, group="live", description="P2P lobby lifetime before formal start")
ROOM_FREE_HARD_GRACE_SECONDS = _limit("ROOM_FREE_HARD_GRACE_SECONDS", 15 * 60, minimum=15 * 60, maximum=15 * 60, group="live", description="Free Debate overall grace beyond both side banks")
ROOM_MOCK_HARD_GRACE_SECONDS = _limit("ROOM_MOCK_HARD_GRACE_SECONDS", 15 * 60, minimum=15 * 60, maximum=15 * 60, group="live", description="Mock overall grace beyond planned duration")
ROOM_ENDED_RETENTION_SECONDS = _limit("ROOM_ENDED_RETENTION_SECONDS", 15 * 60, minimum=15 * 60, maximum=15 * 60, group="live", description="Member-only ended room result retention")
ROOM_RETAINED_ENDED_MAX = _limit("ROOM_RETAINED_ENDED_MAX", 8, minimum=2, maximum=8, group="live", description="Retained ended rooms in the in-process registry")
ROOM_TURN_FINALIZE_TIMEOUT_SECONDS = _limit("ROOM_TURN_FINALIZE_TIMEOUT_SECONDS", 1, minimum=1, maximum=2, group="live", description="Bounded client transcript finalization window on forced turn stops")
ROOM_MANUAL_TURN_FINALIZE_TIMEOUT_SECONDS = _limit("ROOM_MANUAL_TURN_FINALIZE_TIMEOUT_SECONDS", 3, minimum=3, maximum=3, group="live", description="Manual speech-recognition drain watchdog after an authoritative stop intent")
ROOM_TRANSCRIPT_MAX_ITEMS = _limit("ROOM_TRANSCRIPT_MAX_ITEMS", 80, minimum=10, maximum=80, group="live", description="Transcript items retained in RAM")
ROOM_TRANSCRIPT_ITEM_MAX_CHARS = _limit("ROOM_TRANSCRIPT_ITEM_MAX_CHARS", 2_000, minimum=100, maximum=2_000, group="live", description="Characters per transcript item")
ROOM_TRANSCRIPT_TOTAL_MAX_CHARS = _limit("ROOM_TRANSCRIPT_TOTAL_MAX_CHARS", 40_000, minimum=10_000, maximum=60_000, group="live", description="Total transcript characters retained per P2P room")
ROOM_CONTROL_RATE_MESSAGES_PER_SECOND = _limit("ROOM_CONTROL_RATE_MESSAGES_PER_SECOND", 10, minimum=5, maximum=10, group="live", description="Sustained multiplayer control messages per member/second")
ROOM_CONTROL_RATE_BURST_MESSAGES = _limit("ROOM_CONTROL_RATE_BURST_MESSAGES", 20, minimum=10, maximum=20, group="live", description="Multiplayer control-message token-bucket burst per member")
ROOM_CRITICAL_RATE_MESSAGES_PER_SECOND = _limit("ROOM_CRITICAL_RATE_MESSAGES_PER_SECOND", 2, minimum=1, maximum=2, group="live", description="Safety-critical room-control reserve refill per member/second")
ROOM_CRITICAL_RATE_BURST_MESSAGES = _limit("ROOM_CRITICAL_RATE_BURST_MESSAGES", 8, minimum=6, maximum=8, group="live", description="Safety-critical room-control reserve burst per member")
ROOM_WS_TEXT_MAX_BYTES = _limit("ROOM_WS_TEXT_MAX_BYTES", 100 * KIB, minimum=90 * KIB, maximum=100 * KIB, group="live", description="Room client JSON text bytes accepted before parsing")
PRACTICE_LIVE_MIN_GAP_SECONDS = _limit("PRACTICE_LIVE_MIN_GAP_SECONDS", 3, minimum=1, maximum=60, group="live", description="Minimum seconds between token mints")
PRACTICE_LIVE_RATE_WINDOW_SECONDS = _limit("PRACTICE_LIVE_RATE_WINDOW_SECONDS", 60 * 60, minimum=60, maximum=24 * 60 * 60, group="live", description="In-memory duplicate-mint timestamp retention")
LIVE_FREE_MAX_MINUTES = _limit("LIVE_FREE_MAX_MINUTES", 10, minimum=1, maximum=10, group="live", description="Server-authoritative Free De duration per side")
LIVE_FREE_SESSION_MAX_SECONDS = _limit("LIVE_FREE_SESSION_MAX_SECONDS", 30 * 60, minimum=20 * 60, maximum=30 * 60, group="live", description="Solo Free De overall hard deadline including grace")
LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS = _limit("LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS", 25_000, minimum=2_000, maximum=32_000, group="live", description="Gemini Live context compression trigger")
LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS = _limit("LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS", 8_000, minimum=1_000, maximum=16_000, group="live", description="Gemini Live sliding-window retained tokens")
LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS = _limit("LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS", 60, minimum=60, maximum=60, group="live", description="Window for starting a new Gemini Live session")
LIVE_TOKEN_MINT_TIMEOUT_SECONDS = _limit("LIVE_TOKEN_MINT_TIMEOUT_SECONDS", 20, minimum=5, maximum=20, group="live", description="Gemini ephemeral-token mint request timeout")
LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS = _limit("LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS", 3, minimum=3, maximum=3, group="live", description="Per-statement timeout while atomically issuing a Solo Live token")
LIVE_TOKEN_EXPIRY_GRACE_SECONDS = _limit("LIVE_TOKEN_EXPIRY_GRACE_SECONDS", 60, minimum=30, maximum=300, group="live", description="Gemini Live token lifetime beyond latest legal session start and planned runtime")
LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS = _limit("LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS", 45, minimum=10, maximum=55, group="live", description="Short retry window for a disclosed ephemeral Live token")
LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS = _limit("LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS", 5, minimum=5, maximum=5, group="live", description="Clock and delivery safety margin before a cached Live token's start window closes")
LIVE_TOKEN_RESPONSE_CACHE_MAX_ENTRIES = _limit("LIVE_TOKEN_RESPONSE_CACHE_MAX_ENTRIES", 64, minimum=4, maximum=128, group="live", description="Maximum retryable ephemeral Live tokens retained in process memory")
LIVE_MOCK_OVERALL_GRACE_SECONDS = _limit("LIVE_MOCK_OVERALL_GRACE_SECONDS", 10 * 60, minimum=10 * 60, maximum=10 * 60, group="live", description="Single overall grace budget shared by every Solo Mock section")
LIVE_PRACTICE_CLAIM_TTL_SECONDS = _limit("LIVE_PRACTICE_CLAIM_TTL_SECONDS", 2 * 60 * 60, minimum=2 * 60 * 60, maximum=2 * 60 * 60, group="live", description="Signed Solo practice/JIT mint claim lifetime")
LIVE_PRACTICE_CLAIM_MAX_CHARS = _limit("LIVE_PRACTICE_CLAIM_MAX_CHARS", 120_000, minimum=32_000, maximum=120_000, group="live", description="Signed Solo practice/JIT claim characters")
LIVE_SYSTEM_PROMPT_MAX_CHARS = _limit("LIVE_SYSTEM_PROMPT_MAX_CHARS", 20_000, minimum=4_000, maximum=20_000, group="live", description="Server-authored Gemini Live system prompt characters")
PROJECTOR_MATCH_LIMIT = _limit("PROJECTOR_MATCH_LIMIT", 200, minimum=1, maximum=200, group="live", description="Matches exposed to projector")

# TTS, AI Coach, training, RAG and provider prompt protection.
TTS_CONCURRENCY = _limit("TTS_CONCURRENCY", 2, minimum=1, maximum=2, group="ai", description="Concurrent TTS synthesis")
TTS_MAX_RESPONSE_BYTES = _limit("TTS_MAX_RESPONSE_BYTES", 4 * MIB, minimum=64 * KIB, maximum=4 * MIB, group="ai", description="TTS response bytes")
TTS_TEXT_MAX_CHARS = _limit("TTS_TEXT_MAX_CHARS", 1_200, minimum=100, maximum=1_200, group="ai", description="Characters per TTS synthesis request")
TTS_LEXICON_LIMIT = _limit("TTS_LEXICON_LIMIT", 2_000, minimum=100, maximum=2_000, group="ai", description="TTS lexicon entries loaded")
TTS_LEXICON_CACHE_TTL_SECONDS = _limit("TTS_LEXICON_CACHE_TTL_SECONDS", 60, minimum=1, maximum=3_600, group="ai", description="TTS lexicon RAM-cache lifetime")
TTS_PROVIDER_TIMEOUT_SECONDS = _limit("TTS_PROVIDER_TIMEOUT_SECONDS", 30, minimum=1, maximum=120, group="ai", description="TTS provider request timeout")
TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS = _limit("TTS_PROVIDER_CONNECT_TIMEOUT_SECONDS", 5, minimum=1, maximum=30, group="ai", description="Custom TTS connection timeout")
AI_TRAINING_PROVIDER_TIMEOUT_SECONDS = _limit("AI_TRAINING_PROVIDER_TIMEOUT_SECONDS", 60, minimum=1, maximum=300, group="ai", description="AI training provider request timeout")
AI_PROVIDER_OPENROUTER_TIMEOUT_SECONDS = _limit("AI_PROVIDER_OPENROUTER_TIMEOUT_SECONDS", 70, minimum=1, maximum=300, group="ai", description="OpenRouter generation timeout")
AI_PROVIDER_GEMINI_TIMEOUT_SECONDS = _limit("AI_PROVIDER_GEMINI_TIMEOUT_SECONDS", 90, minimum=1, maximum=300, group="ai", description="Gemini generation timeout")
AI_PROVIDER_RESPONSE_MAX_BYTES = _limit("AI_PROVIDER_RESPONSE_MAX_BYTES", 2 * MIB, minimum=64 * KIB, maximum=2 * MIB, group="ai", description="Provider JSON response bytes buffered in RAM")
RAG_PROVIDER_TIMEOUT_SECONDS = _limit("RAG_PROVIDER_TIMEOUT_SECONDS", 30, minimum=1, maximum=120, group="ai", description="RAG embedding provider timeout")
RAG_SCHEMA_CHECK_TTL_SECONDS = _limit("RAG_SCHEMA_CHECK_TTL_SECONDS", 300, minimum=30, maximum=3_600, group="ai", description="Failed RAG schema readiness cache TTL")
ROOM_JUDGEMENT_TIMEOUT_SECONDS = _limit("ROOM_JUDGEMENT_TIMEOUT_SECONDS", 45, minimum=1, maximum=120, group="ai", description="Room judgement provider timeout")
ROOM_JUDGEMENT_CONCURRENCY = _limit("ROOM_JUDGEMENT_CONCURRENCY", 2, minimum=1, maximum=2, group="ai", description="Concurrent ended-room judgement workflows")
MEDIA_PROBE_TIMEOUT_SECONDS = _limit("MEDIA_PROBE_TIMEOUT_SECONDS", 10, minimum=1, maximum=60, group="ai", description="ffprobe recording inspection timeout")
MEDIA_TRANSCODE_TIMEOUT_SECONDS = _limit("MEDIA_TRANSCODE_TIMEOUT_SECONDS", 120, minimum=10, maximum=300, group="ai", description="ffmpeg full-match audio normalization timeout")
MAX_AUDIO_BYTES = _limit("MAX_AUDIO_BYTES", 2 * MIB, minimum=KIB, maximum=2 * MIB, group="ai", description="TTS training recording bytes")
TTS_MAX_DURATION_SECONDS = _limit("TTS_MAX_DURATION_SECONDS", 60, minimum=1, maximum=60, group="ai", description="TTS/AI recording duration")
TTS_REVIEW_CONCURRENCY = _limit("TTS_REVIEW_CONCURRENCY", 2, minimum=1, maximum=2, group="ai", description="Concurrent TTS quality reviews")
AI_COACH_CONCURRENCY = _limit("AI_COACH_CONCURRENCY", 3, minimum=1, maximum=3, group="ai", description="Concurrent AI Coach requests")
COMPETITION_PREP_PROJECT_LIMIT = _limit("COMPETITION_PREP_PROJECT_LIMIT", 200, minimum=10, maximum=500, group="database", description="Competition-preparation projects visible to one member")
COMPETITION_PREP_MEMBER_LIMIT = _limit("COMPETITION_PREP_MEMBER_LIMIT", 50, minimum=2, maximum=100, group="database", description="Collaborators per competition-preparation project")
COMPETITION_PREP_MANUSCRIPT_LIMIT = _limit("COMPETITION_PREP_MANUSCRIPT_LIMIT", 30, minimum=4, maximum=100, group="database", description="Manuscripts per competition-preparation project")
COMPETITION_PREP_MANUSCRIPT_MAX_CHARS = _limit("COMPETITION_PREP_MANUSCRIPT_MAX_CHARS", 20_000, minimum=2_000, maximum=40_000, group="ai", description="Characters per competition-preparation manuscript")
COMPETITION_PREP_CARD_LIMIT = _limit("COMPETITION_PREP_CARD_LIMIT", 200, minimum=20, maximum=500, group="database", description="Strategy or evidence cards per competition-preparation project")
COMPETITION_PREP_WEAKNESS_LIMIT = _limit("COMPETITION_PREP_WEAKNESS_LIMIT", 100, minimum=10, maximum=300, group="database", description="Weaknesses per competition-preparation project")
COMPETITION_PREP_AI_CONTEXT_MAX_CHARS = _limit("COMPETITION_PREP_AI_CONTEXT_MAX_CHARS", 60_000, minimum=10_000, maximum=60_000, group="ai", description="Combined competition-preparation context sent to a provider")
COMPETITION_PREP_AI_OUTPUT_MAX_CHARS = _limit("COMPETITION_PREP_AI_OUTPUT_MAX_CHARS", 40_000, minimum=5_000, maximum=60_000, group="ai", description="AI output retained per competition-preparation run")
COMPETITION_PREP_PRUNE_BATCH = _limit("COMPETITION_PREP_PRUNE_BATCH", 50, minimum=5, maximum=200, group="database", description="Expired competition-preparation projects removed per request")
KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES = _limit("KIOSK_MATCH_REVIEW_MAX_AUDIO_BYTES", 12 * MIB, minimum=MIB, maximum=12 * MIB, group="ai", description="Temporary full-match kiosk recording bytes")
KIOSK_MATCH_REVIEW_MAX_SECONDS = _limit("KIOSK_MATCH_REVIEW_MAX_SECONDS", 90 * 60, minimum=10 * 60, maximum=90 * 60, group="ai", description="Full-match kiosk recording duration")
KIOSK_MATCH_REVIEW_CONCURRENCY = _limit("KIOSK_MATCH_REVIEW_CONCURRENCY", 1, minimum=1, maximum=1, group="ai", description="Concurrent full-match audio reviews")
KIOSK_MATCH_REVIEW_MARKER_LIMIT = _limit("KIOSK_MATCH_REVIEW_MARKER_LIMIT", 600, minimum=20, maximum=600, group="ai", description="Timestamped side markers accepted per full match")
KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS = _limit("KIOSK_MATCH_REVIEW_TRANSCRIPT_MAX_CHARS", 120_000, minimum=20_000, maximum=120_000, group="ai", description="Detailed full-match transcript characters passed to judgement")
KIOSK_MATCH_REVIEW_PROVIDER_TIMEOUT_SECONDS = _limit("KIOSK_MATCH_REVIEW_PROVIDER_TIMEOUT_SECONDS", 300, minimum=60, maximum=300, group="ai", description="Per-pass full-match Gemini timeout")
KIOSK_PROJECTOR_RESULT_PERSIST_ATTEMPTS = _limit("KIOSK_PROJECTOR_RESULT_PERSIST_ATTEMPTS", 3, minimum=1, maximum=5, group="ai", description="Server-side attempts to persist a completed kiosk review to projector state")
KIOSK_LOCAL_RECORDING_RETENTION_DAYS = _limit("KIOSK_LOCAL_RECORDING_RETENTION_DAYS", 7, minimum=1, maximum=30, group="storage", description="Failed kiosk recordings retained in browser storage")
OFFICIAL_AI_JUDGE_CONCURRENCY = _limit("OFFICIAL_AI_JUDGE_CONCURRENCY", 1, minimum=1, maximum=1, group="ai", description="Concurrent official third-judge provider calls")
OFFICIAL_AI_JUDGE_TIMEOUT_SECONDS = _limit("OFFICIAL_AI_JUDGE_TIMEOUT_SECONDS", 300, minimum=60, maximum=300, group="ai", description="Official third-judge provider timeout")
OFFICIAL_AI_JUDGE_PROMPT_MAX_CHARS = _limit("OFFICIAL_AI_JUDGE_PROMPT_MAX_CHARS", 140_000, minimum=20_000, maximum=140_000, group="ai", description="Official third-judge prompt characters including full transcript")
OFFICIAL_AI_JUDGE_CLAIM_TTL_SECONDS = _limit("OFFICIAL_AI_JUDGE_CLAIM_TTL_SECONDS", 360, minimum=300, maximum=600, group="ai", description="Official third-judge attempt claim lifetime")
PROJECTOR_START_COMMAND_TTL_SECONDS = _limit("PROJECTOR_START_COMMAND_TTL_SECONDS", 20, minimum=5, maximum=60, group="live", description="Projector start command acknowledgement deadline")
PROJECTOR_KIOSK_LEASE_TTL_SECONDS = _limit("PROJECTOR_KIOSK_LEASE_TTL_SECONDS", 15, minimum=10, maximum=60, group="live", description="Projector Kiosk owner lease lifetime")
LIVE_BRIEF_TTL_MINUTES = _limit("LIVE_BRIEF_TTL_MINUTES", 15, minimum=1, maximum=60, group="ai", description="Prepared Live research brief TTL")
LIVE_BRIEF_MAX_CHARS = _limit("LIVE_BRIEF_MAX_CHARS", 4_500, minimum=500, maximum=4_500, group="ai", description="Prepared Live research brief characters")
AI_COACH_TOPIC_LIMIT = _limit("AI_COACH_TOPIC_LIMIT", 2_000, minimum=100, maximum=2_000, group="ai", description="Topics loaded by AI Coach")
AI_COACH_MATCH_LIMIT = _limit("AI_COACH_MATCH_LIMIT", 500, minimum=50, maximum=500, group="ai", description="Matches loaded by AI Coach")
LLM_CONTENT_MAX_CHARS = _limit("LLM_CONTENT_MAX_CHARS", 20_000, minimum=1_000, maximum=20_000, group="ai", description="Characters per LLM training submission")
LLM_SUBMISSION_MAX_TOTAL = _limit("LLM_SUBMISSION_MAX_TOTAL", 5_000, minimum=100, maximum=5_000, group="ai", description="LLM submissions retained")
LLM_REVIEW_CONCURRENCY = _limit("LLM_REVIEW_CONCURRENCY", 2, minimum=1, maximum=2, group="ai", description="Concurrent LLM reviews")
AI_FACTORY_SOURCE_MAX_CHARS = _limit("AI_FACTORY_SOURCE_MAX_CHARS", 20_000, minimum=1_000, maximum=20_000, group="ai", description="Characters in one immutable data-factory source snapshot")
AI_FACTORY_SOURCE_NOTE_MAX_CHARS = _limit("AI_FACTORY_SOURCE_NOTE_MAX_CHARS", 1_000, minimum=100, maximum=1_000, group="ai", description="Rights and provenance note characters on one data-factory source")
AI_FACTORY_INSTRUCTION_MAX_CHARS = _limit("AI_FACTORY_INSTRUCTION_MAX_CHARS", 500, minimum=0, maximum=500, group="ai", description="Manager instruction characters per data-factory job")
AI_FACTORY_CANDIDATE_DEFAULT = _limit("AI_FACTORY_CANDIDATE_DEFAULT", 3, minimum=1, maximum=5, group="ai", description="Default generated candidates per data-factory job")
AI_FACTORY_CANDIDATE_MAX = _limit("AI_FACTORY_CANDIDATE_MAX", 5, minimum=1, maximum=5, group="ai", description="Maximum generated candidates per data-factory job")
AI_FACTORY_RAG_CONTENT_MAX_CHARS = _limit("AI_FACTORY_RAG_CONTENT_MAX_CHARS", 3_000, minimum=500, maximum=3_000, group="ai", description="Characters in one reviewed RAG card")
AI_FACTORY_RAG_CLAIM_MAX = _limit("AI_FACTORY_RAG_CLAIM_MAX", 8, minimum=1, maximum=8, group="ai", description="Claims in one argument-decomposition RAG card")
AI_FACTORY_SFT_USER_MAX_CHARS = _limit("AI_FACTORY_SFT_USER_MAX_CHARS", 4_000, minimum=500, maximum=4_000, group="ai", description="User-message characters in one SFT item")
AI_FACTORY_SFT_ASSISTANT_MAX_CHARS = _limit("AI_FACTORY_SFT_ASSISTANT_MAX_CHARS", 6_000, minimum=500, maximum=6_000, group="ai", description="Assistant-message characters in one SFT item")
AI_FACTORY_PREVIEW_TTL_SECONDS = _limit("AI_FACTORY_PREVIEW_TTL_SECONDS", 900, minimum=60, maximum=900, group="ai", description="Lifetime of an exact provider-bound data-factory preview")
AI_FACTORY_ATTEMPT_MAX = _limit("AI_FACTORY_ATTEMPT_MAX", 3, minimum=1, maximum=3, group="ai", description="Manual provider attempts allowed per data-factory job")
AI_FACTORY_CONCURRENCY = _limit("AI_FACTORY_CONCURRENCY", 2, minimum=1, maximum=2, group="ai", description="Concurrent data-factory provider calls")
AI_FACTORY_MANAGER_CONCURRENCY = _limit("AI_FACTORY_MANAGER_CONCURRENCY", 1, minimum=1, maximum=1, group="ai", description="Concurrent data-factory provider calls per manager")
AI_FACTORY_TOPIC_TAG_MAX = _limit("AI_FACTORY_TOPIC_TAG_MAX", 5, minimum=1, maximum=5, group="ai", description="Approved custom topic tags attached to one factory item")
AI_FACTORY_TOPIC_TAG_MAX_CHARS = _limit("AI_FACTORY_TOPIC_TAG_MAX_CHARS", 40, minimum=1, maximum=40, group="ai", description="Characters in one custom data-factory topic tag")
AI_FACTORY_RELEASE_MAX_ITEMS = _limit("AI_FACTORY_RELEASE_MAX_ITEMS", 500, minimum=1, maximum=500, group="ai", description="Items in one immutable RAG or SFT release")
AI_FACTORY_RELEASE_MAX_BYTES = _limit("AI_FACTORY_RELEASE_MAX_BYTES", 5 * MIB, minimum=KIB, maximum=5 * MIB, group="ai", description="Bytes in one immutable data-factory JSONL release")
AI_FACTORY_SOURCE_MAX_TOTAL = _limit("AI_FACTORY_SOURCE_MAX_TOTAL", 2_000, minimum=100, maximum=2_000, group="database", description="Data-factory source snapshots retained for admin workflows")
AI_FACTORY_JOB_MAX_TOTAL = _limit("AI_FACTORY_JOB_MAX_TOTAL", 10_000, minimum=100, maximum=10_000, group="database", description="Data-factory jobs retained for admin workflows")
AI_FACTORY_ITEM_MAX_TOTAL = _limit("AI_FACTORY_ITEM_MAX_TOTAL", 50_000, minimum=500, maximum=50_000, group="database", description="Generated data-factory items retained for review and release")
AI_FACTORY_TOPIC_TAG_MAX_TOTAL = _limit("AI_FACTORY_TOPIC_TAG_MAX_TOTAL", 500, minimum=10, maximum=500, group="database", description="Custom data-factory topic tags retained")
AI_FACTORY_RELEASE_MAX_TOTAL = _limit("AI_FACTORY_RELEASE_MAX_TOTAL", 200, minimum=10, maximum=200, group="database", description="Immutable data-factory releases retained")
AI_FACTORY_TRANSCRIPT_MAX_CHARS = _limit("AI_FACTORY_TRANSCRIPT_MAX_CHARS", 200_000, minimum=20_000, maximum=200_000, group="ai", description="Characters in one immutable full-match transcript")
AI_FACTORY_TRANSCRIPT_CORE_CHARS = _limit("AI_FACTORY_TRANSCRIPT_CORE_CHARS", 11_000, minimum=5_000, maximum=15_000, group="ai", description="Non-overlapping transcript characters owned by one structure window")
AI_FACTORY_TRANSCRIPT_OVERLAP_CHARS = _limit("AI_FACTORY_TRANSCRIPT_OVERLAP_CHARS", 2_000, minimum=500, maximum=4_000, group="ai", description="Context characters supplied on each side of a transcript structure window")
AI_FACTORY_TRANSCRIPT_BOUNDARY_MAX = _limit("AI_FACTORY_TRANSCRIPT_BOUNDARY_MAX", 80, minimum=10, maximum=80, group="ai", description="Speaker boundaries returned by one transcript structure window")
AI_FACTORY_TRANSCRIPT_OUTPUT_MAX_TOKENS = _limit("AI_FACTORY_TRANSCRIPT_OUTPUT_MAX_TOKENS", 8_000, minimum=2_000, maximum=8_000, group="ai", description="Provider output tokens for one transcript structure window")
AI_FACTORY_TRANSCRIPT_REVIEW_CONTEXT_CHARS = _limit("AI_FACTORY_TRANSCRIPT_REVIEW_CONTEXT_CHARS", 4_000, minimum=500, maximum=4_000, group="api", description="Transcript context characters returned on each side of a reviewed segment")
AI_FACTORY_TRANSCRIPT_MAX_TOTAL = _limit("AI_FACTORY_TRANSCRIPT_MAX_TOTAL", 500, minimum=10, maximum=500, group="database", description="Full-match transcripts retained by the data factory")
AI_FACTORY_TRANSCRIPT_RUN_MAX_TOTAL = _limit("AI_FACTORY_TRANSCRIPT_RUN_MAX_TOTAL", 2_000, minimum=10, maximum=2_000, group="database", description="Transcript structure runs retained by the data factory")
AI_FACTORY_TRANSCRIPT_SEGMENT_MAX_TOTAL = _limit("AI_FACTORY_TRANSCRIPT_SEGMENT_MAX_TOTAL", 100_000, minimum=500, maximum=100_000, group="database", description="Transcript segments retained for review")
DATASET_SNAPSHOT_MAX_ITEMS = _limit("DATASET_SNAPSHOT_MAX_ITEMS", 500, minimum=1, maximum=500, group="ai", description="Items per dataset snapshot")
DATASET_SNAPSHOT_MAX_COUNT = _limit("DATASET_SNAPSHOT_MAX_COUNT", 200, minimum=10, maximum=200, group="ai", description="Dataset snapshots retained")
RECORDING_MANIFEST_MAX_ROWS = _limit("RECORDING_MANIFEST_MAX_ROWS", 2_000, minimum=1, maximum=2_000, group="ai", description="Rows in recording manifests")
RAG_REINDEX_MAX_DOCUMENTS = _limit("RAG_REINDEX_MAX_DOCUMENTS", 10, minimum=1, maximum=10, group="ai", description="RAG documents per reindex")
RAG_REINDEX_MAX_CHUNKS = _limit("RAG_REINDEX_MAX_CHUNKS", 100, minimum=1, maximum=100, group="ai", description="RAG chunks per reindex")
RAG_DOCUMENT_MAX_TOTAL = _limit("RAG_DOCUMENT_MAX_TOTAL", 1_000, minimum=100, maximum=1_000, group="ai", description="Active RAG documents")
RAG_EMBED_CONCURRENCY = _limit("RAG_EMBED_CONCURRENCY", 3, minimum=1, maximum=3, group="ai", description="Concurrent embedding requests")
RAG_CONTEXT_MAX_CHARS = _limit("RAG_CONTEXT_MAX_CHARS", 12_000, minimum=1_000, maximum=12_000, group="ai", description="RAG context characters appended to a provider prompt")
AI_TRAINING_INVENTORY_LIMIT = _limit("AI_TRAINING_INVENTORY_LIMIT", 2_000, minimum=100, maximum=2_000, group="ai", description="TTS training scripts retained")
AI_TRAINING_JSON_MAX_BYTES = _limit("AI_TRAINING_JSON_MAX_BYTES", 100 * KIB, minimum=KIB, maximum=100 * KIB, group="ai", description="AI training JSON column bytes")
AI_EVAL_CASE_LIMIT = _limit("AI_EVAL_CASE_LIMIT", 200, minimum=30, maximum=200, group="ai", description="AI evaluation cases")
AI_MODEL_VERSION_LIMIT = _limit("AI_MODEL_VERSION_LIMIT", 200, minimum=10, maximum=200, group="ai", description="AI model versions retained")
TTS_AI_ANALYSIS_SCRIPT_LIMIT = _limit("TTS_AI_ANALYSIS_SCRIPT_LIMIT", 500, minimum=50, maximum=500, group="ai", description="Scripts per AI coverage analysis")
AI_TRAINING_PROMPT_MAX_CHARS = _limit("AI_TRAINING_PROMPT_MAX_CHARS", 60_000, minimum=10_000, maximum=60_000, group="ai", description="AI training prompt characters")
AI_TRAINING_AUDIT_RETENTION_DAYS = _limit("AI_TRAINING_AUDIT_RETENTION_DAYS", 400, minimum=90, maximum=400, group="ai", description="AI training audit retention")
AI_TRAINING_ADMIN_PAGE_SIZE = _limit("AI_TRAINING_ADMIN_PAGE_SIZE", 5, minimum=1, maximum=100, group="ai", description="AI training admin rows/page")
AI_TRAINING_READINESS_GROUP_LIMIT = _limit("AI_TRAINING_READINESS_GROUP_LIMIT", 200, minimum=1, maximum=200, group="ai", description="Readiness aggregate groups")
AI_SUGGESTION_BATCH_MAX = _limit("AI_SUGGESTION_BATCH_MAX", 50, minimum=1, maximum=500, group="ai", description="AI script suggestions applied per request")
AI_PROVIDER_PROMPT_MAX_CHARS = _limit("AI_PROVIDER_PROMPT_MAX_CHARS", 60_000, minimum=4_000, maximum=60_000, group="ai", description="Provider prompt characters")
AI_PROVIDER_OUTPUT_MAX_TOKENS = _limit("AI_PROVIDER_OUTPUT_MAX_TOKENS", 65_536, minimum=1_000, maximum=65_536, group="ai", description="Hard output-token ceiling when an AI feature supplies a per-call bound")
OPENROUTER_WEB_SEARCH_MAX_RESULTS = _limit("OPENROUTER_WEB_SEARCH_MAX_RESULTS", 5, minimum=1, maximum=10, group="ai", description="OpenRouter results returned per web-search call")
OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS = _limit("OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS", 15, minimum=1, maximum=25, group="ai", description="OpenRouter cumulative web-search results per request")
AI_PROVIDER_SOURCE_LIMIT = _limit("AI_PROVIDER_SOURCE_LIMIT", 20, minimum=1, maximum=20, group="ai", description="Grounded sources returned")
LMC_AI_NODE_MAX = _limit("LMC_AI_NODE_MAX", 8, minimum=1, maximum=8, group="ai", description="Registered local AI computers")
LMC_AI_NODE_NAME_MAX_CHARS = _limit("LMC_AI_NODE_NAME_MAX_CHARS", 80, minimum=1, maximum=80, group="ai", description="Local AI computer display-name characters")
LMC_AI_NODE_WS_FRAME_MAX_BYTES = _limit("LMC_AI_NODE_WS_FRAME_MAX_BYTES", 64 * KIB, minimum=KIB, maximum=64 * KIB, group="api", description="Local AI node WebSocket frame bytes")
LMC_AI_HEARTBEAT_INTERVAL_SECONDS = _limit("LMC_AI_HEARTBEAT_INTERVAL_SECONDS", 15, minimum=5, maximum=60, group="ai", description="Local AI node heartbeat interval")
LMC_AI_HEARTBEAT_TIMEOUT_SECONDS = _limit("LMC_AI_HEARTBEAT_TIMEOUT_SECONDS", 45, minimum=15, maximum=180, group="ai", description="Local AI node offline timeout")
LMC_AI_ACTIVE_GENERATIONS = _limit("LMC_AI_ACTIVE_GENERATIONS", 1, minimum=1, maximum=1, group="ai", description="Concurrent local AI generations per selected node")
LMC_AI_QUEUE_MAX = _limit("LMC_AI_QUEUE_MAX", 2, minimum=0, maximum=2, group="ai", description="Queued local AI requests behind the active generation")
LMC_AI_MESSAGE_MAX_CHARS = _limit("LMC_AI_MESSAGE_MAX_CHARS", 3_000, minimum=100, maximum=3_000, group="ai", description="Characters in one local AI user message")
LMC_AI_CONTEXT_MAX_CHARS = _limit("LMC_AI_CONTEXT_MAX_CHARS", 3_000, minimum=500, maximum=3_000, group="ai", description="Characters from browser history sent to a local AI request")
LMC_AI_REQUEST_MESSAGES_MAX = _limit("LMC_AI_REQUEST_MESSAGES_MAX", 40, minimum=1, maximum=40, group="ai", description="Messages accepted in one local AI request")
LMC_AI_REQUEST_TIMEOUT_SECONDS = _limit("LMC_AI_REQUEST_TIMEOUT_SECONDS", 180, minimum=30, maximum=180, group="ai", description="Total local AI streaming request timeout")
LMC_AI_PREFLIGHT_TIMEOUT_SECONDS = _limit("LMC_AI_PREFLIGHT_TIMEOUT_SECONDS", 60, minimum=10, maximum=60, group="ai", description="Local AI model preflight timeout")
LMC_AI_OUTPUT_MAX_BYTES = _limit("LMC_AI_OUTPUT_MAX_BYTES", 256 * KIB, minimum=KIB, maximum=256 * KIB, group="ai", description="Local AI streamed answer bytes")
LMC_AI_EVAL_CAMPAIGN_MAX = _limit("LMC_AI_EVAL_CAMPAIGN_MAX", 10, minimum=1, maximum=10, group="ai", description="Retained fixed local-AI eval campaigns")
LMC_AI_EVAL_OUTPUT_MAX_BYTES = _limit("LMC_AI_EVAL_OUTPUT_MAX_BYTES", 16 * KIB, minimum=KIB, maximum=16 * KIB, group="ai", description="Bytes in one fixed local-AI eval answer")
LMC_AI_EVAL_GENERATION_ATTEMPT_MAX = _limit("LMC_AI_EVAL_GENERATION_ATTEMPT_MAX", 3, minimum=1, maximum=3, group="ai", description="Real local-AI attempts per eval output")
LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS = _limit("LMC_AI_EVAL_REVIEW_NOTE_MAX_CHARS", 500, minimum=0, maximum=500, group="ai", description="Characters in one blind-review note")
LMC_AI_EVAL_REVIEWS_PER_PAIR = _limit("LMC_AI_EVAL_REVIEWS_PER_PAIR", 3, minimum=3, maximum=3, group="ai", description="Unique reviewers required per eval pair")
LMC_AI_EVAL_REVIEW_ASSIGNMENT_TTL_SECONDS = _limit("LMC_AI_EVAL_REVIEW_ASSIGNMENT_TTL_SECONDS", 24 * 60 * 60, minimum=24 * 60 * 60, maximum=24 * 60 * 60, group="ai", description="Fixed lifetime of one blind-review reservation")
LMC_AI_EVAL_ASSIGNMENT_LIST_MAX = _limit("LMC_AI_EVAL_ASSIGNMENT_LIST_MAX", 300, minimum=270, maximum=300, group="api", description="Manager-visible pending eval reservations")
LMC_AI_EVAL_PROCESSING_LEASE_SECONDS = _limit("LMC_AI_EVAL_PROCESSING_LEASE_SECONDS", 240, minimum=181, maximum=600, group="ai", description="Lease for restart-safe eval generation claims")
LMC_AI_EVAL_EXPORT_MAX_BYTES = _limit("LMC_AI_EVAL_EXPORT_MAX_BYTES", 2 * MIB, minimum=64 * KIB, maximum=2 * MIB, group="api", description="Manager eval audit export bytes")
LMC_AI_BROWSER_HISTORY_MAX_MESSAGES = _limit("LMC_AI_BROWSER_HISTORY_MAX_MESSAGES", 100, minimum=2, maximum=100, group="browser", description="Messages retained for one browser-local AI conversation")
LMC_AI_BROWSER_HISTORY_MAX_CHARS = _limit("LMC_AI_BROWSER_HISTORY_MAX_CHARS", 200_000, minimum=1_000, maximum=200_000, group="browser", description="Characters retained for one browser-local AI conversation")
DATASET_ARCHIVE_MAX_ITEMS = _limit("DATASET_ARCHIVE_MAX_ITEMS", 5_000, minimum=100, maximum=5_000, group="ai", description="Files accepted from an offline training archive")
DATASET_ARCHIVE_MAX_BYTES = _limit("DATASET_ARCHIVE_MAX_BYTES", 10 * GB, minimum=MIB, maximum=10 * GB, group="ai", description="Uncompressed bytes accepted from an offline training archive")
DATASET_MANIFEST_MAX_BYTES = _limit("DATASET_MANIFEST_MAX_BYTES", 10 * MIB, minimum=64 * KIB, maximum=10 * MIB, group="ai", description="Offline recordings manifest bytes loaded in RAM")
DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS = _limit("DATASET_MEDIA_PROCESS_TIMEOUT_SECONDS", 120, minimum=10, maximum=600, group="ai", description="Offline ffmpeg or ffprobe timeout per training clip")
VOTE_AI_PROMPT_MAX_CHARS = _limit("VOTE_AI_PROMPT_MAX_CHARS", 60_000, minimum=4_000, maximum=60_000, group="ai", description="Vote-AI prompt characters")
VOTE_AI_TOPIC_SAMPLE_LIMIT = _limit("VOTE_AI_TOPIC_SAMPLE_LIMIT", 500, minimum=50, maximum=500, group="ai", description="Topics sampled for Vote AI")
VOTE_AI_DISCUSSION_COMMENT_LIMIT = _limit("VOTE_AI_DISCUSSION_COMMENT_LIMIT", 30, minimum=1, maximum=30, group="ai", description="Comments sent to Vote AI")
VOTE_AI_CATEGORY_EXAMPLE_LIMIT = _limit("VOTE_AI_CATEGORY_EXAMPLE_LIMIT", 15, minimum=1, maximum=15, group="ai", description="Category examples sent to Vote AI")

# Cloudflare R2, photos and upload lifecycle.
R2_STORAGE_WARN_BYTES = _limit("R2_STORAGE_WARN_BYTES", 7 * GB, minimum=MIB, maximum=7 * GB, group="storage", description="R2 warning threshold")
R2_STORAGE_STOP_BYTES = _limit("R2_STORAGE_STOP_BYTES", 8 * GB, minimum=MIB, maximum=8 * GB, group="storage", description="R2 new-upload hard gate")
R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS = _limit("R2_USAGE_SNAPSHOT_MAX_AGE_SECONDS", 6 * 60 * 60, minimum=300, maximum=24 * 60 * 60, group="storage", description="R2 exact-usage snapshot TTL")
R2_INTENT_RETENTION_DAYS = _limit("R2_INTENT_RETENTION_DAYS", 90, minimum=30, maximum=400, group="storage", description="Completed/orphan upload-intent retention")
R2_ORPHAN_MIN_AGE_HOURS = _limit("R2_ORPHAN_MIN_AGE_HOURS", 48, minimum=24, maximum=7 * 24, group="storage", description="Minimum age before orphan cleanup")
R2_ORPHAN_DRY_RUN_DISPLAY_LIMIT = _limit("R2_ORPHAN_DRY_RUN_DISPLAY_LIMIT", 100, minimum=1, maximum=100, group="storage", description="Orphans printed by a dry run")
R2_UPLOAD_URL_TTL_SECONDS = _limit("R2_UPLOAD_URL_TTL_SECONDS", 300, minimum=60, maximum=900, group="storage", description="Default direct-upload URL lifetime")
R2_DOWNLOAD_URL_TTL_SECONDS = _limit("R2_DOWNLOAD_URL_TTL_SECONDS", 600, minimum=60, maximum=3_600, group="storage", description="Default direct-download URL lifetime")
R2_MEDIA_LINK_TTL_SECONDS = _limit("R2_MEDIA_LINK_TTL_SECONDS", 1_800, minimum=60, maximum=3_600, group="storage", description="Authenticated photo/audio playback URL lifetime")
R2_BULK_LINK_TTL_SECONDS = _limit("R2_BULK_LINK_TTL_SECONDS", 3_600, minimum=60, maximum=3_600, group="storage", description="Bulk recording manifest URL lifetime")
R2_UPLOAD_CLAIM_TTL_SECONDS = _limit("R2_UPLOAD_CLAIM_TTL_SECONDS", 600, minimum=60, maximum=900, group="storage", description="Signed upload-claim lifetime")
TTS_REVIEW_CLAIM_TTL_SECONDS = _limit("TTS_REVIEW_CLAIM_TTL_SECONDS", 900, minimum=60, maximum=1_800, group="storage", description="Signed TTS quality-review claim lifetime")
R2_OBJECT_CACHE_MAX_AGE_SECONDS = _limit("R2_OBJECT_CACHE_MAX_AGE_SECONDS", 86_400, minimum=0, maximum=31_536_000, group="storage", description="Private R2 object cache metadata lifetime")
R2_URL_MIN_TTL_SECONDS = _limit("R2_URL_MIN_TTL_SECONDS", 60, minimum=1, maximum=60, group="storage", description="Minimum presigned URL lifetime")
R2_UPLOAD_URL_MAX_TTL_SECONDS = _limit("R2_UPLOAD_URL_MAX_TTL_SECONDS", 900, minimum=60, maximum=3_600, group="storage", description="Maximum presigned upload URL lifetime")
R2_DOWNLOAD_URL_MAX_TTL_SECONDS = _limit("R2_DOWNLOAD_URL_MAX_TTL_SECONDS", 3_600, minimum=60, maximum=86_400, group="storage", description="Maximum presigned download URL lifetime")
R2_CLAIM_MAX_TTL_SECONDS = _limit("R2_CLAIM_MAX_TTL_SECONDS", 1_800, minimum=60, maximum=3_600, group="storage", description="Maximum signed R2 claim lifetime")
R2_CLIENT_MAX_ATTEMPTS = _limit("R2_CLIENT_MAX_ATTEMPTS", 3, minimum=1, maximum=10, group="storage", description="R2 SDK request attempts")
PHOTO_BATCH_MAX_ITEMS = _limit("PHOTO_BATCH_MAX_ITEMS", 5, minimum=1, maximum=20, group="storage", description="Photos per completion request")
PHOTO_MAX_BYTES = _limit("PHOTO_MAX_BYTES", 2 * MIB, minimum=KIB, maximum=2 * MIB, group="storage", description="Compressed original photo bytes")
PHOTO_THUMBNAIL_MAX_BYTES = _limit("PHOTO_THUMBNAIL_MAX_BYTES", 300 * KIB, minimum=500, maximum=300 * KIB, group="storage", description="Photo thumbnail bytes")
PHOTO_MAX_DIMENSION = _limit("PHOTO_MAX_DIMENSION", 2_000, minimum=100, maximum=2_000, group="storage", description="Photo width/height pixels")
PHOTO_THUMBNAIL_MAX_DIMENSION = _limit("PHOTO_THUMBNAIL_MAX_DIMENSION", 480, minimum=100, maximum=480, group="storage", description="Photo thumbnail width/height pixels")

# Supabase row inventories, retention and interaction growth.
ACCOUNT_INVENTORY_LIMIT = _limit("ACCOUNT_INVENTORY_LIMIT", 1_000, minimum=100, maximum=1_000, group="database", description="Accounts retained")
ACCOUNT_LIST_LIMIT = _limit("ACCOUNT_LIST_LIMIT", 1_000, minimum=100, maximum=1_000, group="database", description="Accounts loaded per list")
MATCH_INVENTORY_LIMIT = _limit("MATCH_INVENTORY_LIMIT", 500, minimum=100, maximum=500, group="database", description="Matches retained/loaded")
JUDGE_MAX_PER_MATCH = _limit("JUDGE_MAX_PER_MATCH", 51, minimum=51, maximum=51, group="database", description="Total human and official AI score sheets per match")
REGISTRATION_MAX_PER_EDITION = _limit("REGISTRATION_MAX_PER_EDITION", 500, minimum=1, maximum=500, group="database", description="Registrations per edition")
REGISTRATION_EDITION_HISTORY_LIMIT = _limit("REGISTRATION_EDITION_HISTORY_LIMIT", 100, minimum=1, maximum=100, group="database", description="Registration editions listed")
OPEN_DB_MAX_TOPICS = _limit("OPEN_DB_MAX_TOPICS", 2_000, minimum=100, maximum=2_000, group="database", description="Public topic-bank rows")
TOPIC_BANK_MAX = _limit("TOPIC_BANK_MAX", 2_000, minimum=100, maximum=2_000, group="database", description="Topic bank rows")
COMMENT_HISTORY_LIMIT = _limit("COMMENT_HISTORY_LIMIT", 100, minimum=1, maximum=100, group="database", description="Comments returned per motion")
COMMENT_MAX_PER_MOTION = _limit("COMMENT_MAX_PER_MOTION", 100, minimum=1, maximum=100, group="database", description="Comments retained per motion")
VOTE_ANALYSIS_MOTION_LIMIT = _limit("VOTE_ANALYSIS_MOTION_LIMIT", 200, minimum=1, maximum=200, group="database", description="Motions per vote analysis")
VOTE_PENDING_MOTION_LIMIT = _limit("VOTE_PENDING_MOTION_LIMIT", 10, minimum=1, maximum=10, group="database", description="Pending topic/deposition motions")
VIDEO_REPLAY_LIST_LIMIT = _limit("VIDEO_REPLAY_LIST_LIMIT", 200, minimum=1, maximum=200, group="database", description="Videos loaded on replay page")
VIDEO_OPTION_LIMIT = _limit("VIDEO_OPTION_LIMIT", 500, minimum=1, maximum=500, group="database", description="Video options loaded")
VIDEO_IMPORT_MAX_ROWS = _limit("VIDEO_IMPORT_MAX_ROWS", 500, minimum=1, maximum=500, group="database", description="Rows per video import")
VIDEO_TOTAL_LIMIT = _limit("VIDEO_TOTAL_LIMIT", 2_000, minimum=VIDEO_IMPORT_MAX_ROWS, maximum=2_000, group="database", description="Videos retained")
VIDEO_COMMENT_MAX_PER_VIDEO = _limit("VIDEO_COMMENT_MAX_PER_VIDEO", 1_000, minimum=1, maximum=1_000, group="database", description="Comments per video")
VIDEO_COMMENT_MAX_PER_USER_DAY = _limit("VIDEO_COMMENT_MAX_PER_USER_DAY", 50, minimum=1, maximum=50, group="database", description="Video comments per user/day")
VIDEO_VIEW_DEDUPE_HOURS = _limit("VIDEO_VIEW_DEDUPE_HOURS", 24, minimum=1, maximum=7 * 24, group="database", description="Video-view deduplication window")
VIDEO_COMMENT_RATE_WINDOW_HOURS = _limit("VIDEO_COMMENT_RATE_WINDOW_HOURS", 24, minimum=1, maximum=7 * 24, group="database", description="Video-comment user quota window")
VIDEO_PROGRESS_MAX_SECONDS = _limit("VIDEO_PROGRESS_MAX_SECONDS", 24 * 60 * 60, minimum=60, maximum=7 * 24 * 60 * 60, group="database", description="Maximum accepted video progress timestamp")
BUG_REPORT_MAX_TOTAL = _limit("BUG_REPORT_MAX_TOTAL", 5_000, minimum=100, maximum=5_000, group="database", description="Bug reports retained")
BUG_REPORT_MAX_PER_USER_DAY = _limit("BUG_REPORT_MAX_PER_USER_DAY", 10, minimum=1, maximum=10, group="database", description="Bug reports per user/day")
BUG_REPORT_RATE_WINDOW_HOURS = _limit("BUG_REPORT_RATE_WINDOW_HOURS", 24, minimum=1, maximum=7 * 24, group="database", description="Bug-report user quota window")
BUG_REPORT_RECENT_LIMIT = _limit("BUG_REPORT_RECENT_LIMIT", 30, minimum=1, maximum=30, group="database", description="Recent bug reports loaded")
PUSH_RECIPIENT_LIMIT = _limit("PUSH_RECIPIENT_LIMIT", 500, minimum=1, maximum=500, group="database", description="Recipients per push operation")
PUSH_SEND_CONCURRENCY = _limit("PUSH_SEND_CONCURRENCY", 8, minimum=1, maximum=8, group="runtime", description="Concurrent outbound Web Push requests")
PUSH_ACTIVE_DEVICES_PER_USER = _limit("PUSH_ACTIVE_DEVICES_PER_USER", 5, minimum=1, maximum=5, group="database", description="Active push devices per user")
PUSH_INACTIVE_RETENTION_DAYS = _limit("PUSH_INACTIVE_RETENTION_DAYS", 90, minimum=1, maximum=400, group="database", description="Inactive push subscription retention")
PUSH_SUBSCRIPTION_MAX_BYTES = _limit("PUSH_SUBSCRIPTION_MAX_BYTES", 4 * KIB, minimum=512, maximum=4 * KIB, group="database", description="Serialized push subscription bytes")
PUSH_ENDPOINT_MAX_CHARS = _limit("PUSH_ENDPOINT_MAX_CHARS", 2_048, minimum=128, maximum=2_048, group="database", description="Push endpoint characters")
PUSH_KEY_MAX_CHARS = _limit("PUSH_KEY_MAX_CHARS", 512, minimum=64, maximum=512, group="database", description="Push key characters")
LOGIN_RECORD_RETENTION_DAYS = _limit("LOGIN_RECORD_RETENTION_DAYS", 400, minimum=31, maximum=400, group="database", description="Login record retention")
NOTIFICATION_READ_RETENTION_DAYS = _limit("NOTIFICATION_READ_RETENTION_DAYS", 400, minimum=31, maximum=400, group="database", description="Notification-read retention")
AI_USAGE_RETENTION_DAYS = _limit("AI_USAGE_RETENTION_DAYS", 400, minimum=90, maximum=400, group="database", description="AI usage telemetry retention")
COMMITTEE_COOKIE_MAX_AGE_DAYS = _limit("COMMITTEE_COOKIE_MAX_AGE_DAYS", 180, minimum=1, maximum=400, group="database", description="Committee login cookie lifetime")
COMMITTEE_SESSION_MAX_AGE_SECONDS = COMMITTEE_COOKIE_MAX_AGE_DAYS * 24 * 60 * 60
COMMITTEE_SESSION_CLOCK_SKEW_SECONDS = _limit("COMMITTEE_SESSION_CLOCK_SKEW_SECONDS", 60, minimum=0, maximum=300, group="runtime", description="Allowed future clock skew in committee session tokens")
COMMITTEE_SESSION_TOKEN_MAX_CHARS = _limit("COMMITTEE_SESSION_TOKEN_MAX_CHARS", 2_048, minimum=256, maximum=4_096, group="runtime", description="Committee session token characters")
LOGIN_RATE_WINDOW_SECONDS = _limit("LOGIN_RATE_WINDOW_SECONDS", 5 * 60, minimum=60, maximum=60 * 60, group="runtime", description="In-process login attempt rolling window")
LOGIN_RATE_MAX_PER_CLIENT_ACCOUNT = _limit("LOGIN_RATE_MAX_PER_CLIENT_ACCOUNT", 8, minimum=1, maximum=30, group="runtime", description="Login attempts per client and account per window")
LOGIN_RATE_MAX_PER_CLIENT = _limit("LOGIN_RATE_MAX_PER_CLIENT", 30, minimum=1, maximum=100, group="runtime", description="Login attempts per client across accounts per window")
LOGIN_RATE_MAX_GLOBAL = _limit("LOGIN_RATE_MAX_GLOBAL", 120, minimum=10, maximum=500, group="runtime", description="Login attempts across this worker per window")
HOME_ACTIVE_MEMBER_WINDOW_HOURS = _limit("HOME_ACTIVE_MEMBER_WINDOW_HOURS", 24, minimum=1, maximum=7 * 24, group="database", description="Home active-member window")
OPENROUTER_CREDIT_TIMEOUT_SECONDS = _limit("OPENROUTER_CREDIT_TIMEOUT_SECONDS", 12, minimum=1, maximum=60, group="ai", description="OpenRouter credit lookup timeout")

# Bounded utility workflows.
SCHEDULE_MAX_TEAMS = _limit("SCHEDULE_MAX_TEAMS", 128, minimum=2, maximum=128, group="workflow", description="Teams per schedule draw")
SCHEDULE_MAX_TEAM_NAME_CHARS = _limit("SCHEDULE_MAX_TEAM_NAME_CHARS", 100, minimum=1, maximum=100, group="workflow", description="Team-name characters in draw")


def _validate_relationships() -> None:
    if AI_FACTORY_CANDIDATE_DEFAULT > AI_FACTORY_CANDIDATE_MAX:
        raise RuntimeError(
            "AI_FACTORY_CANDIDATE_DEFAULT cannot exceed AI_FACTORY_CANDIDATE_MAX"
        )
    if AI_FACTORY_MANAGER_CONCURRENCY > AI_FACTORY_CONCURRENCY:
        raise RuntimeError(
            "AI_FACTORY_MANAGER_CONCURRENCY cannot exceed AI_FACTORY_CONCURRENCY"
        )
    if not (
        LOGIN_RATE_MAX_PER_CLIENT_ACCOUNT
        <= LOGIN_RATE_MAX_PER_CLIENT
        <= LOGIN_RATE_MAX_GLOBAL
    ):
        raise RuntimeError(
            "Login limits must satisfy per-client-account <= per-client <= global"
        )
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
    if PHOTO_THUMBNAIL_MAX_BYTES > PHOTO_MAX_BYTES:
        raise RuntimeError("PHOTO_THUMBNAIL_MAX_BYTES cannot exceed PHOTO_MAX_BYTES")
    if PHOTO_THUMBNAIL_MAX_DIMENSION > PHOTO_MAX_DIMENSION:
        raise RuntimeError("PHOTO_THUMBNAIL_MAX_DIMENSION cannot exceed PHOTO_MAX_DIMENSION")
    if VIDEO_IMPORT_MAX_ROWS > VIDEO_TOTAL_LIMIT:
        raise RuntimeError("VIDEO_IMPORT_MAX_ROWS cannot exceed VIDEO_TOTAL_LIMIT")
    if LIVE_FREE_SESSION_MAX_SECONDS < LIVE_FREE_MAX_MINUTES * 2 * 60:
        raise RuntimeError(
            "LIVE_FREE_SESSION_MAX_SECONDS must cover both Free De side budgets"
        )
    if ROOM_CONTROL_RATE_BURST_MESSAGES < ROOM_CONTROL_RATE_MESSAGES_PER_SECOND:
        raise RuntimeError(
            "ROOM_CONTROL_RATE_BURST_MESSAGES must cover one second of control traffic"
        )
    if ROOM_CRITICAL_RATE_BURST_MESSAGES < ROOM_CRITICAL_RATE_MESSAGES_PER_SECOND:
        raise RuntimeError(
            "ROOM_CRITICAL_RATE_BURST_MESSAGES must cover one second of critical traffic"
        )
    if ROOM_TRANSCRIPT_TOTAL_MAX_CHARS < ROOM_TRANSCRIPT_ITEM_MAX_CHARS:
        raise RuntimeError(
            "ROOM_TRANSCRIPT_TOTAL_MAX_CHARS must cover one transcript item"
        )
    if LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS >= LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS:
        raise RuntimeError(
            "LIVE_CONTEXT_COMPRESSION_TARGET_TOKENS must be lower than "
            "LIVE_CONTEXT_COMPRESSION_TRIGGER_TOKENS"
        )
    if OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS < OPENROUTER_WEB_SEARCH_MAX_RESULTS:
        raise RuntimeError(
            "OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS must be at least "
            "OPENROUTER_WEB_SEARCH_MAX_RESULTS"
        )
    if LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS >= LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS:
        raise RuntimeError(
            "LIVE_TOKEN_RESPONSE_CACHE_TTL_SECONDS must be shorter than the "
            "Gemini new-session window"
        )
    if LIVE_TOKEN_MINT_TIMEOUT_SECONDS >= LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS:
        raise RuntimeError(
            "LIVE_TOKEN_MINT_TIMEOUT_SECONDS must be shorter than the new-session window"
        )
    if (
        LIVE_TOKEN_MINT_TIMEOUT_SECONDS
        + LIVE_TOKEN_LEDGER_DB_TIMEOUT_SECONDS
        + LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS
        >= LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS
    ):
        raise RuntimeError(
            "Solo Live mint, DB checkout/ledger, and cache safety bounds must fit "
            "inside the new-session window"
        )
    if LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS >= LIVE_TOKEN_NEW_SESSION_WINDOW_SECONDS:
        raise RuntimeError(
            "LIVE_TOKEN_RESPONSE_CACHE_SAFETY_SECONDS must be shorter than the "
            "new-session window"
        )
    if LIVE_PRACTICE_CLAIM_TTL_SECONDS < 2 * 60 * 60:
        raise RuntimeError(
            "LIVE_PRACTICE_CLAIM_TTL_SECONDS must cover the longest linked Mock"
        )
    if LIVE_PRACTICE_CLAIM_MAX_CHARS < LIVE_SYSTEM_PROMPT_MAX_CHARS * 6:
        raise RuntimeError(
            "LIVE_PRACTICE_CLAIM_MAX_CHARS must cover a signed Unicode Live prompt"
        )


_validate_relationships()


STARTUP_LIMIT_NAMES = (
    "UVICORN_LIMIT_CONCURRENCY",
    "UVICORN_WS_MAX_SIZE",
    "UVICORN_WS_MAX_QUEUE",
    "MALLOC_ARENA_MAX",
    "MALLOC_TRIM_THRESHOLD_",
)


def startup_limits() -> dict[str, int]:
    """Named startup contract consumed by ``deploy/start.sh``."""
    values = (
        UVICORN_LIMIT_CONCURRENCY, UVICORN_WS_MAX_SIZE, UVICORN_WS_MAX_QUEUE,
        MALLOC_ARENA_MAX, MALLOC_TRIM_THRESHOLD_BYTES,
    )
    return dict(zip(STARTUP_LIMIT_NAMES, values, strict=True))


def effective_limits() -> dict[str, dict]:
    """Serializable effective registry, grouped by environment-variable name."""
    return {name: asdict(spec) for name, spec in sorted(LIMIT_SPECS.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect effective system limits")
    parser.add_argument("--json", action="store_true", help="print the complete limit registry")
    parser.add_argument("--startup", action="store_true", help="print shell startup limits")
    args = parser.parse_args()
    if args.startup:
        for name, value in startup_limits().items():
            print(f"{name}={value}")
    else:
        print(json.dumps(effective_limits(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
