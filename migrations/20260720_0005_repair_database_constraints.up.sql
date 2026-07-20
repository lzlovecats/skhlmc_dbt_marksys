-- Remove duplicate catalog objects, preserve resolved removal-vote history,
-- and add the useful bootstrap indexes that were never ledgered in production.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.topic_removal_votes
    DROP CONSTRAINT IF EXISTS fk_topic_removal_votes_topic,
    DROP CONSTRAINT IF EXISTS topic_removal_votes_proposer_user_fkey;

ALTER TABLE public.score_drafts
    DROP CONSTRAINT IF EXISTS score_drafts_match_id_fkey,
    DROP CONSTRAINT IF EXISTS score_drafts_match_id_judge_name_side_key,
    DROP CONSTRAINT IF EXISTS score_drafts_match_judge_side_key;

ALTER TABLE public.scores
    DROP CONSTRAINT IF EXISTS scores_match_id_fkey;

DROP INDEX IF EXISTS public.idx_competition_prep_manuscripts_project;
DROP INDEX IF EXISTS public.idx_match_roster_links_token;
DROP INDEX IF EXISTS public.idx_tvb_topic_text;
DROP INDEX IF EXISTS public.idx_trvb_topic_text;

CREATE UNIQUE INDEX idx_match_photos_r2_key
    ON public.match_photos(r2_key)
    WHERE r2_key IS NOT NULL;
CREATE UNIQUE INDEX idx_tts_voice_recordings_r2_key
    ON public.tts_voice_recordings(r2_key)
    WHERE r2_key IS NOT NULL;
CREATE INDEX idx_video_comments_user_created
    ON public.video_comments(user_id, created_at DESC);
CREATE INDEX idx_push_subscriptions_inactive_updated
    ON public.push_subscriptions(updated_at)
    WHERE is_active=FALSE;
CREATE INDEX idx_login_records_logged_in_at
    ON public.login_records(logged_in_at);
CREATE INDEX idx_notification_reads_read_at
    ON public.notification_reads(read_at);
CREATE INDEX idx_bug_reports_status_created
    ON public.bug_reports(status, created_at DESC);
CREATE INDEX idx_bug_reports_reporter_created
    ON public.bug_reports(reporter_user_id, created_at DESC);

