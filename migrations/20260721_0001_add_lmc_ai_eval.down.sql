-- Refuse destructive rollback after any campaign, review, output or eval usage.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.ai_eval_campaigns LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_eval_outputs LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_eval_reviews LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_fund_usage_logs WHERE feature='lmc_ai_eval' LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_eval_cases
            WHERE suite_id<>'lmc_ai_fixed_v1' OR suite_version<>1 LIMIT 1)
    THEN
        RAISE EXCEPTION 'refusing to remove used local AI evaluation data';
    END IF;
END $$;

DELETE FROM public.ai_eval_cases WHERE suite_id='lmc_ai_fixed_v1' AND suite_version=1;

DROP INDEX public.uq_ai_eval_usage_operation_stage;

ALTER TABLE public.ai_fund_usage_logs DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
    feature IN ('speech_review','strategy','competition_prep','web_research','fact_check','free_debate_live','full_mock_live','vote_review','vote_analysis','vote_discussion','tts_review','tts_script_analysis','llm_review','kiosk_match_review','tts','kiosk_match_review_tts','data_factory_generation','official_ai_judge','lmc_ai_chat')
);

DROP TABLE public.ai_eval_reviews;
DROP TABLE public.ai_eval_outputs;
DROP TABLE public.ai_eval_campaigns;
DROP TABLE public.ai_eval_cases;
