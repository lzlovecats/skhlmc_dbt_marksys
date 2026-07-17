-- Replace forum match links with exact replay-video links and add live links
-- from forum threads to team-history events.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE public.ghost_forum_thread_videos (
    thread_id BIGINT NOT NULL,
    video_id  INTEGER NOT NULL,
    PRIMARY KEY (thread_id, video_id),
    CONSTRAINT fk_ghost_thread_video_thread
        FOREIGN KEY (thread_id) REFERENCES public.ghost_forum_threads(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_video_video
        FOREIGN KEY (video_id) REFERENCES public.match_videos(id)
        ON DELETE CASCADE
);

INSERT INTO public.ghost_forum_thread_videos(thread_id, video_id)
SELECT link.thread_id, selected_video.video_id
FROM public.ghost_forum_thread_matches AS link
JOIN LATERAL (
    SELECT video.id AS video_id
    FROM public.match_videos AS video
    WHERE video.match_id=link.match_id
      AND COALESCE(video.is_visible, TRUE)=TRUE
    ORDER BY video.display_order ASC NULLS LAST,
             video.created_at DESC,
             video.id DESC
    LIMIT 1
) AS selected_video ON TRUE
ON CONFLICT (thread_id, video_id) DO NOTHING;

CREATE TABLE public.ghost_forum_thread_history_events (
    thread_id BIGINT NOT NULL,
    event_id  BIGINT NOT NULL,
    PRIMARY KEY (thread_id, event_id),
    CONSTRAINT fk_ghost_thread_history_event_thread
        FOREIGN KEY (thread_id) REFERENCES public.ghost_forum_threads(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_ghost_thread_history_event_event
        FOREIGN KEY (event_id) REFERENCES public.history_events(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_ghost_forum_thread_videos_video
    ON public.ghost_forum_thread_videos(video_id);
CREATE INDEX idx_ghost_forum_thread_history_events_event
    ON public.ghost_forum_thread_history_events(event_id);

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_videos FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_videos FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_history_events FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ghost_forum_thread_history_events FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

DROP TABLE public.ghost_forum_thread_matches;
