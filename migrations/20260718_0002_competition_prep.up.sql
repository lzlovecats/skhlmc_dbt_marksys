CREATE TABLE competition_prep_projects (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    recent_match_id BIGINT REFERENCES recent_matches(id) ON DELETE SET NULL,
    topic_text TEXT NOT NULL CHECK (char_length(topic_text) BETWEEN 1 AND 500),
    our_side TEXT NOT NULL CHECK (our_side IN ('pro', 'con')),
    debate_format TEXT NOT NULL CHECK (debate_format IN ('校園隨想', '聯中', '星島', '基本法盃')),
    opponent TEXT NOT NULL DEFAULT '' CHECK (char_length(opponent) <= 200),
    match_date DATE NOT NULL,
    match_time TIME,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE competition_prep_members (
    project_id BIGINT NOT NULL REFERENCES competition_prep_projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    added_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, user_id)
);

CREATE UNIQUE INDEX uq_competition_prep_project_owner
    ON competition_prep_members(project_id) WHERE role = 'owner';

CREATE TABLE competition_prep_manuscripts (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES competition_prep_projects(id) ON DELETE CASCADE,
    slot TEXT NOT NULL CHECK (slot IN ('main', 'dep1', 'dep2', 'dep3', 'closing', 'interaction', 'other')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    body TEXT NOT NULL DEFAULT '',
    assigned_user_id TEXT REFERENCES accounts(user_id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'reviewed', 'final')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, slot)
);

CREATE TABLE competition_prep_strategy_cards (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES competition_prep_projects(id) ON DELETE CASCADE,
    parent_card_id BIGINT REFERENCES competition_prep_strategy_cards(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('mainline', 'definition', 'standard', 'burden', 'argument', 'opponent_argument', 'attack', 'opponent_answer', 'rebuttal', 'defence_floor', 'concession', 'question')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    content TEXT NOT NULL DEFAULT '',
    assigned_slot TEXT CHECK (assigned_slot IS NULL OR assigned_slot IN ('main', 'dep1', 'dep2', 'dep3', 'closing', 'interaction')),
    priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'handled', 'risk', 'not_applicable')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE competition_prep_evidence_cards (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES competition_prep_projects(id) ON DELETE CASCADE,
    claim_text TEXT NOT NULL CHECK (char_length(claim_text) BETWEEN 1 AND 500),
    excerpt TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '' CHECK (char_length(source_url) <= 2000),
    source_name TEXT NOT NULL DEFAULT '' CHECK (char_length(source_name) <= 200),
    published_date DATE,
    accessed_date DATE NOT NULL DEFAULT CURRENT_DATE,
    region TEXT NOT NULL DEFAULT '' CHECK (char_length(region) <= 100),
    source_type TEXT NOT NULL DEFAULT 'other' CHECK (source_type IN ('government', 'academic', 'news', 'ngo', 'industry', 'ai_research', 'other')),
    side_scope TEXT NOT NULL DEFAULT 'both' CHECK (side_scope IN ('our', 'opponent', 'both')),
    limitations TEXT NOT NULL DEFAULT '',
    linked_strategy_card_id BIGINT REFERENCES competition_prep_strategy_cards(id) ON DELETE SET NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE competition_prep_weaknesses (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES competition_prep_projects(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL DEFAULT 'manual' CHECK (source_type IN ('manual', 'audit', 'speech', 'strategy')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'logic' CHECK (category IN ('logic', 'evidence', 'definition', 'response', 'delivery', 'coordination')),
    assigned_user_id TEXT REFERENCES accounts(user_id) ON DELETE SET NULL,
    priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'practicing', 'passed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    updated_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE competition_prep_ai_runs (
    run_id TEXT PRIMARY KEY CHECK (char_length(run_id) BETWEEN 16 AND 200),
    project_id BIGINT NOT NULL REFERENCES competition_prep_projects(id) ON DELETE CASCADE,
    run_type TEXT NOT NULL CHECK (run_type IN ('team_audit', 'strategy_seed', 'strategy_attack', 'speech_review', 'speech_retake', 'weakness_feedback')),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    model_label TEXT NOT NULL CHECK (char_length(model_label) BETWEEN 1 AND 120),
    snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(snapshot_json) = 'object'),
    output_markdown TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES accounts(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_competition_prep_projects_expiry
    ON competition_prep_projects(expires_at, id);
CREATE INDEX idx_competition_prep_members_user
    ON competition_prep_members(user_id, project_id);
CREATE INDEX idx_competition_prep_manuscripts_project
    ON competition_prep_manuscripts(project_id, slot);
CREATE INDEX idx_competition_prep_strategy_project
    ON competition_prep_strategy_cards(project_id, sort_order, id);
CREATE INDEX idx_competition_prep_evidence_project
    ON competition_prep_evidence_cards(project_id, id);
CREATE INDEX idx_competition_prep_weakness_project
    ON competition_prep_weaknesses(project_id, status, priority, id);
CREATE INDEX idx_competition_prep_ai_runs_project
    ON competition_prep_ai_runs(project_id, created_at DESC);

REVOKE ALL PRIVILEGES ON TABLE competition_prep_projects FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE competition_prep_projects FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON TABLE competition_prep_members FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE competition_prep_members FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON TABLE competition_prep_manuscripts FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE competition_prep_manuscripts FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON TABLE competition_prep_strategy_cards FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE competition_prep_strategy_cards FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON TABLE competition_prep_evidence_cards FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE competition_prep_evidence_cards FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON TABLE competition_prep_weaknesses FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE competition_prep_weaknesses FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON TABLE competition_prep_ai_runs FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE competition_prep_ai_runs FROM ' || quote_ident(role_name);
    END LOOP;
END $$;
REVOKE ALL PRIVILEGES ON SEQUENCE competition_prep_projects_id_seq,
    competition_prep_manuscripts_id_seq, competition_prep_strategy_cards_id_seq,
    competition_prep_evidence_cards_id_seq, competition_prep_weaknesses_id_seq
    FROM PUBLIC;

DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE competition_prep_projects_id_seq, competition_prep_manuscripts_id_seq, competition_prep_strategy_cards_id_seq, competition_prep_evidence_cards_id_seq, competition_prep_weaknesses_id_seq FROM '
            || quote_ident(role_name);
    END LOOP;
END $$;

ALTER TABLE ai_fund_usage_logs DROP CONSTRAINT IF EXISTS ai_fund_usage_logs_feature_check;
ALTER TABLE ai_fund_usage_logs ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
    feature IN ('speech_review', 'strategy', 'competition_prep', 'web_research', 'fact_check', 'free_debate_live', 'full_mock_live', 'vote_review', 'vote_analysis', 'vote_discussion', 'tts_review', 'tts_script_analysis', 'llm_review', 'kiosk_match_review', 'tts', 'kiosk_match_review_tts')
);
