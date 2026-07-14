DROP INDEX IF EXISTS idx_video_roster_member_video;
DROP TABLE IF EXISTS video_roster;

DROP INDEX IF EXISTS idx_video_chapters_one_best_debater;
ALTER TABLE video_chapters
    DROP CONSTRAINT IF EXISTS video_chapters_best_role_check;
ALTER TABLE video_chapters
    DROP COLUMN IF EXISTS is_best_debater;
