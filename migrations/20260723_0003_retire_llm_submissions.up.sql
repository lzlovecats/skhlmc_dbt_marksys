-- Retire the database-backed LLM submission workflow after its accepted rows
-- have been exported through the authorised operational backup procedure.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DELETE FROM public.ai_training_audit
WHERE target_type='llm_submission';

DROP TABLE public.llm_training_submissions;
