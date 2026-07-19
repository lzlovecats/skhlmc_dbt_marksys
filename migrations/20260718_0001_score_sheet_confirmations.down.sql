-- Remove the score-sheet confirmation workflow and its bearer links.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DROP TABLE IF EXISTS public.score_sheet_confirmations;
