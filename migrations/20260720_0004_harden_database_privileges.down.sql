-- Break-glass rollback for the legacy Supabase browser grants.  Applying this
-- file deliberately reopens the pre-hardening surface and therefore requires
-- the migration runner's explicit rollback confirmation.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM app_backend;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM app_backend;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM app_backend;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM app_backend;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM app_backend;

DO $migration$
BEGIN
    EXECUTE 'REVOKE CONNECT ON DATABASE '
        || quote_ident(current_database()) || ' FROM app_backend';
END
$migration$;

GRANT USAGE ON SCHEMA public TO PUBLIC, anon, authenticated;

GRANT ALL PRIVILEGES ON TABLE
    public.accounts,
    public.ai_coach_live_briefs,
    public.ai_fund_transactions,
    public.ai_fund_usage_logs,
    public.app_config,
    public.best_debater_rankings,
    public.bug_reports,
    public.committee_vote_activity_view,
    public.competition_registration_settings,
    public.competition_registrations,
    public.debater_scores,
    public.debaters,
    public.lateness_fund_expenses,
    public.lateness_fund_periods,
    public.lateness_fund_records,
    public.llm_training_submissions,
    public.login_records,
    public.match_photos,
    public.match_roster_links,
    public.match_videos,
    public.matches,
    public.motion_comments,
    public.notification_reads,
    public.projector_state,
    public.push_subscriptions,
    public.score_drafts,
    public.scores,
    public.topic_removal_vote_ballots,
    public.topic_removal_votes,
    public.topic_vote_ballots,
    public.topic_votes,
    public.topics,
    public.tts_lexicon,
    public.tts_scripts,
    public.tts_voice_consents,
    public.tts_voice_recordings,
    public.video_chapters,
    public.video_comments,
    public.video_progress,
    public.video_views,
    public.video_votes
TO anon, authenticated;

GRANT ALL PRIVILEGES ON SEQUENCE
    public.ai_fund_transactions_id_seq,
    public.ai_fund_usage_logs_id_seq,
    public.bug_reports_id_seq,
    public.competition_registrations_id_seq,
    public.lateness_fund_expenses_id_seq,
    public.lateness_fund_records_id_seq,
    public.llm_training_submissions_id_seq,
    public.login_record_id_seq,
    public.match_photos_id_seq,
    public.match_videos_id_seq,
    public.motion_comments_id_seq,
    public.tts_voice_recordings_id_seq,
    public.video_comments_id_seq,
    public.video_views_id_seq
TO anon, authenticated;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO PUBLIC, anon, authenticated;

-- app_backend is cluster-wide and may be shared by another database.  Keep the
-- inert NOLOGIN role after removing this database's grants.
