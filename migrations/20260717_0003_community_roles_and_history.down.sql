-- Remove committee community data and restore the pre-consolidation role keys.
-- The retired sql_password is intentionally never restored.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DROP TABLE public.ghost_forum_thread_photos;
DROP TABLE public.ghost_forum_thread_matches;
DROP TABLE public.ghost_forum_reactions;
DROP TABLE public.ghost_forum_posts;
DROP TABLE public.ghost_forum_threads;
DROP TABLE public.history_event_photos;
DROP TABLE public.history_event_matches;
DROP TABLE public.history_events;
DROP TABLE public.committee_memberships;
DROP TABLE public.recent_match_notifications;
DROP TABLE public.recent_matches;

DO $migration$
DECLARE
    ai_accounts JSONB;
    senior_accounts JSONB;
BEGIN
    SELECT COALESCE(value, '[]'::jsonb)
    INTO ai_accounts
    FROM public.app_config
    WHERE key='ai_managers';
    ai_accounts := COALESCE(ai_accounts, '[]'::jsonb);

    SELECT COALESCE(value, '[]'::jsonb)
    INTO senior_accounts
    FROM public.app_config
    WHERE key='senior_committee_members';
    senior_accounts := COALESCE(senior_accounts, '[]'::jsonb);

    INSERT INTO public.app_config
        (key, namespace, value, value_type, is_secret, updated_at)
    VALUES
        ('tts_recording_reviewers', 'access', ai_accounts, 'array', FALSE, NOW()),
        ('ai_fund_treasurers', 'access', ai_accounts, 'array', FALSE, NOW()),
        ('lateness_fund_managers', 'access', senior_accounts, 'array', FALSE, NOW())
    ON CONFLICT (key) DO UPDATE SET
        namespace=EXCLUDED.namespace,
        value=EXCLUDED.value,
        value_type=EXCLUDED.value_type,
        is_secret=EXCLUDED.is_secret,
        updated_at=EXCLUDED.updated_at;

    DELETE FROM public.app_config
    WHERE key IN ('ai_managers', 'senior_committee_members');
END
$migration$;
