-- Permanently remove the unused AI data factory and its historical records.
-- The shared AI Training TTS and LLM-submission workflows remain intact.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DELETE FROM public.ai_fund_usage_logs
WHERE feature = 'data_factory_generation';

DELETE FROM public.ai_training_audit
WHERE LEFT(target_type, 11) = 'ai_factory_'
   OR LEFT(action, 8) = 'factory_';

DROP TABLE public.ai_factory_transcript_segments;
DROP TABLE public.ai_factory_transcript_attempts;
DROP TABLE public.ai_factory_transcript_windows;
DROP TABLE public.ai_factory_transcript_runs;
DROP TABLE public.ai_factory_transcripts;
DROP TABLE public.ai_factory_release_items;
DROP TABLE public.ai_factory_releases;
DROP TABLE public.ai_factory_item_tags;
DROP TABLE public.ai_factory_topic_tags;
DROP TABLE public.ai_factory_items;
DROP TABLE public.ai_factory_attempts;
DROP TABLE public.ai_factory_jobs;
DROP TABLE public.ai_factory_sources;

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT ai_fund_usage_logs_feature_check
    CHECK (
        feature IN (
            'speech_review', 'strategy', 'competition_prep', 'web_research',
            'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review',
            'vote_analysis', 'vote_discussion', 'tts_review',
            'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts',
            'kiosk_match_review_tts', 'official_ai_judge', 'lmc_ai_chat',
            'lmc_ai_eval'
        )
    );
