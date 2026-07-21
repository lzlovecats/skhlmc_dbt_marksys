-- Let the AI data factory run independently of the separately managed AI Fund.
-- Estimated and actual provider costs remain in the existing audit ledgers.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.ai_factory_attempts
    DROP CONSTRAINT ai_factory_attempts_budget_reservation;
