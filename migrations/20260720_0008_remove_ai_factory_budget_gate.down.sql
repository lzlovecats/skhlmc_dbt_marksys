-- Restore the historical AI Fund reservation shape.  Existing attempts made
-- while the gate was removed remain readable; NOT VALID avoids rewriting or
-- deleting their permanent provider audit evidence.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.ai_factory_attempts
    ADD CONSTRAINT ai_factory_attempts_budget_reservation
    CHECK (
        (estimated_cost_hkd = 0
            AND budget_provider_name IS NULL
            AND budget_period_month IS NULL
            AND budget_window_start IS NULL)
        OR
        (estimated_cost_hkd > 0
            AND char_length(budget_provider_name) BETWEEN 1 AND 80
            AND budget_period_month IS NOT NULL
            AND budget_window_start IS NOT NULL)
    ) NOT VALID;
