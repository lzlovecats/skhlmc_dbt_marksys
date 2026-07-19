-- Prepare all three discussion stores for stable repository-backed sticker ids.
-- Only the graduate forum uses the field in this release; vote/video remain
-- text-only until their separate product and AI contracts are implemented.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.ghost_forum_posts
    ADD COLUMN sticker_id TEXT,
    ADD CONSTRAINT chk_ghost_forum_posts_sticker_id
        CHECK (
            sticker_id IS NULL
            OR (char_length(sticker_id) BETWEEN 1 AND 200 AND body = '')
        );

ALTER TABLE public.motion_comments
    ADD COLUMN sticker_id TEXT,
    ADD CONSTRAINT chk_motion_comments_sticker_id
        CHECK (sticker_id IS NULL OR char_length(sticker_id) BETWEEN 1 AND 200);

ALTER TABLE public.video_comments
    ADD COLUMN sticker_id TEXT,
    ADD CONSTRAINT chk_video_comments_sticker_id
        CHECK (sticker_id IS NULL OR char_length(sticker_id) BETWEEN 1 AND 200);
