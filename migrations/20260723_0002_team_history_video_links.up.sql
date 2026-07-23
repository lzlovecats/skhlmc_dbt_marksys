-- Replace team-history match links with exact replay-video links so one event
-- can retain multiple videos, including several videos from the same match.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE public.history_event_videos (
    event_id BIGINT NOT NULL,
    video_id INTEGER NOT NULL,
    PRIMARY KEY (event_id, video_id),
    CONSTRAINT fk_history_event_video_event
        FOREIGN KEY (event_id) REFERENCES public.history_events(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_history_event_video_video
        FOREIGN KEY (video_id) REFERENCES public.match_videos(id)
        ON DELETE CASCADE
);

INSERT INTO public.history_event_videos(event_id, video_id)
SELECT link.event_id, selected_video.video_id
FROM public.history_event_matches AS link
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
ON CONFLICT (event_id, video_id) DO NOTHING;

CREATE INDEX idx_history_event_videos_video
    ON public.history_event_videos(video_id);

REVOKE ALL PRIVILEGES ON TABLE public.history_event_videos FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.history_event_videos FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

DROP TABLE public.history_event_matches;
