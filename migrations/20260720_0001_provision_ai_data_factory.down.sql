-- Refuse to erase factory lineage, governance evidence or accounted calls.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.ai_factory_release_items LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_releases LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_item_tags LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_topic_tags LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_items LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_attempts LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_jobs LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_sources LIMIT 1)
        OR EXISTS (
            SELECT 1 FROM public.ai_training_audit
            WHERE target_type IN (
                'ai_factory_source', 'ai_factory_job', 'ai_factory_attempt',
                'ai_factory_item', 'ai_factory_topic_tag', 'ai_factory_release'
            )
            LIMIT 1
        )
        OR EXISTS (
            SELECT 1 FROM public.ai_fund_usage_logs
            WHERE feature = 'data_factory_generation'
            LIMIT 1
        )
    THEN
        RAISE EXCEPTION
            'refusing to remove a used AI data factory or its audit evidence';
    END IF;
END $$;

DROP INDEX IF EXISTS public.idx_ai_training_audit_created_at;
CREATE INDEX idx_ai_training_audit_created_at
    ON public.ai_training_audit(created_at)
    WHERE action NOT IN (
        'consent_granted', 'consent_withdrawn', 'submission_withdrawn'
    );

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
        feature IN (
            'speech_review', 'strategy', 'competition_prep', 'web_research',
            'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review',
            'vote_analysis', 'vote_discussion', 'tts_review',
            'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts',
            'kiosk_match_review_tts'
        )
    );

DROP TABLE public.ai_factory_release_items;
DROP TABLE public.ai_factory_releases;
DROP TABLE public.ai_factory_item_tags;
DROP TABLE public.ai_factory_topic_tags;
DROP TABLE public.ai_factory_items;
DROP TABLE public.ai_factory_attempts;
DROP TABLE public.ai_factory_jobs;
DROP TABLE public.ai_factory_sources;
