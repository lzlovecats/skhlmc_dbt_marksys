-- Restore the former match-based forum-link contract.  Exact links to legacy
-- videos without a match cannot be represented and therefore block rollback
-- instead of being silently discarded.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.ghost_forum_thread_videos AS link
        JOIN public.match_videos AS video ON video.id=link.video_id
        WHERE video.match_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Cannot roll back while forum links reference standalone videos';
    END IF;
END $$;

CREATE TABLE public.ghost_forum_thread_matches (
    thread_id BIGINT NOT NULL,
    match_id  TEXT NOT NULL,
    PRIMARY KEY (thread_id, match_id),
    CONSTRAINT fk_ghost_thread_match_thread
        FOREIGN KEY (thread_id) REFERENCES public.ghost_forum_threads(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_match_match
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id)
        ON DELETE CASCADE
);

INSERT INTO public.ghost_forum_thread_matches(thread_id, match_id)
SELECT DISTINCT link.thread_id, video.match_id
FROM public.ghost_forum_thread_videos AS link
JOIN public.match_videos AS video ON video.id=link.video_id
WHERE video.match_id IS NOT NULL;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_matches FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_matches FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

DROP TABLE public.ghost_forum_thread_history_events;
DROP TABLE public.ghost_forum_thread_videos;
