-- Add bounded review reservations and explicit export-before-purge state.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.ai_eval_campaigns
    ADD COLUMN exported_at TIMESTAMPTZ,
    ADD COLUMN exported_by TEXT;

ALTER TABLE public.ai_eval_campaigns
    ADD CONSTRAINT ai_eval_campaigns_export_actor_length CHECK (
        exported_by IS NULL OR char_length(exported_by) BETWEEN 1 AND 100
    ),
    ADD CONSTRAINT ai_eval_campaigns_export_state CHECK (
        (exported_at IS NULL AND exported_by IS NULL)
        OR
        (exported_at IS NOT NULL AND exported_by IS NOT NULL
            AND status IN ('closed','invalidated'))
    );

ALTER TABLE public.ai_eval_reviews
    ADD COLUMN expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '24 hours'),
    ADD COLUMN released_at TIMESTAMPTZ,
    ADD COLUMN released_by TEXT,
    ADD COLUMN release_reason TEXT NOT NULL DEFAULT '';

UPDATE public.ai_eval_reviews
SET expires_at = assigned_at + INTERVAL '24 hours'
WHERE expires_at IS NULL;

ALTER TABLE public.ai_eval_reviews
    ALTER COLUMN expires_at SET NOT NULL,
    ADD CONSTRAINT ai_eval_reviews_expiry_order CHECK (expires_at > assigned_at),
    ADD CONSTRAINT ai_eval_reviews_release_actor_length CHECK (
        released_by IS NULL OR char_length(released_by) BETWEEN 1 AND 100
    ),
    ADD CONSTRAINT ai_eval_reviews_release_reason_length CHECK (
        char_length(release_reason) <= 500
    ),
    ADD CONSTRAINT ai_eval_reviews_release_state CHECK (
        (released_at IS NULL AND released_by IS NULL AND release_reason='')
        OR
        (released_at IS NOT NULL AND released_by IS NOT NULL
            AND submitted_at IS NULL)
    );

CREATE INDEX idx_ai_eval_reviews_pending
    ON public.ai_eval_reviews(campaign_id,reviewer_user_id,expires_at)
    WHERE submitted_at IS NULL AND released_at IS NULL;

COMMENT ON TABLE public.ai_eval_cases IS 'skhlmc-feature:eval:20260721_0002';
