-- Add the durable, competition-staff-controlled official AI third judge.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.scores
    ADD COLUMN judge_kind TEXT NOT NULL DEFAULT 'human';
ALTER TABLE public.scores
    ADD CONSTRAINT scores_judge_kind_check
    CHECK (judge_kind IN ('human', 'ai'));

CREATE UNIQUE INDEX idx_scores_one_official_ai_judge
    ON public.scores(match_id) WHERE judge_kind = 'ai';

CREATE TABLE public.official_ai_judge_runs (
    match_id             TEXT PRIMARY KEY,
    projector_session_id TEXT NOT NULL,
    operation_id         TEXT NOT NULL UNIQUE,
    status               TEXT NOT NULL
        CHECK (status IN ('ready','processing','retryable','succeeded','fallback')),
    attempt_count        SMALLINT NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 2),
    current_model_label  TEXT,
    final_model_label    TEXT,
    final_judge_name     TEXT,
    last_error           TEXT NOT NULL DEFAULT '',
    current_claim_token  TEXT,
    claim_expires_at     TIMESTAMPTZ,
    created_by           TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at         TIMESTAMPTZ,
    CONSTRAINT fk_official_ai_judge_run_match
        FOREIGN KEY (match_id) REFERENCES public.matches(match_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_official_ai_judge_run_session
        FOREIGN KEY (projector_session_id)
        REFERENCES public.projector_ai_sessions(session_id)
        ON DELETE RESTRICT
);

CREATE TABLE public.official_ai_judge_attempts (
    id                    BIGSERIAL PRIMARY KEY,
    match_id              TEXT NOT NULL,
    attempt_no            SMALLINT NOT NULL CHECK (attempt_no BETWEEN 1 AND 2),
    model_label           TEXT NOT NULL,
    provider              TEXT NOT NULL,
    human_judge_count     SMALLINT NOT NULL
        CHECK (human_judge_count >= 2 AND MOD(human_judge_count, 2) = 0),
    pro_deduction         INTEGER NOT NULL CHECK (pro_deduction >= 0),
    con_deduction         INTEGER NOT NULL CHECK (con_deduction >= 0),
    status                TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed','running','succeeded','failed')),
    provider_attempted    BOOLEAN NOT NULL DEFAULT FALSE,
    error_message         TEXT NOT NULL DEFAULT '',
    result_payload        JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider_attempted_at TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ,
    CONSTRAINT fk_official_ai_judge_attempt_run
        FOREIGN KEY (match_id)
        REFERENCES public.official_ai_judge_runs(match_id)
        ON DELETE CASCADE,
    UNIQUE (match_id, attempt_no),
    UNIQUE (match_id, model_label)
);

CREATE INDEX idx_official_ai_judge_attempts_match_created
    ON public.official_ai_judge_attempts(match_id, created_at DESC);

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
            'official_ai_judge'
        )
    );

REVOKE ALL PRIVILEGES ON TABLE public.official_ai_judge_runs FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.official_ai_judge_runs FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.official_ai_judge_attempts FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.official_ai_judge_attempts FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON SEQUENCE public.official_ai_judge_attempts_id_seq
    FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE public.official_ai_judge_attempts_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;
