DO $$
DECLARE
    enabled_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO enabled_count
    FROM public.lmc_ai_nodes
    WHERE enabled = TRUE;

    IF enabled_count > 1 THEN
        RAISE EXCEPTION 'single Workstation migration requires at most one enabled lmc_ai_nodes row';
    END IF;
END
$$;

CREATE UNIQUE INDEX uq_lmc_ai_single_enabled_workstation
    ON public.lmc_ai_nodes(enabled)
    WHERE enabled = TRUE;

DELETE FROM public.app_config
WHERE key IN (
    'lmc_ai_active_node_id',
    'lmc_ai_model_set',
    'lmc_ai_thinking_enabled'
);

COMMENT ON TABLE public.lmc_ai_nodes IS
    'skhlmc-feature:lmc_ai:20260722_0003';
