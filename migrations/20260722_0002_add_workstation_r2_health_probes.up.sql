-- Track short-lived direct-R2 Workstation health probes for bounded cleanup.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.workstation_r2_health_probes (
    intent_id   TEXT        PRIMARY KEY,
    node_id     TEXT        NOT NULL UNIQUE
                            REFERENCES public.lmc_ai_nodes(node_id)
                            ON DELETE CASCADE,
    object_key  TEXT        NOT NULL UNIQUE,
    sha256      TEXT        NOT NULL,
    byte_size   INTEGER     NOT NULL CHECK (byte_size > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT workstation_r2_health_intent_id
        CHECK (char_length(intent_id) = 32 AND intent_id ~ '^[0-9a-f]+$'),
    CONSTRAINT workstation_r2_health_object_key
        CHECK (
            char_length(object_key) BETWEEN 1 AND 512
            AND object_key ~ '^pending/workstation-health/'
        ),
    CONSTRAINT workstation_r2_health_sha256
        CHECK (
            char_length(sha256) = 64
            AND sha256 = lower(sha256)
            AND sha256 ~ '^[0-9a-f]+$'
        )
);

CREATE INDEX idx_workstation_r2_health_created
    ON public.workstation_r2_health_probes(created_at);

COMMENT ON TABLE public.lmc_ai_nodes IS
    'skhlmc-feature:lmc_ai:20260722_0002';

REVOKE ALL PRIVILEGES ON TABLE public.workstation_r2_health_probes FROM PUBLIC;

DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.workstation_r2_health_probes FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
