-- Restore the former match-based team-history contract. Exact links to
-- standalone videos or multiple videos from one match cannot be represented,
-- so rollback fails closed instead of discarding links.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.history_event_videos AS link
        JOIN public.match_videos AS video ON video.id=link.video_id
        WHERE video.match_id IS NULL
    ) OR EXISTS (
        SELECT 1
        FROM public.history_event_videos AS link
        JOIN public.match_videos AS video ON video.id=link.video_id
        WHERE video.match_id IS NOT NULL
        GROUP BY link.event_id, video.match_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Cannot roll back while team-history links require exact video identity';
    END IF;
END $$;

CREATE TABLE public.history_event_matches (
    event_id BIGINT NOT NULL,
    match_id TEXT NOT NULL,
    PRIMARY KEY (event_id, match_id),
    CONSTRAINT fk_history_event_match_event
        FOREIGN KEY (event_id) REFERENCES public.history_events(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_history_event_match_match
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id)
        ON DELETE CASCADE
);

INSERT INTO public.history_event_matches(event_id, match_id)
SELECT link.event_id, video.match_id
FROM public.history_event_videos AS link
JOIN public.match_videos AS video ON video.id=link.video_id
WHERE video.match_id IS NOT NULL;

REVOKE ALL PRIVILEGES ON TABLE public.history_event_matches FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.history_event_matches FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

DROP TABLE public.history_event_videos;
