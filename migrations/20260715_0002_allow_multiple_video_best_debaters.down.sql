-- A rollback keeps the first marked chapter for each video before restoring
-- the former one-best-debater constraint.

WITH ranked AS (
    SELECT video_id,
           chapter_label,
           ROW_NUMBER() OVER (
               PARTITION BY video_id
               ORDER BY display_order, start_seconds, chapter_label
           ) AS marker_order
    FROM video_chapters
    WHERE is_best_debater = TRUE
)
UPDATE video_chapters AS chapter
SET is_best_debater = FALSE
FROM ranked
WHERE chapter.video_id = ranked.video_id
  AND chapter.chapter_label = ranked.chapter_label
  AND ranked.marker_order > 1;

CREATE UNIQUE INDEX idx_video_chapters_one_best_debater
    ON video_chapters (video_id)
    WHERE is_best_debater = TRUE;
