-- Remove lifecycle fields only when none of their state would be lost.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.ai_eval_reviews WHERE released_at IS NOT NULL LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM public.ai_eval_campaigns WHERE exported_at IS NOT NULL LIMIT 1
    ) OR EXISTS (
        SELECT 1 FROM public.ai_training_audit
        WHERE action='eval_campaign_purged' LIMIT 1
    ) THEN
        RAISE EXCEPTION 'refusing to remove used local AI evaluation lifecycle state';
    END IF;
END $$;

DROP INDEX public.idx_ai_eval_reviews_pending;

ALTER TABLE public.ai_eval_reviews
    DROP CONSTRAINT ai_eval_reviews_release_state,
    DROP CONSTRAINT ai_eval_reviews_release_reason_length,
    DROP CONSTRAINT ai_eval_reviews_release_actor_length,
    DROP CONSTRAINT ai_eval_reviews_expiry_order,
    DROP COLUMN release_reason,
    DROP COLUMN released_by,
    DROP COLUMN released_at,
    DROP COLUMN expires_at;

ALTER TABLE public.ai_eval_campaigns
    DROP CONSTRAINT ai_eval_campaigns_export_state,
    DROP CONSTRAINT ai_eval_campaigns_export_actor_length,
    DROP COLUMN exported_by,
    DROP COLUMN exported_at;

COMMENT ON TABLE public.ai_eval_cases IS 'skhlmc-feature:eval:20260721_0001';
