-- Refuse to erase transcript lineage, review decisions or provider attempts.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.ai_factory_transcript_segments LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_transcript_attempts LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_transcript_windows LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_transcript_runs LIMIT 1)
        OR EXISTS (SELECT 1 FROM public.ai_factory_transcripts LIMIT 1)
        OR EXISTS (
            SELECT 1 FROM public.ai_training_audit
            WHERE target_type IN (
                'ai_factory_transcript',
                'ai_factory_transcript_run',
                'ai_factory_transcript_attempt',
                'ai_factory_transcript_segment'
            )
            LIMIT 1
        )
    THEN
        RAISE EXCEPTION
            'refusing to remove used transcript factory data or audit evidence';
    END IF;
END $$;

DROP TABLE public.ai_factory_transcript_segments;
DROP TABLE public.ai_factory_transcript_attempts;
DROP TABLE public.ai_factory_transcript_windows;
DROP TABLE public.ai_factory_transcript_runs;
DROP TABLE public.ai_factory_transcripts;

COMMENT ON TABLE public.ai_factory_sources IS
    'skhlmc-feature:data_factory:20260720_0001';

DROP INDEX IF EXISTS public.idx_ai_training_audit_created_at;
CREATE INDEX idx_ai_training_audit_created_at
    ON public.ai_training_audit(created_at)
    WHERE action NOT IN (
        'consent_granted',
        'consent_withdrawn',
        'submission_withdrawn',
        'factory_source_created',
        'factory_source_withdrawn',
        'factory_item_reviewed',
        'factory_item_withdrawn',
        'factory_item_invalidated',
        'factory_topic_tag_approved',
        'factory_topic_tag_retired',
        'factory_release_published',
        'factory_release_invalidated'
    );
