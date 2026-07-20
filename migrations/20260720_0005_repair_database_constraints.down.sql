-- Restore the pre-cleanup catalog.  The historical topic foreign key is
-- recreated unvalidated so rows preserved after a passed removal do not make
-- this explicit rollback fail.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DROP INDEX IF EXISTS public.idx_bug_reports_reporter_created;
DROP INDEX IF EXISTS public.idx_bug_reports_status_created;
DROP INDEX IF EXISTS public.idx_notification_reads_read_at;
DROP INDEX IF EXISTS public.idx_login_records_logged_in_at;
DROP INDEX IF EXISTS public.idx_push_subscriptions_inactive_updated;
DROP INDEX IF EXISTS public.idx_video_comments_user_created;
DROP INDEX IF EXISTS public.idx_tts_voice_recordings_r2_key;
DROP INDEX IF EXISTS public.idx_match_photos_r2_key;

CREATE INDEX idx_competition_prep_manuscripts_project
    ON public.competition_prep_manuscripts(project_id, slot);
CREATE INDEX idx_match_roster_links_token
    ON public.match_roster_links(roster_token);
CREATE INDEX idx_tvb_topic_text
    ON public.topic_vote_ballots(topic_text);
CREATE INDEX idx_trvb_topic_text
    ON public.topic_removal_vote_ballots(topic_text);

ALTER TABLE public.score_drafts
    ADD CONSTRAINT score_drafts_match_id_fkey
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id)
        ON DELETE CASCADE,
    ADD CONSTRAINT score_drafts_match_id_judge_name_side_key
        UNIQUE (match_id, judge_name, side),
    ADD CONSTRAINT score_drafts_match_judge_side_key
        UNIQUE (match_id, judge_name, side);

ALTER TABLE public.scores
    ADD CONSTRAINT scores_match_id_fkey
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id)
        ON DELETE CASCADE;

ALTER TABLE public.topic_removal_votes
    ADD CONSTRAINT topic_removal_votes_proposer_user_fkey
        FOREIGN KEY (proposer_user_id) REFERENCES public.accounts(user_id),
    ADD CONSTRAINT fk_topic_removal_votes_topic
        FOREIGN KEY (topic_text) REFERENCES public.topics(topic_text)
        ON DELETE CASCADE NOT VALID;
