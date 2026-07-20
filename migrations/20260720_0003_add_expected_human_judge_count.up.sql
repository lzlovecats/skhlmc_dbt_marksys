-- Persist the authoritative planned human-judge count for official AI gating.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.matches
    ADD COLUMN expected_human_judge_count SMALLINT;

ALTER TABLE public.matches
    ADD CONSTRAINT matches_expected_human_judge_count_check
    CHECK (
        expected_human_judge_count IS NULL
        OR expected_human_judge_count BETWEEN 1 AND 50
    );
