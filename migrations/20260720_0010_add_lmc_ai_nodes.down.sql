-- Refuse to erase any registered node, active selection or local-AI usage.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.lmc_ai_nodes LIMIT 1)
        OR EXISTS (
            SELECT 1 FROM public.app_config
            WHERE key = 'lmc_ai_active_node_id'
              AND value <> '""'::jsonb
            LIMIT 1
        )
        OR EXISTS (
            SELECT 1 FROM public.ai_fund_usage_logs
            WHERE feature = 'lmc_ai_chat'
            LIMIT 1
        )
    THEN
        RAISE EXCEPTION
            'refusing to remove used local AI node or usage data';
    END IF;
END $$;

DELETE FROM public.app_config WHERE key = 'lmc_ai_active_node_id';

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
        feature IN (
            'speech_review', 'strategy', 'competition_prep', 'web_research',
            'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review',
            'vote_analysis', 'vote_discussion', 'tts_review',
            'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts',
            'kiosk_match_review_tts', 'data_factory_generation',
            'official_ai_judge'
        )
    );

ALTER TABLE public.ai_fund_usage_logs
    DROP COLUMN provider_duration_ms;

DROP TABLE public.lmc_ai_nodes;
