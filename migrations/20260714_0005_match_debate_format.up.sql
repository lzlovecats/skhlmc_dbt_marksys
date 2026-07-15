-- Store the official debate format alongside each match so Kiosk and other
-- competition-day tools do not rely on browser-supplied format metadata.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.matches
    ADD COLUMN debate_format TEXT,
    ADD COLUMN free_debate_minutes NUMERIC(4,1);

UPDATE public.matches
SET debate_format = '校園隨想'
WHERE debate_format IS NULL;

ALTER TABLE public.matches
    ALTER COLUMN debate_format SET DEFAULT '校園隨想',
    ALTER COLUMN debate_format SET NOT NULL,
    ADD CONSTRAINT matches_debate_format_check
        CHECK (debate_format IN ('校園隨想', '聯中', '星島', '基本法盃')),
    ADD CONSTRAINT matches_free_debate_minutes_check
        CHECK (
            free_debate_minutes IS NULL
            OR (
                debate_format = '聯中'
                AND free_debate_minutes BETWEEN 2 AND 10
            )
        );
