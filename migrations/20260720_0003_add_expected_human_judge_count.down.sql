-- Remove the planned human-judge count when rolling back the feature.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.matches
    DROP CONSTRAINT matches_expected_human_judge_count_check;

ALTER TABLE public.matches
    DROP COLUMN expected_human_judge_count;
