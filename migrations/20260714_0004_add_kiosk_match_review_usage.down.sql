-- Roll back the kiosk full-match usage type only when no such ledger rows
-- remain. PostgreSQL validates the restored check and fails closed otherwise.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT IF EXISTS ai_fund_usage_logs_feature_check,
    DROP CONSTRAINT IF EXISTS chk_ai_fund_usage_feature;

ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT chk_ai_fund_usage_feature
    CHECK (feature IN (
        'speech_review',
        'strategy',
        'web_research',
        'fact_check',
        'free_debate_live',
        'full_mock_live',
        'vote_review',
        'vote_analysis',
        'vote_discussion',
        'tts_review',
        'tts_script_analysis',
        'llm_review'
    ));
