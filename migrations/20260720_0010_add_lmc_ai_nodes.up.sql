-- Provision the private outbound local-AI node registry and usage category.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.lmc_ai_nodes (
    node_id               TEXT        PRIMARY KEY,
    display_name          TEXT        NOT NULL,
    token_hash            TEXT        NOT NULL UNIQUE,
    enabled               BOOLEAN     NOT NULL DEFAULT TRUE,
    last_runtime          TEXT,
    last_runtime_version  TEXT,
    last_model            TEXT,
    last_capabilities     JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_connected_at     TIMESTAMPTZ,
    last_disconnected_at  TIMESTAMPTZ,
    CONSTRAINT lmc_ai_nodes_id_length
        CHECK (char_length(node_id) BETWEEN 1 AND 64),
    CONSTRAINT lmc_ai_nodes_name_length
        CHECK (char_length(display_name) BETWEEN 1 AND 80),
    CONSTRAINT lmc_ai_nodes_token_hash
        CHECK (
            char_length(token_hash) = 64
            AND token_hash = lower(token_hash)
            AND token_hash ~ '^[0-9a-f]+$'
        ),
    CONSTRAINT lmc_ai_nodes_capabilities_object
        CHECK (
            last_capabilities IS NULL
            OR jsonb_typeof(last_capabilities) = 'object'
        )
);

CREATE INDEX idx_lmc_ai_nodes_enabled_created
    ON public.lmc_ai_nodes(enabled, created_at DESC);

COMMENT ON TABLE public.lmc_ai_nodes IS
    'skhlmc-feature:lmc_ai:20260720_0010';

REVOKE ALL PRIVILEGES ON TABLE public.lmc_ai_nodes FROM PUBLIC;

DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.lmc_ai_nodes FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

ALTER TABLE public.ai_fund_usage_logs
    ADD COLUMN provider_duration_ms INTEGER NOT NULL DEFAULT 0
        CHECK (provider_duration_ms >= 0);

ALTER TABLE public.ai_fund_usage_logs
    DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs
    ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
        feature IN (
            'speech_review', 'strategy', 'competition_prep', 'web_research',
            'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review',
            'vote_analysis', 'vote_discussion', 'tts_review',
            'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts',
            'kiosk_match_review_tts', 'data_factory_generation',
            'official_ai_judge', 'lmc_ai_chat'
        )
    );
