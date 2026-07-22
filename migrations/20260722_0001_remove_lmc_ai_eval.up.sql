-- Permanently retire the fixed local-AI A/B evaluation feature.
-- Historical zero-cost attempt rows remain in ai_fund_usage_logs.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DROP INDEX public.uq_ai_eval_usage_operation_stage;
DROP TABLE public.ai_eval_reviews;
DROP TABLE public.ai_eval_outputs;
DROP TABLE public.ai_eval_campaigns;
DROP TABLE public.ai_eval_cases;
