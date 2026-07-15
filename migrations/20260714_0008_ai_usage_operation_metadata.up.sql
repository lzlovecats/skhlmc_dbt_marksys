-- Correlate multi-call AI tasks and account for character-billed TTS calls.
-- Prompt, transcript and synthesized text content remain outside this ledger.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.ai_fund_usage_logs
    ADD COLUMN billable_characters INTEGER NOT NULL DEFAULT 0
        CHECK (billable_characters >= 0),
    ADD COLUMN operation_id TEXT
        CHECK (
            operation_id IS NULL
            OR CHAR_LENGTH(operation_id) BETWEEN 1 AND 200
        ),
    ADD COLUMN operation_stage TEXT
        CHECK (
            operation_stage IS NULL
            OR CHAR_LENGTH(operation_stage) BETWEEN 1 AND 80
        );

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
        'kiosk_match_review',
        'tts',
        'kiosk_match_review_tts'
    ));

CREATE INDEX idx_ai_fund_usage_logs_operation
    ON public.ai_fund_usage_logs(operation_id, operation_stage)
    WHERE operation_id IS NOT NULL;
