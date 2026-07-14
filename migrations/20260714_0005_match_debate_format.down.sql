-- Rollback removes official format metadata added by the forward migration.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.matches
    DROP CONSTRAINT matches_free_debate_minutes_check,
    DROP CONSTRAINT matches_debate_format_check,
    DROP COLUMN free_debate_minutes,
    DROP COLUMN debate_format;
