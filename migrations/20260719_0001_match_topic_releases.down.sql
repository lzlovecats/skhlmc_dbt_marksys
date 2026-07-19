-- Remove match-topic release links, staged topics, and veto audit data.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DROP TABLE IF EXISTS public.match_topic_releases;
