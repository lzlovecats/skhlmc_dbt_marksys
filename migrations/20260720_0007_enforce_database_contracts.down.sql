-- Remove the new enforcement while retaining deterministic data cleanup.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.llm_training_submissions
    DROP CONSTRAINT llm_training_submissions_ai_review_status_check;

ALTER TABLE public.ai_coach_live_briefs
    DROP CONSTRAINT ai_coach_live_briefs_expiry_check,
    ALTER COLUMN expires_at TYPE TEXT
        USING to_char(
            expires_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD HH24:MI:SS'
        ),
    ALTER COLUMN created_at TYPE TEXT
        USING to_char(
            created_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD HH24:MI:SS'
        );

ALTER TABLE public.r2_upload_intents
    DROP CONSTRAINT r2_upload_intents_completion_check,
    DROP CONSTRAINT r2_upload_intents_status_check,
    DROP CONSTRAINT r2_upload_intents_object_keys_check,
    ALTER COLUMN object_keys TYPE TEXT USING object_keys::TEXT;

ALTER TABLE public.topic_removal_vote_ballots
    ALTER COLUMN vote_choice DROP NOT NULL;
ALTER TABLE public.topic_vote_ballots
    ALTER COLUMN vote_choice DROP NOT NULL;

ALTER TABLE public.topic_removal_votes
    DROP CONSTRAINT topic_removal_votes_reasons_array_check,
    DROP CONSTRAINT topic_removal_votes_pending_deadline_check,
    DROP CONSTRAINT topic_removal_votes_threshold_check,
    DROP CONSTRAINT topic_removal_votes_status_check,
    ALTER COLUMN approval_threshold DROP NOT NULL,
    ALTER COLUMN created_at DROP NOT NULL,
    ALTER COLUMN status DROP DEFAULT,
    ALTER COLUMN removal_reasons DROP NOT NULL,
    ALTER COLUMN removal_reasons DROP DEFAULT,
    ALTER COLUMN removal_reasons TYPE TEXT USING removal_reasons::TEXT;

ALTER TABLE public.topic_votes
    DROP CONSTRAINT topic_votes_pending_deadline_check,
    DROP CONSTRAINT topic_votes_threshold_check,
    DROP CONSTRAINT topic_votes_status_check,
    ALTER COLUMN approval_threshold DROP NOT NULL,
    ALTER COLUMN created_at DROP NOT NULL,
    ALTER COLUMN status SET DEFAULT 'PENDING';

ALTER TABLE public.accounts
    DROP CONSTRAINT accounts_status_check,
    ALTER COLUMN account_status DROP NOT NULL,
    ALTER COLUMN account_status DROP DEFAULT;
