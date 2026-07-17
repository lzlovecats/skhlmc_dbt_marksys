-- Remove forum engagement state and outbox.  pg_trgm is shared database
-- infrastructure and is intentionally retained on rollback.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DROP INDEX public.idx_ghost_forum_posts_body_trgm;
DROP INDEX public.idx_ghost_forum_threads_title_trgm;
DROP TABLE public.ghost_forum_notifications;
DROP TABLE public.ghost_forum_thread_user_state;
DROP TABLE public.ghost_forum_user_profiles;
