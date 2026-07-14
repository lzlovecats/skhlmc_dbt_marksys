-- Register the kiosk full-match audio review in the existing AI-fund ledger.
-- The application writes only provider usage metadata; raw match audio is
-- temporary R2 data and is never stored in this table.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

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
