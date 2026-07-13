-- Restore only the direct grants observed immediately after 0001. PUBLIC had
-- no privileges and is intentionally not granted anything by this rollback.

GRANT ALL PRIVILEGES ON TABLE
    practice_daily_usage,
    bandwidth_usage_logs,
    r2_upload_intents,
    ai_coach_prepare_usage
TO anon, authenticated;

GRANT ALL PRIVILEGES ON SEQUENCE
    bandwidth_usage_logs_id_seq,
    ai_coach_prepare_usage_id_seq
TO anon, authenticated;
