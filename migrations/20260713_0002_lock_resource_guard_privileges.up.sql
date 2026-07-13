-- These ledgers are backend-only. Supabase default privileges grant new tables
-- to browser roles, so revoke them explicitly until the full RLS perimeter is
-- introduced by a later migration.

REVOKE ALL PRIVILEGES ON TABLE
    practice_daily_usage,
    bandwidth_usage_logs,
    r2_upload_intents,
    ai_coach_prepare_usage
FROM PUBLIC, anon, authenticated;

REVOKE ALL PRIVILEGES ON SEQUENCE
    bandwidth_usage_logs_id_seq,
    ai_coach_prepare_usage_id_seq
FROM PUBLIC, anon, authenticated;
