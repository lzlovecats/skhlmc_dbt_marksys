-- Refuse to remove official score or provider-attempt audit evidence.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.scores WHERE judge_kind = 'ai' LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM public.official_ai_judge_runs LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM public.official_ai_judge_attempts LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM public.ai_fund_usage_logs
        WHERE feature = 'official_ai_judge' LIMIT 1
    ) THEN
        RAISE EXCEPTION
            'refusing to remove official AI judge result or audit evidence';
    END IF;
END $$;

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
        feature IN (
            'speech_review', 'strategy', 'competition_prep', 'web_research',
            'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review',
            'vote_analysis', 'vote_discussion', 'tts_review',
            'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts',
            'kiosk_match_review_tts', 'data_factory_generation'
        )
    );

DROP TABLE public.official_ai_judge_attempts;
DROP TABLE public.official_ai_judge_runs;
DROP INDEX public.idx_scores_one_official_ai_judge;
ALTER TABLE public.scores DROP CONSTRAINT scores_judge_kind_check;
ALTER TABLE public.scores DROP COLUMN judge_kind;
