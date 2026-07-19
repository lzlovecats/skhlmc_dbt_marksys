SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.video_comments
    DROP CONSTRAINT chk_video_comments_sticker_id,
    DROP COLUMN sticker_id;

ALTER TABLE public.motion_comments
    DROP CONSTRAINT chk_motion_comments_sticker_id,
    DROP COLUMN sticker_id;

ALTER TABLE public.ghost_forum_posts
    DROP CONSTRAINT chk_ghost_forum_posts_sticker_id,
    DROP COLUMN sticker_id;
