-- Restoring the old feature constraint fails closed while TTS usage rows exist.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DROP INDEX IF EXISTS public.idx_ai_fund_usage_logs_operation;

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT ai_fund_usage_logs_feature_check;

ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT ai_fund_usage_logs_feature_check
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
        'llm_review',
        'kiosk_match_review'
    ));

ALTER TABLE public.ai_fund_usage_logs
    DROP COLUMN operation_stage,
    DROP COLUMN operation_id,
    DROP COLUMN billable_characters;
