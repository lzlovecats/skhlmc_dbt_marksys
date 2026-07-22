-- Remove the Workstation health-probe ledger only when no cleanup is pending.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.workstation_r2_health_probes LIMIT 1
    ) THEN
        RAISE EXCEPTION
            'refusing to remove unfinished Workstation R2 health probes';
    END IF;
END $$;

DROP TABLE public.workstation_r2_health_probes;

COMMENT ON TABLE public.lmc_ai_nodes IS
    'skhlmc-feature:lmc_ai:20260720_0010';
