-- Convert the two structured legacy text fields and make verified application
-- invariants explicit.  Resolved motions may keep a null historical deadline;
-- only pending motions require one.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

UPDATE public.accounts
SET account_status='inactive'
WHERE account_status IS NULL;

ALTER TABLE public.accounts
    ALTER COLUMN account_status SET DEFAULT 'inactive',
    ALTER COLUMN account_status SET NOT NULL,
    ADD CONSTRAINT accounts_status_check
        CHECK (BTRIM(account_status) IN ('admin','active','inactive'))
        NOT VALID;
ALTER TABLE public.accounts
    VALIDATE CONSTRAINT accounts_status_check;

ALTER TABLE public.topic_votes
    ALTER COLUMN status SET DEFAULT 'pending',
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN approval_threshold SET NOT NULL,
    ADD CONSTRAINT topic_votes_status_check
        CHECK (status IN ('pending','passed','rejected')) NOT VALID,
    ADD CONSTRAINT topic_votes_threshold_check
        CHECK (approval_threshold>0) NOT VALID,
    ADD CONSTRAINT topic_votes_pending_deadline_check
        CHECK (status<>'pending' OR deadline_date IS NOT NULL) NOT VALID;
ALTER TABLE public.topic_votes
    VALIDATE CONSTRAINT topic_votes_status_check;
ALTER TABLE public.topic_votes
    VALIDATE CONSTRAINT topic_votes_threshold_check;
ALTER TABLE public.topic_votes
    VALIDATE CONSTRAINT topic_votes_pending_deadline_check;

ALTER TABLE public.topic_removal_votes
    ALTER COLUMN removal_reasons TYPE JSONB
        USING COALESCE(removal_reasons, '[]')::JSONB,
    ALTER COLUMN removal_reasons SET DEFAULT '[]'::JSONB,
    ALTER COLUMN removal_reasons SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'pending',
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN approval_threshold SET NOT NULL,
    ADD CONSTRAINT topic_removal_votes_status_check
        CHECK (BTRIM(status) IN ('pending','passed','rejected')) NOT VALID,
    ADD CONSTRAINT topic_removal_votes_threshold_check
        CHECK (approval_threshold>0) NOT VALID,
    ADD CONSTRAINT topic_removal_votes_pending_deadline_check
        CHECK (BTRIM(status)<>'pending' OR deadline_date IS NOT NULL) NOT VALID,
    ADD CONSTRAINT topic_removal_votes_reasons_array_check
        CHECK (jsonb_typeof(removal_reasons)='array') NOT VALID;
ALTER TABLE public.topic_removal_votes
    VALIDATE CONSTRAINT topic_removal_votes_status_check;
ALTER TABLE public.topic_removal_votes
    VALIDATE CONSTRAINT topic_removal_votes_threshold_check;
ALTER TABLE public.topic_removal_votes
    VALIDATE CONSTRAINT topic_removal_votes_pending_deadline_check;
ALTER TABLE public.topic_removal_votes
    VALIDATE CONSTRAINT topic_removal_votes_reasons_array_check;

ALTER TABLE public.topic_vote_ballots
    ALTER COLUMN vote_choice SET NOT NULL;
ALTER TABLE public.topic_removal_vote_ballots
    ALTER COLUMN vote_choice SET NOT NULL;

ALTER TABLE public.r2_upload_intents
    ALTER COLUMN object_keys TYPE JSONB USING object_keys::JSONB,
    ADD CONSTRAINT r2_upload_intents_object_keys_check
        CHECK (
            jsonb_typeof(object_keys)='array'
            AND jsonb_array_length(object_keys)>0
        ) NOT VALID,
    ADD CONSTRAINT r2_upload_intents_status_check
        CHECK (
            status IN (
                'issued','completed','processing','consumed','orphan_deleted'
            )
        ) NOT VALID,
    ADD CONSTRAINT r2_upload_intents_completion_check
        CHECK (
            (status IN ('issued','processing') AND completed_at IS NULL)
            OR
            (status IN ('completed','consumed','orphan_deleted')
                AND completed_at IS NOT NULL)
        ) NOT VALID;
ALTER TABLE public.r2_upload_intents
    VALIDATE CONSTRAINT r2_upload_intents_object_keys_check;
ALTER TABLE public.r2_upload_intents
    VALIDATE CONSTRAINT r2_upload_intents_status_check;
ALTER TABLE public.r2_upload_intents
    VALIDATE CONSTRAINT r2_upload_intents_completion_check;

ALTER TABLE public.ai_coach_live_briefs
    ALTER COLUMN expires_at TYPE TIMESTAMPTZ
        USING expires_at::TIMESTAMP AT TIME ZONE 'UTC',
    ALTER COLUMN created_at TYPE TIMESTAMPTZ
        USING created_at::TIMESTAMP AT TIME ZONE 'UTC',
    ADD CONSTRAINT ai_coach_live_briefs_expiry_check
        CHECK (expires_at>created_at) NOT VALID;
ALTER TABLE public.ai_coach_live_briefs
    VALIDATE CONSTRAINT ai_coach_live_briefs_expiry_check;

ALTER TABLE public.llm_training_submissions
    ADD CONSTRAINT llm_training_submissions_ai_review_status_check
        CHECK (
            ai_review_status IN ('passed','failed','error')
        ) NOT VALID;
ALTER TABLE public.llm_training_submissions
    VALIDATE CONSTRAINT llm_training_submissions_ai_review_status_check;
